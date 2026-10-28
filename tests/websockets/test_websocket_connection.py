from __future__ import annotations

from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.testing.http_client import TestClient
from swiftpy.websockets.connection import (
    WebSocket,
    WebSocketConnection,
    WebSocketDisconnect,
    WebSocketException,
    WebSocketState,
)


@pytest.mark.asyncio
async def test_websocket_accept_send_and_receive_text() -> None:
    app = Application()

    @app.websocket("/ws/echo")
    async def echo(ws: WebSocketConnection) -> None:
        await ws.accept()
        message = await ws.receive_text()
        await ws.send_text(f"Echo: {message}")

    client = TestClient(app)
    async with client.websocket_connect("/ws/echo") as ws:
        await ws.send_text("Hello SwiftPY!")
        response = await ws.receive_text()
        assert response == "Echo: Hello SwiftPY!"


@pytest.mark.asyncio
async def test_websocket_send_and_receive_json() -> None:
    app = Application()

    @app.websocket("/ws/json")
    async def json_handler(ws: WebSocket) -> None:
        await ws.accept()
        data = await ws.receive_json()
        await ws.send_json({"received": data, "status": "ok"})

    client = TestClient(app)
    async with client.websocket_connect("/ws/json") as ws:
        await ws.send_json({"action": "ping", "id": 123})
        resp = await ws.receive_json()
        assert resp == {"received": {"action": "ping", "id": 123}, "status": "ok"}


@pytest.mark.asyncio
async def test_websocket_send_and_receive_bytes() -> None:
    app = Application()

    @app.websocket("/ws/binary")
    async def binary_handler(ws: WebSocketConnection) -> None:
        await ws.accept()
        data = await ws.receive_bytes()
        await ws.send_bytes(data + b"-pong")

    client = TestClient(app)
    async with client.websocket_connect("/ws/binary") as ws:
        await ws.send_bytes(b"ping")
        resp = await ws.receive_bytes()
        assert resp == b"ping-pong"


@pytest.mark.asyncio
async def test_websocket_path_parameters() -> None:
    app = Application()
    received_rooms: list[str] = []

    @app.websocket("/ws/chat/{room_id}")
    async def chat(ws: WebSocketConnection, room_id: str) -> None:
        await ws.accept()
        received_rooms.append(room_id)
        msg = await ws.receive_text()
        await ws.send_text(f"Room {room_id}: {msg}")

    client = TestClient(app)
    async with client.websocket_connect("/ws/chat/lobby-99") as ws:
        await ws.send_text("hi")
        resp = await ws.receive_text()
        assert resp == "Room lobby-99: hi"

    assert received_rooms == ["lobby-99"]


@pytest.mark.asyncio
async def test_websocket_path_parameter_coercion() -> None:
    app = Application()
    received_ids: list[int] = []

    @app.websocket("/ws/users/{user_id}")
    async def user_ws(ws: WebSocketConnection, user_id: int) -> None:
        await ws.accept()
        received_ids.append(user_id)
        await ws.send_json({"user_id": user_id})

    client = TestClient(app)
    async with client.websocket_connect("/ws/users/42") as ws:
        resp = await ws.receive_json()
        assert resp == {"user_id": 42}
    assert received_ids == [42]

    # Test invalid coercion closes connection
    with pytest.raises(WebSocketDisconnect) as exc_info:
        async with client.websocket_connect("/ws/users/not-an-int"):
            pass
    assert exc_info.value.code == 1002


@pytest.mark.asyncio
async def test_websocket_headers_query_params_and_properties() -> None:
    app = Application()
    inspected_props: dict[str, Any] = {}

    @app.websocket("/ws/inspect")
    async def inspect_handler(ws: WebSocketConnection) -> None:
        inspected_props["path"] = ws.path
        inspected_props["query_token"] = ws.query_params.get("token")
        inspected_props["header_auth"] = ws.headers.get("x-auth")
        inspected_props["is_connected_before"] = ws.is_connected
        await ws.accept()
        inspected_props["is_connected_after"] = ws.is_connected
        ws.state["custom"] = "value_123"
        inspected_props["custom_state"] = ws.state["custom"]
        await ws.send_text("ready")

    client = TestClient(app)
    async with client.websocket_connect(
        "/ws/inspect?token=secret123&flag=true",
        headers={"x-auth": "bearer-test"},
    ) as ws:
        msg = await ws.receive_text()
        assert msg == "ready"

    assert inspected_props["path"] == "/ws/inspect"
    assert inspected_props["query_token"] == "secret123"
    assert inspected_props["header_auth"] == "bearer-test"
    assert inspected_props["is_connected_before"] is False
    assert inspected_props["is_connected_after"] is True
    assert inspected_props["custom_state"] == "value_123"


@pytest.mark.asyncio
async def test_websocket_not_found_closes_connection() -> None:
    app = Application()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        async with client.websocket_connect("/ws/nonexistent"):
            pass
    assert exc_info.value.code == 1000


@pytest.mark.asyncio
async def test_websocket_unhandled_handler_exception_closes_with_1011() -> None:
    app = Application()

    @app.websocket("/ws/crash")
    async def crash_handler(ws: WebSocketConnection) -> None:
        await ws.accept()
        raise RuntimeError("Something exploded!")

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        async with client.websocket_connect("/ws/crash") as ws:
            await ws.receive_text()
    assert exc_info.value.code == 1011


@pytest.mark.asyncio
async def test_websocket_client_disconnect_raises_in_handler() -> None:
    app = Application()
    disconnect_caught = False

    @app.websocket("/ws/disconnect")
    async def handle_disconnect(ws: WebSocketConnection) -> None:
        nonlocal disconnect_caught
        await ws.accept()
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect as exc:
            disconnect_caught = True
            assert exc.code == 1000

    client = TestClient(app)
    async with client.websocket_connect("/ws/disconnect") as ws:
        await ws.send_text("msg 1")
        # Exit context block closes the client side connection

    assert disconnect_caught is True


@pytest.mark.asyncio
async def test_websocket_send_and_receive_before_accept_raises_error() -> None:
    scope: dict[str, Any] = {"type": "websocket", "path": "/test"}
    received_messages: list[dict[str, Any]] = []

    async def fake_receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def fake_send(msg: dict[str, Any]) -> None:
        received_messages.append(msg)

    conn = WebSocketConnection(scope, fake_receive, fake_send)
    assert conn.client_state == WebSocketState.CONNECTING

    with pytest.raises(WebSocketException, match="has not been accepted"):
        await conn.receive()

    with pytest.raises(WebSocketException, match="is not connected"):
        await conn.send("cannot send yet")

    # Now accept
    await conn.accept()
    assert conn.is_connected
    assert received_messages == [{"type": "websocket.accept"}]

    # Close
    await conn.close(code=1000, reason="Normal Closure")
    assert not conn.is_connected
    assert conn.close_code == 1000

    # Sending after close raises
    with pytest.raises(WebSocketException, match="is not connected"):
        await conn.send("cannot send after close")


@pytest.mark.asyncio
async def test_websocket_accept_with_subprotocols_and_headers() -> None:
    sent_msgs: list[dict[str, Any]] = []

    async def fake_receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def fake_send(msg: dict[str, Any]) -> None:
        sent_msgs.append(msg)

    scope: dict[str, Any] = {
        "type": "websocket",
        "path": "/ws",
        "subprotocols": ["graphql-ws", "chat.v1"],
    }
    conn = WebSocketConnection(scope, fake_receive, fake_send)
    assert conn.subprotocols == ["graphql-ws", "chat.v1"]

    await conn.accept(
        subprotocol="chat.v1",
        headers=[(b"sec-websocket-protocol", b"chat.v1"), (b"x-server", b"swiftpy")],
    )
    assert conn.is_connected
    assert sent_msgs == [
        {
            "type": "websocket.accept",
            "subprotocol": "chat.v1",
            "headers": [
                (b"sec-websocket-protocol", b"chat.v1"),
                (b"x-server", b"swiftpy"),
            ],
        }
    ]

    # Repeated accept raises WebSocketException
    with pytest.raises(WebSocketException, match="already accepted"):
        await conn.accept()


@pytest.mark.asyncio
async def test_websocket_close_before_accept_and_repeated_close() -> None:
    sent_msgs: list[dict[str, Any]] = []

    async def fake_receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def fake_send(msg: dict[str, Any]) -> None:
        sent_msgs.append(msg)

    scope: dict[str, Any] = {"type": "websocket", "path": "/ws"}
    conn = WebSocketConnection(scope, fake_receive, fake_send)

    # Close before accept (rejection)
    await conn.close(code=1008, reason="Unauthorized")
    assert conn.client_state == WebSocketState.DISCONNECTED
    assert conn.close_code == 1008
    assert conn.close_reason == "Unauthorized"
    assert sent_msgs == [{"type": "websocket.close", "code": 1008, "reason": "Unauthorized"}]

    # Repeated close is idempotent and does not send duplicate frames
    await conn.close(code=1008)
    assert len(sent_msgs) == 1

    # Calling accept on closed connection raises
    with pytest.raises(WebSocketException, match="closed"):
        await conn.accept()


@pytest.mark.asyncio
async def test_websocket_accept_when_client_disconnected_or_unexpected() -> None:
    # 1. Client disconnects before accept
    async def disconnect_receive() -> dict[str, Any]:
        return {"type": "websocket.disconnect", "code": 1001, "reason": "Going Away"}

    async def noop_send(msg: dict[str, Any]) -> None:
        pass

    scope: dict[str, Any] = {"type": "websocket", "path": "/ws"}
    conn = WebSocketConnection(scope, disconnect_receive, noop_send)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        await conn.accept()
    assert exc_info.value.code == 1001
    assert exc_info.value.reason == "Going Away"
    assert conn.client_state == WebSocketState.DISCONNECTED

    # 2. Unexpected ASGI message type during accept
    async def unexpected_receive() -> dict[str, Any]:
        return {"type": "websocket.receive", "text": "early data"}

    conn_unexpected = WebSocketConnection(scope, unexpected_receive, noop_send)
    with pytest.raises(WebSocketException, match="Expected 'websocket.connect'"):
        await conn_unexpected.accept()


@pytest.mark.asyncio
async def test_websocket_type_mismatches_and_malformed_json() -> None:
    # Set up connection that yields binary data
    msg_queue: list[dict[str, Any]] = [
        {"type": "websocket.receive", "bytes": b"\x00\x01\x02"},
        {"type": "websocket.receive", "text": "not-valid-json{{{"},
        {"type": "websocket.receive", "text": "hello string"},
    ]

    async def fake_receive() -> dict[str, Any]:
        return msg_queue.pop(0)

    async def noop_send(msg: dict[str, Any]) -> None:
        pass

    scope: dict[str, Any] = {"type": "websocket", "path": "/ws"}
    conn = WebSocketConnection(scope, fake_receive, noop_send)
    conn.client_state = WebSocketState.CONNECTED

    # 1. receive_text() receives binary data -> raises WebSocketException
    with pytest.raises(WebSocketException, match="Expected text frame"):
        await conn.receive_text()

    # 2. receive_json() with malformed JSON text -> raises WebSocketException
    with pytest.raises(WebSocketException, match="Malformed JSON received"):
        await conn.receive_json()

    # 3. receive_bytes() receives text frame -> raises WebSocketException
    with pytest.raises(WebSocketException, match="Expected binary frame"):
        await conn.receive_bytes()


@pytest.mark.asyncio
async def test_websocket_repeated_query_params_and_headers() -> None:
    scope: dict[str, Any] = {
        "type": "websocket",
        "path": "/ws/query",
        "query_string": b"tag=python&tag=asgi&category=web",
        "headers": [
            (b"X-Custom-Header", b"Value1"),
            (b"x-trace-id", b"trace-999"),
        ],
        "client": ("192.168.1.100", 44332),
    }

    async def noop_receive() -> dict[str, Any]:
        return {}

    async def noop_send(msg: dict[str, Any]) -> None:
        pass

    conn = WebSocketConnection(scope, noop_receive, noop_send)

    # First value
    assert conn.query_params["tag"] == "python"
    assert conn.query_params["category"] == "web"

    # All values
    assert conn.query_param_list["tag"] == ["python", "asgi"]
    assert conn.query_param_list["category"] == ["web"]

    # Headers normalized to lowercase keys
    assert conn.headers["x-custom-header"] == "Value1"
    assert conn.headers["x-trace-id"] == "trace-999"

    # Client tuple
    assert conn.client == ("192.168.1.100", 44332)
