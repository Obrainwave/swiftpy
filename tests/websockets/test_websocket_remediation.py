from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.events.redis_bus import RedisEventBus
from swiftpy.testing.http_client import TestClient
from swiftpy.websockets.connection import (
    WebSocketConnection,
    WebSocketDisconnect,
    WebSocketState,
)


class _RecordingConnection(WebSocketConnection):
    def __init__(self, conn_id: str) -> None:
        async def _noop_receive() -> dict[str, Any]:
            return {"type": "websocket.disconnect", "code": 1000}

        async def _noop_send(_msg: dict[str, Any]) -> None:
            return None

        super().__init__(
            scope={"type": "websocket", "path": "/ws"},
            receive=_noop_receive,
            send=_noop_send,
        )
        self.conn_id = conn_id
        self.client_state = WebSocketState.CONNECTED
        self.received_messages: list[str | bytes] = []

    async def send_text(self, text: str) -> None:
        self.received_messages.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.received_messages.append(data)


@pytest.mark.asyncio
async def test_websocket_origin_parser_downgrade_and_empty_allowlist() -> None:
    """WS-1: Prevent Origin split('://') bypass, http->wss downgrade, and preserve allowed_origins=[]."""
    # 1. Explicit websockets.allowed_origins=[] denies all browser origins while preserving None non-browser
    app_deny_all = Application(
        auto_discover_providers=False,
        config={"websockets": {"allowed_origins": []}},
    )
    assert app_deny_all.allowed_origins == set()

    @app_deny_all.websocket("/ws/deny")
    async def deny_ws(ws: WebSocketConnection) -> None:
        await ws.accept()
        await ws.send_text("ok")

    client_deny = TestClient(app_deny_all)
    async with client_deny.websocket_connect("/ws/deny") as ws:
        assert await ws.receive_text() == "ok"

    with pytest.raises(WebSocketDisconnect) as exc_deny:
        async with client_deny.websocket_connect(
            "/ws/deny",
            headers={"origin": "http://127.0.0.1:80"},
        ):
            pass
    assert exc_deny.value.code == 1008

    # 2. Crafted Origin: http://https://myapp.com, userinfo, and extra paths rejected
    app_allow = Application(
        auto_discover_providers=False,
        allowed_origins=["https://myapp.com"],
    )

    @app_allow.websocket("/ws/allow")
    async def allow_ws(ws: WebSocketConnection) -> None:
        await ws.accept()
        await ws.send_text("allowed")

    client_allow = TestClient(app_allow)
    for bad_origin in (
        "http://https://myapp.com",
        "https://evil.com@myapp.com",
        "https://myapp.com/evil",
        "http://myapp.com",
        "null",
    ):
        with pytest.raises(WebSocketDisconnect) as exc_bad:
            async with client_allow.websocket_connect(
                "/ws/allow",
                headers={"origin": bad_origin},
            ):
                pass
        assert exc_bad.value.code == 1008, f"Expected 1008 for Origin {bad_origin!r}"

    # 3. Same-origin mode rejects http:// -> wss:// downgrade while allowing https:// -> wss://
    app_same = Application(auto_discover_providers=False)
    wss_scope = {"type": "websocket", "scheme": "wss", "path": "/ws/same"}
    assert (
        app_same._is_origin_allowed("http://myapp.com:80", "myapp.com:443", wss_scope)
        is False
    )
    assert (
        app_same._is_origin_allowed("http://myapp.com", "myapp.com", wss_scope)
        is False
    )
    assert (
        app_same._is_origin_allowed("https://myapp.com", "myapp.com:443", wss_scope)
        is True
    )
    assert (
        app_same._is_origin_allowed("https://myapp.com:443", "myapp.com", wss_scope)
        is True
    )


@pytest.mark.asyncio
async def test_websocket_send_json_and_distributed_broadcast_across_workers() -> None:
    """WS-2: encode_json support, worker startup subscription to websocket.broadcast, and echo dedup."""
    ts = datetime(2026, 11, 14, 15, 0, 0, tzinfo=UTC)
    item_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    price = Decimal("42.50")

    # 1. WebSocketConnection.send_json uses encode_json (datetime, UUID, Decimal)
    sent_frames: list[dict[str, Any]] = []

    async def _recv() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def _send(msg: dict[str, Any]) -> None:
        sent_frames.append(msg)

    conn = WebSocketConnection(
        scope={"type": "websocket", "path": "/ws/json"},
        receive=_recv,
        send=_send,
    )
    await conn.accept()
    await conn.send_json({"ts": ts, "id": item_id, "price": price})
    decoded = json.loads(sent_frames[-1]["text"])
    assert decoded == {
        "ts": ts.isoformat(),
        "id": str(item_id),
        "price": "42.50",
    }

    # 2. FrameworkServiceProvider.startup subscribes WebSocketManager to RedisEventBus with worker dedup
    shared_dispatcher = EventDispatcher()
    app_worker_1 = Application(auto_discover_providers=False)
    app_worker_2 = Application(auto_discover_providers=False)

    class _LoopbackBus(RedisEventBus):
        def __init__(self, dispatcher: EventDispatcher) -> None:
            super().__init__(dispatcher=dispatcher, ignore_self=False)
            self.published: list[tuple[str, Any]] = []

        async def publish(
            self, event: str, payload: Any = None, *, local: bool = False
        ) -> int:
            serialized = self._serialize_value(payload)
            self.published.append((event, serialized))
            await self.dispatcher.dispatch(event, serialized)
            return 1

    bus1 = _LoopbackBus(shared_dispatcher)
    bus2 = _LoopbackBus(shared_dispatcher)
    app_worker_1.container.instance(RedisEventBus, bus1)
    app_worker_2.container.instance(RedisEventBus, bus2)

    try:
        await app_worker_1.startup()
        await app_worker_2.startup()

        c1 = _RecordingConnection("w1-client")
        c2 = _RecordingConnection("w2-client")
        await app_worker_1.websocket_manager.connect(c1, room="updates")
        await app_worker_2.websocket_manager.connect(c2, room="updates")

        rich_payload = {"ts": ts, "id": item_id, "price": price}
        await app_worker_1.websocket_manager.broadcast(
            rich_payload, room="updates", distribute=True
        )

        # c1 received local broadcast exactly once (no self-echo loop!)
        assert len(c1.received_messages) == 1
        assert json.loads(str(c1.received_messages[0])) == {
            "ts": ts.isoformat(),
            "id": str(item_id),
            "price": "42.50",
        }

        # c2 received cross-worker broadcast exactly once
        assert len(c2.received_messages) == 1
        assert json.loads(str(c2.received_messages[0])) == {
            "ts": ts.isoformat(),
            "id": str(item_id),
            "price": "42.50",
        }
    finally:
        await app_worker_1.shutdown()
        await app_worker_2.shutdown()


@pytest.mark.asyncio
async def test_websocket_idle_timeout_config_and_cached_utf8_properties() -> None:
    """WS-3: Forward websockets.idle_timeout, decode query_string as UTF-8, and cache headers/query_params."""
    async def _noop_receive() -> dict[str, Any]:
        return {"type": "websocket.disconnect"}

    async def _noop_send(_msg: dict[str, Any]) -> None:
        return None

    # 1. Cached headers & UTF-8 query_params / query_param_list
    raw_conn = WebSocketConnection(
        scope={
            "type": "websocket",
            "path": "/ws/meta",
            "headers": [(b"X-Custom", b"val1"), (b"Host", b"localhost")],
            "query_string": b"city=M%C3%BCnchen&raw=caf\xc3\xa9&bad=\xff",
        },
        receive=_noop_receive,
        send=_noop_send,
    )
    h1 = raw_conn.headers
    h2 = raw_conn.headers
    assert h1 is h2
    assert h1 == {"x-custom": "val1", "host": "localhost"}

    qp1 = raw_conn.query_params
    qp2 = raw_conn.query_params
    qpl1 = raw_conn.query_param_list
    qpl2 = raw_conn.query_param_list
    assert qp1 is qp2
    assert qpl1 is qpl2
    assert qp1["city"] == "München"
    assert qp1["raw"] == "café"
    assert "\ufffd" in qp1["bad"]

    # 2. Application forwards websockets.idle_timeout and disconnects idle clients cleanly
    observed_timeouts: list[float | None] = []
    close_codes: list[int] = []

    app = Application(
        auto_discover_providers=False,
        config={"websockets": {"idle_timeout": 0.05}},
    )

    @app.websocket("/ws/idle")
    async def idle_ws(ws: WebSocketConnection) -> None:
        observed_timeouts.append(ws.idle_timeout)
        await ws.accept()
        try:
            await ws.receive()
        except WebSocketDisconnect as exc:
            close_codes.append(exc.code)
            raise

    scope: dict[str, Any] = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": "/ws/idle",
        "headers": [(b"host", b"127.0.0.1:80")],
    }
    sent: list[dict[str, Any]] = []
    connect_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal connect_sent
        if not connect_sent:
            connect_sent = True
            return {"type": "websocket.connect"}
        await asyncio.sleep(1.0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await app(scope, receive, send)
    assert observed_timeouts == [0.05]
    assert close_codes == [1000]
    assert any(
        m.get("type") == "websocket.close" and m.get("reason") == "Idle timeout"
        for m in sent
    )
