from __future__ import annotations

from typing import Any

import pytest

from swiftpy import (
    Application,
    HttpException,
    Request,
    Response,
    Router,
    abort,
    json_response,
)
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.router import RouteGroup, RouteRegistrationError
from swiftpy.testing.http_client import TestClient


class _TraceOneMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        request.state.setdefault("mw", []).append("one")
        resp = await next_handler(request)
        resp.headers["X-MW-One"] = "1"
        return resp


class _TraceTwoMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        request.state.setdefault("mw", []).append("two")
        resp = await next_handler(request)
        resp.headers["X-MW-Two"] = "2"
        return resp


class _DeclarativeConflict(HttpException):
    status_code = 409
    message = "Already exists"


@pytest.mark.asyncio
async def test_request_direct_attribute_access_and_input_methods() -> None:
    app = Application(auto_discover_providers=False, max_body_size=128)

    @app.post("/register")
    async def register(request: Request) -> Response:
        email = request.email
        has_name = request.has("name")
        fallback_role = request.get("role", "member")
        # Real Request attributes win over payload keys ('path', 'method'); .input() reads shadowed key
        real_path = request.path
        payload_path = await request.input("path")
        all_inputs = request.all()
        return json_response(
            {
                "email": email,
                "has_name": has_name,
                "role": fallback_role,
                "real_path": real_path,
                "payload_path": payload_path,
                "all": all_inputs,
            }
        )

    @app.get("/items")
    async def list_items(request: Request) -> Response:
        return json_response(
            {
                "page": request.page,
                "has_filter": request.has("filter"),
                "all": request.all(),
            }
        )

    @app.post("/missing")
    async def missing_attr(request: Request) -> Response:
        try:
            _ = request.does_not_exist
        except AttributeError:
            return json_response({"missing_raised": True})
        return json_response({"missing_raised": False}, status_code=500)

    client = TestClient(app)

    # 1. JSON POST auto-loads request input
    res_json = await client.post(
        "/register",
        json={"email": "dev@swiftpy.dev", "name": "Ada", "path": "/shadowed"},
    )
    res_json.assert_ok()
    assert res_json.json() == {
        "email": "dev@swiftpy.dev",
        "has_name": True,
        "role": "member",
        "real_path": "/register",
        "payload_path": "/shadowed",
        "all": {"email": "dev@swiftpy.dev", "name": "Ada", "path": "/shadowed"},
    }

    # 2. Form urlencoded POST auto-loads request input too
    res_form = await client.post(
        "/register",
        data={"email": "form@swiftpy.dev", "path": "/form-path"},
    )
    res_form.assert_ok()
    assert res_form.json()["email"] == "form@swiftpy.dev"
    assert res_form.json()["has_name"] is False
    assert res_form.json()["payload_path"] == "/form-path"

    # 3. GET query params accessible via attribute without await
    res_get = await client.get("/items", query={"page": "3"})
    res_get.assert_ok()
    assert res_get.json() == {"page": "3", "has_filter": False, "all": {"page": "3"}}

    # 4. Missing field raises AttributeError
    res_missing = await client.post("/missing", json={"email": "a@b.com"})
    res_missing.assert_ok()
    assert res_missing.json() == {"missing_raised": True}

    # 5. Oversized JSON payload triggers 413 during auto-load
    res_big = await client.post("/register", json={"email": "x" * 200})
    assert res_big.status_code == 413


@pytest.mark.asyncio
async def test_request_auto_load_opt_out_and_multipart_not_eagerly_read() -> None:
    app_no_autoload = Application(
        auto_discover_providers=False,
        auto_load_request_input=False,
    )

    @app_no_autoload.post("/manual")
    async def manual(request: Request) -> Response:
        before_loaded = request._input_loaded
        with pytest.raises(AttributeError):
            _ = request.email
        loaded = await request.input()
        return json_response(
            {
                "before_loaded": before_loaded,
                "email_after": request.email,
                "loaded": loaded,
            }
        )

    @app_no_autoload.post("/upload")
    async def upload(request: Request) -> Response:
        return json_response({"eagerly_loaded": request._input_loaded})

    client_off = TestClient(app_no_autoload)
    res = await client_off.post("/manual", json={"email": "manual@example.com"})
    res.assert_ok()
    assert res.json() == {
        "before_loaded": False,
        "email_after": "manual@example.com",
        "loaded": {"email": "manual@example.com"},
    }

    # Even with default auto_load=True, multipart requests are never eagerly consumed
    app_default = Application(auto_discover_providers=False)
    multipart_body_calls = 0

    @app_default.post("/multipart")
    async def multipart_handler(request: Request) -> Response:
        return json_response({"loaded": request._input_loaded})

    async def multipart_receive() -> dict[str, Any]:
        nonlocal multipart_body_calls
        multipart_body_calls += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def capture_send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await app_default(
        {
            "type": "http",
            "method": "POST",
            "path": "/multipart",
            "headers": [(b"content-type", b"multipart/form-data; boundary=abc")],
            "query_string": b"",
        },
        multipart_receive,
        capture_send,
    )
    assert multipart_body_calls == 0
    assert b'"loaded":false' in sent[1]["body"].replace(b" ", b"")


@pytest.mark.asyncio
async def test_router_prefix_and_middleware_composition() -> None:
    app = Application(auto_discover_providers=False)

    api = Router(prefix="/v1", middleware=[_TraceOneMiddleware])

    @api.get("/ping")
    async def ping() -> Response:
        return json_response({"pong": True})

    @api.group("/admin", middleware=[_TraceTwoMiddleware])
    def _admin_group(group: RouteGroup) -> None:
        @group.get("/stats")
        async def stats(request: Request) -> Response:
            return json_response({"mw": request.state.get("mw", [])})

    app.include_router(api, prefix="/api")

    client = TestClient(app)

    res_ping = await client.get("/api/v1/ping")
    res_ping.assert_ok()
    assert res_ping.json() == {"pong": True}
    assert res_ping.headers["x-mw-one"] == "1"

    res_stats = await client.get("/api/v1/admin/stats")
    res_stats.assert_ok()
    assert res_stats.json() == {"mw": ["one", "two"]}
    assert res_stats.headers["x-mw-one"] == "1"
    assert res_stats.headers["x-mw-two"] == "2"

    # Invalid prefix rejected eagerly on Router(...)
    with pytest.raises(RouteRegistrationError, match="dynamic pattern"):
        Router(prefix="/users/{id}")


@pytest.mark.asyncio
async def test_declarative_http_exception_and_abort_helper() -> None:
    app = Application(auto_discover_providers=False)

    @app.get("/conflict-default")
    async def conflict_default() -> Response:
        raise _DeclarativeConflict()

    @app.get("/conflict-custom")
    async def conflict_custom() -> Response:
        raise _DeclarativeConflict("Email already registered")

    @app.get("/abort-default")
    async def abort_default() -> Response:
        abort(404)

    @app.get("/abort-custom")
    async def abort_custom() -> Response:
        abort(429, "Slow down", headers={"Retry-After": "30"})

    client = TestClient(app)

    r1 = await client.get("/conflict-default")
    assert r1.status_code == 409
    assert r1.json() == {"detail": "Already exists"}

    r2 = await client.get("/conflict-custom")
    assert r2.status_code == 409
    assert r2.json() == {"detail": "Email already registered"}

    r3 = await client.get("/abort-default")
    assert r3.status_code == 404
    assert r3.json() == {"detail": "Not Found"}

    r4 = await client.get("/abort-custom")
    assert r4.status_code == 429
    assert r4.json() == {"detail": "Slow down"}
    assert r4.headers["retry-after"] == "30"


@pytest.mark.asyncio
async def test_request_reserved_attrs_not_spoofed_and_raw_webhook_autoload_safe() -> None:
    app = Application(auto_discover_providers=False)

    @app.get("/inspect-security")
    async def inspect_security(request: Request) -> Response:
        return json_response(
            {
                "scheme": request.scheme,
                "getattr_user": getattr(request, "user", None),
                "hasattr_is_admin": hasattr(request, "is_admin"),
                "hasattr_session": hasattr(request, "session"),
                "explicit_user": request.get("user"),
                "explicit_is_admin": await request.input("is_admin"),
            }
        )

    @app.post("/raw-webhook")
    async def raw_webhook(request: Request) -> Response:
        raw = await request.body()
        return json_response({"raw_len": len(raw), "loaded": request._input_loaded})

    @app.post("/explicit-json")
    async def explicit_json(request: Request) -> Response:
        data = await request.input()
        return json_response({"data": data})

    client = TestClient(app)

    # 1. Query params cannot spoof request.scheme or reserved auth/session attributes
    sec_res = await client.get(
        "/inspect-security",
        query={"scheme": "https", "user": "admin", "is_admin": "1", "session": "forged"},
    )
    sec_res.assert_ok()
    assert sec_res.json() == {
        "scheme": "http",
        "getattr_user": None,
        "hasattr_is_admin": False,
        "hasattr_session": False,
        "explicit_user": "admin",
        "explicit_is_admin": "1",
    }

    # 2. Raw webhook handler taking `request: Request` with non-JSON body still runs under auto_load=True
    wh_res = await client.post(
        "/raw-webhook",
        data=b"{not-valid-json-signature-body",
        headers={"content-type": "application/json"},
    )
    wh_res.assert_ok()
    assert wh_res.json() == {"raw_len": 30, "loaded": False}

    # 3. Handler that explicitly calls `await request.input()` on malformed JSON still raises 400
    bad_json_res = await client.post(
        "/explicit-json",
        data=b"{not-valid-json",
        headers={"content-type": "application/json"},
    )
    assert bad_json_res.status_code == 400
    assert bad_json_res.json() == {"detail": "Invalid JSON payload"}
