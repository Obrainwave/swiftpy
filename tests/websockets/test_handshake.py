from __future__ import annotations

from typing import Any

import pytest

from swiftpy.auth.api_key_guard import require_api_key
from swiftpy.auth.exceptions import AuthenticationError
from swiftpy.auth.jwt_guard import JWTGuard
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.core.application import Application
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response, forbidden, unauthorized
from swiftpy.http.router import RouteGroup
from swiftpy.testing.http_client import TestClient
from swiftpy.websockets.connection import WebSocketConnection, WebSocketDisconnect


class AuthMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        auth_header = request.headers.get("authorization")
        if not auth_header or auth_header != "Bearer valid-token":
            return unauthorized("Invalid or missing token")
        request.state["auth_user"] = "admin_user"
        return await next_handler(request)


class OrderTrackingMiddleware(Middleware):
    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        trace = request.state.get("trace", [])
        trace.append(f"enter:{self.tag}")
        request.state["trace"] = trace
        resp = await next_handler(request)
        trace.append(f"exit:{self.tag}")
        return resp


@pytest.mark.asyncio
async def test_handshake_middleware_rejects_unauthenticated_connection_with_1008() -> None:
    """
    Middleware returning status >= 400 before accept() must close the connection with 1008 (Policy Violation).
    """
    app = Application()
    app.middleware([AuthMiddleware])

    accepted = False

    @app.websocket("/ws/secure")
    async def secure_ws(ws: WebSocketConnection) -> None:
        nonlocal accepted
        await ws.accept()
        accepted = True
        await ws.send_text("welcome")

    client = TestClient(app)

    # 1. Unauthenticated connection is rejected with 1008
    with pytest.raises(WebSocketDisconnect) as exc_info:
        async with client.websocket_connect("/ws/secure"):
            pass
    assert exc_info.value.code == 1008
    assert accepted is False

    # 2. Authenticated connection succeeds
    async with client.websocket_connect(
        "/ws/secure",
        headers={"authorization": "Bearer valid-token"},
    ) as ws:
        msg = await ws.receive_text()
        assert msg == "welcome"
    assert accepted is True


@pytest.mark.asyncio
async def test_handshake_middleware_state_propagation_to_connection() -> None:
    """
    State set on Request during handshake middleware must be propagated to WebSocketConnection.state.
    """
    app = Application()
    app.middleware([AuthMiddleware])

    captured_state: dict[str, object] = {}

    @app.websocket("/ws/state")
    async def state_ws(ws: WebSocketConnection) -> None:
        nonlocal captured_state
        await ws.accept()
        captured_state = dict(ws.state)
        await ws.send_text("ok")

    client = TestClient(app)

    async with client.websocket_connect(
        "/ws/state",
        headers={"authorization": "Bearer valid-token"},
    ) as ws:
        msg = await ws.receive_text()
        assert msg == "ok"

    assert captured_state.get("auth_user") == "admin_user"


@pytest.mark.asyncio
async def test_global_and_route_group_middleware_execution_order() -> None:
    """
    Global middleware must execute before route group middleware in the handshake pipeline.
    """
    order_log: list[str] = []

    class GlobalMiddleware(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            order_log.append("global_before")
            resp = await next_handler(request)
            order_log.append("global_after")
            return resp

    class GroupMiddleware(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            order_log.append("group_before")
            resp = await next_handler(request)
            order_log.append("group_after")
            return resp

    app = Application()
    app.middleware([GlobalMiddleware])

    @app.router.group("/ws/grouped", middleware=[GroupMiddleware])
    def setup_group(group: RouteGroup) -> None:
        @group.websocket("/channel")
        async def group_ws(ws: WebSocketConnection) -> None:
            await ws.accept()
            await ws.send_text("in_group")

    client = TestClient(app)

    async with client.websocket_connect("/ws/grouped/channel") as ws:
        msg = await ws.receive_text()
        assert msg == "in_group"

    assert order_log == ["global_before", "group_before", "group_after", "global_after"]


@pytest.mark.asyncio
async def test_route_group_middleware_rejects_with_1008() -> None:
    """
    Group middleware rejection before accept() closes connection with 1008.
    """
    class RejectingGroupMiddleware(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            return forbidden("Group access denied")

    app = Application()

    @app.router.group("/ws/restricted", middleware=[RejectingGroupMiddleware])
    def setup_restricted(group: RouteGroup) -> None:
        @group.websocket("/feed")
        async def feed_ws(ws: WebSocketConnection) -> None:
            await ws.accept()

    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        async with client.websocket_connect("/ws/restricted/feed"):
            pass
    assert exc_info.value.code == 1008


@pytest.mark.asyncio
async def test_origin_validation_decision_d14() -> None:
    """
    Origin checks according to Decision D-14:
    - Same-origin only by default (matches request Host header).
    - Configured allowed_origins allows matching origins or '*'.
    - Disallowed origins are rejected with code 1008.
    - Missing Origin header (non-browser clients) is allowed.
    """
    # 1. Default: same-origin check against Host
    app_default = Application()

    @app_default.websocket("/ws/same-origin")
    async def default_ws(ws: WebSocketConnection) -> None:
        await ws.accept()
        await ws.send_text("same-origin-ok")

    client_default = TestClient(app_default)

    # Missing Origin (CLI / native client) -> allowed
    async with client_default.websocket_connect("/ws/same-origin") as ws:
        assert await ws.receive_text() == "same-origin-ok"

    # Same origin matching Host (127.0.0.1:80 by default in TestClient) -> allowed
    async with client_default.websocket_connect(
        "/ws/same-origin",
        headers={"origin": "http://127.0.0.1:80"},
    ) as ws:
        assert await ws.receive_text() == "same-origin-ok"

    # Evil origin -> rejected with 1008
    with pytest.raises(WebSocketDisconnect) as exc_same:
        async with client_default.websocket_connect(
            "/ws/same-origin",
            headers={"origin": "http://evil-attacker.com"},
        ):
            pass
    assert exc_same.value.code == 1008

    # 2. Configured allowed_origins with specific domains
    app_whitelist = Application(allowed_origins=["https://myapp.com", "http://localhost:3000"])

    @app_whitelist.websocket("/ws/whitelist")
    async def whitelist_ws(ws: WebSocketConnection) -> None:
        await ws.accept()
        await ws.send_text("whitelist-ok")

    client_whitelist = TestClient(app_whitelist)

    async with client_whitelist.websocket_connect(
        "/ws/whitelist",
        headers={"origin": "https://myapp.com"},
    ) as ws:
        assert await ws.receive_text() == "whitelist-ok"

    with pytest.raises(WebSocketDisconnect) as exc_whitelist:
        async with client_whitelist.websocket_connect(
            "/ws/whitelist",
            headers={"origin": "https://otherapp.com"},
        ):
            pass
    assert exc_whitelist.value.code == 1008

    # 3. Explicit wildcard allowed_origins=["*"]
    app_wildcard = Application(allowed_origins=["*"])

    @app_wildcard.websocket("/ws/wildcard")
    async def wildcard_ws(ws: WebSocketConnection) -> None:
        await ws.accept()
        await ws.send_text("wildcard-ok")

    client_wildcard = TestClient(app_wildcard)

    async with client_wildcard.websocket_connect(
        "/ws/wildcard",
        headers={"origin": "https://anywhere.io"},
    ) as ws:
        assert await ws.receive_text() == "wildcard-ok"


@pytest.mark.asyncio
async def test_jwt_guard_in_websocket_handshake() -> None:
    """
    Test JWTGuard authenticating WebSocket handshakes via:
    1. Authorization header (Bearer <token>)
    2. Sec-WebSocket-Protocol (['bearer', '<token>'])
    3. Query parameter (?token=<token>)
    4. Rejecting invalid or missing tokens with 1008
    """
    issuer = JWTIssuer(secret="very-long-and-secure-secret-key-32bytes!")

    app = Application()
    app.container.instance(JWTIssuer, issuer)
    app.middleware([JWTGuard])

    @app.websocket("/ws/jwt")
    async def jwt_ws(ws: WebSocketConnection) -> None:
        await ws.accept()
        user = ws.state.get("user", {})
        sub = user.get("sub", "") if isinstance(user, dict) else ""
        await ws.send_text(f"hello {sub}")

    client = TestClient(app)
    valid_token = issuer.issue_token("user_42")

    # 1. Valid token in Authorization header
    async with client.websocket_connect(
        "/ws/jwt",
        headers={"authorization": f"Bearer {valid_token}"},
    ) as ws:
        assert await ws.receive_text() == "hello user_42"

    # 2. Valid token in Sec-WebSocket-Protocol subprotocols
    async with client.websocket_connect(
        "/ws/jwt",
        subprotocols=["bearer", valid_token],
    ) as ws:
        assert await ws.receive_text() == "hello user_42"

    # 3. Valid token in query parameter
    async with client.websocket_connect(
        f"/ws/jwt?token={valid_token}",
    ) as ws:
        assert await ws.receive_text() == "hello user_42"

    # 4. Invalid token closes with 1008
    with pytest.raises(WebSocketDisconnect) as exc_info:
        async with client.websocket_connect(
            "/ws/jwt",
            headers={"authorization": "Bearer invalid.jwt.token"},
        ):
            pass
    assert exc_info.value.code == 1008


@pytest.mark.asyncio
async def test_api_key_guard_in_websocket_handshake() -> None:
    """
    Test API key authentication in WebSocket handshakes via:
    1. Custom header (x-api-key)
    2. Authorization header (ApiKey <key>)
    3. Sec-WebSocket-Protocol (['apikey', '<key>'])
    4. Query parameter (?api_key=<key>)
    5. Rejection with code 1008 on missing or invalid key
    """
    class ApiKeyMiddleware(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            async def mock_validator(raw_key: str) -> dict[str, Any] | None:
                if raw_key == "sk_live_123.secret456":
                    return {"user_id": 99, "role": "admin"}
                return None

            try:
                user = await require_api_key(request, key_validator=mock_validator)
                request.state["api_user"] = user
            except AuthenticationError:
                return unauthorized("Invalid API key")
            return await next_handler(request)

    app = Application()
    app.middleware([ApiKeyMiddleware])

    @app.websocket("/ws/api-key")
    async def api_key_ws(ws: WebSocketConnection) -> None:
        await ws.accept()
        user = ws.state.get("api_user", {})
        user_id = user.get("user_id") if isinstance(user, dict) else None
        await ws.send_text(f"auth_ok:{user_id}")

    client = TestClient(app)
    valid_key = "sk_live_123.secret456"

    # 1. Custom header
    async with client.websocket_connect(
        "/ws/api-key",
        headers={"x-api-key": valid_key},
    ) as ws:
        assert await ws.receive_text() == "auth_ok:99"

    # 2. Authorization header
    async with client.websocket_connect(
        "/ws/api-key",
        headers={"authorization": f"ApiKey {valid_key}"},
    ) as ws:
        assert await ws.receive_text() == "auth_ok:99"

    # 3. Sec-WebSocket-Protocol
    async with client.websocket_connect(
        "/ws/api-key",
        subprotocols=["apikey", valid_key],
    ) as ws:
        assert await ws.receive_text() == "auth_ok:99"

    # 4. Query parameter
    async with client.websocket_connect(
        f"/ws/api-key?api_key={valid_key}",
    ) as ws:
        assert await ws.receive_text() == "auth_ok:99"

    # 5. Invalid key rejected with 1008
    with pytest.raises(WebSocketDisconnect) as exc_info:
        async with client.websocket_connect(
            "/ws/api-key",
            headers={"x-api-key": "sk_live_wrong.key"},
        ):
            pass
    assert exc_info.value.code == 1008

