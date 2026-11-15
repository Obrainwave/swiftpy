from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.testing.http_client import TestClient
from swiftpy.websockets.connection import (
    WebSocketConnection,
    WebSocketDisconnect,
)
from swiftpy.websockets.manager import WebSocketManager


@pytest.mark.asyncio
async def test_handler_raising_before_accept_closes_with_1008() -> None:
    """
    If a route handler raises an exception before accept() is invoked,
    the server closes the connection with code 1008 (Policy Violation).
    """
    app = Application()

    @app.websocket("/ws/fail-before-accept")
    async def fail_before_accept(ws: WebSocketConnection) -> None:
        raise PermissionError("Access forbidden by business rule")

    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        async with client.websocket_connect("/ws/fail-before-accept"):
            pass
    assert exc_info.value.code == 1008


@pytest.mark.asyncio
async def test_handler_raising_after_accept_evicts_from_manager_in_finally() -> None:
    """
    If a connection is accepted and registered with WebSocketManager,
    an unhandled exception in the handler must trigger finally cleanup:
    the connection is evicted from WebSocketManager and closed if still open.
    """
    app = Application()
    manager = app.websocket_manager
    assert isinstance(manager, WebSocketManager)

    @app.websocket("/ws/fail-after-accept")
    async def fail_after_accept(ws: WebSocketConnection) -> None:
        await ws.accept()
        await manager.connect(ws, room="test_room")
        assert manager.is_connected(ws)
        raise RuntimeError("Something exploded in handler processing")

    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        async with client.websocket_connect("/ws/fail-after-accept") as ws:
            await ws.receive_text()
    assert exc_info.value.code == 1011

    # In finally, manager must have evicted the connection
    assert manager.active_count == 0
    assert manager.get_connections(room="test_room") == []


@pytest.mark.asyncio
async def test_oversize_text_message_closes_with_1009() -> None:
    """
    Receiving a message larger than max_message_size must close the connection
    with code 1009 (Message Too Large) and raise WebSocketDisconnect(code=1009).
    """
    app = Application(max_ws_message_size=512)

    @app.websocket("/ws/size-limit")
    async def size_ws(ws: WebSocketConnection) -> None:
        await ws.accept()
        # Should raise WebSocketDisconnect with code 1009
        await ws.receive_text()

    client = TestClient(app)

    async with client.websocket_connect("/ws/size-limit") as ws:
        # Small message succeeds
        oversize_payload = "X" * 1024  # 1024 bytes > 512 bytes limit
        await ws.send_text(oversize_payload)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_raw()
        assert exc_info.value.code == 1009


@pytest.mark.asyncio
async def test_oversize_binary_message_closes_with_1009() -> None:
    """
    Receiving binary data larger than max_message_size must close with code 1009.
    """
    app = Application(max_ws_message_size=256)

    @app.websocket("/ws/binary-size-limit")
    async def binary_size_ws(ws: WebSocketConnection) -> None:
        await ws.accept()
        await ws.receive_bytes()

    client = TestClient(app)

    async with client.websocket_connect("/ws/binary-size-limit") as ws:
        oversize_bytes = b"\x00" * 512
        await ws.send_bytes(oversize_bytes)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_raw()
        assert exc_info.value.code == 1009


@pytest.mark.asyncio
async def test_idle_timeout_closes_connection_with_1000() -> None:
    """
    A connection with idle_timeout configured must close with code 1000
    when no message is received within the timeout window.
    """
    to_app_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    scope: dict[str, Any] = {
        "type": "websocket",
        "path": "/ws/idle",
        "headers": [],
        "query_string": b"",
    }

    async def receive() -> dict[str, Any]:
        return await to_app_queue.get()

    async def send(msg: dict[str, Any]) -> None:
        await from_app_queue.put(msg)

    # Instantiate connection with 0.1s idle timeout
    conn = WebSocketConnection(scope, receive, send, idle_timeout=0.1)

    # Initial connect
    await to_app_queue.put({"type": "websocket.connect"})
    await conn.accept()
    accept_msg = await from_app_queue.get()
    assert accept_msg["type"] == "websocket.accept"

    # Now call receive() without sending any client message -> times out
    with pytest.raises(WebSocketDisconnect) as exc_info:
        await conn.receive()

    assert exc_info.value.code == 1000
    assert exc_info.value.reason == "Idle timeout"

    close_msg = await from_app_queue.get()
    assert close_msg["type"] == "websocket.close"
    assert close_msg["code"] == 1000


@pytest.mark.asyncio
async def test_client_disconnect_during_receive_is_debug_logged(caplog: pytest.LogCaptureFixture) -> None:
    """
    Client disconnection during receive is normal control flow and must NOT be logged as an error.
    """
    app = Application()

    @app.websocket("/ws/normal-disconnect")
    async def normal_ws(ws: WebSocketConnection) -> None:
        await ws.accept()
        await ws.receive_text()

    client = TestClient(app)

    with caplog.at_level(logging.DEBUG, logger="swiftpy"):
        async with client.websocket_connect("/ws/normal-disconnect") as ws:
            # Client closes connection normally
            await ws.close(code=1000, reason="Client leaving")

    # Assert no ERROR or EXCEPTION logs were emitted by swiftpy
    error_logs = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(error_logs) == 0
