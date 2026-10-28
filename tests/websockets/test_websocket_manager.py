from __future__ import annotations

import asyncio
from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.helpers.helper import websocket_manager
from swiftpy.testing.http_client import TestClient
from swiftpy.websockets.connection import (
    WebSocketConnection,
    WebSocketDisconnect,
    WebSocketState,
)
from swiftpy.websockets.manager import WebSocketManager


@pytest.mark.asyncio
async def test_manager_connect_disconnect_and_active_count() -> None:
    manager = WebSocketManager()
    assert manager.active_count == 0

    app = Application()

    @app.websocket("/ws/track")
    async def track(ws: WebSocketConnection) -> None:
        await ws.accept()
        await manager.connect(ws)
        assert manager.is_connected(ws)
        assert manager.active_count == 1
        await ws.receive_text()
        await manager.disconnect(ws)
        assert not manager.is_connected(ws)

    client = TestClient(app)
    async with client.websocket_connect("/ws/track") as ws:
        await ws.send_text("leave")

    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_manager_broadcast_to_all() -> None:
    manager = WebSocketManager()
    app = Application()

    @app.websocket("/ws/pubsub")
    async def pubsub(ws: WebSocketConnection) -> None:
        await ws.accept()
        await manager.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            await manager.disconnect(ws)

    client1 = TestClient(app)
    client2 = TestClient(app)

    async with (
        client1.websocket_connect("/ws/pubsub") as ws1,
        client2.websocket_connect("/ws/pubsub") as ws2,
    ):
        assert manager.active_count == 2
        await manager.broadcast("Broadcast to all!")

        msg1 = await ws1.receive_text()
        msg2 = await ws2.receive_text()

        assert msg1 == "Broadcast to all!"
        assert msg2 == "Broadcast to all!"


@pytest.mark.asyncio
async def test_manager_room_broadcast_isolation() -> None:
    manager = WebSocketManager()
    app = Application()

    @app.websocket("/ws/rooms/{room_name}")
    async def room_handler(ws: WebSocketConnection, room_name: str) -> None:
        await ws.accept()
        await manager.connect(ws, room=room_name)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            await manager.disconnect(ws, room=room_name)

    client = TestClient(app)

    async with (
        client.websocket_connect("/ws/rooms/sports") as ws_sports1,
        client.websocket_connect("/ws/rooms/sports") as ws_sports2,
        client.websocket_connect("/ws/rooms/finance") as ws_finance,
    ):
        assert len(manager.get_connections("sports")) == 2
        assert len(manager.get_connections("finance")) == 1
        assert manager.active_count == 3

        # Broadcast to sports only
        await manager.broadcast({"type": "goal", "score": "1-0"}, room="sports")

        msg_s1 = await ws_sports1.receive_json()
        msg_s2 = await ws_sports2.receive_json()
        assert msg_s1 == {"type": "goal", "score": "1-0"}
        assert msg_s2 == {"type": "goal", "score": "1-0"}

        # Broadcast to finance only
        await manager.broadcast({"type": "stock", "ticker": "AAPL"}, room="finance")
        msg_f = await ws_finance.receive_json()
        assert msg_f == {"type": "stock", "ticker": "AAPL"}


@pytest.mark.asyncio
async def test_manager_broadcast_with_custom_filter() -> None:
    manager = WebSocketManager()
    app = Application()

    @app.websocket("/ws/filtered")
    async def filter_handler(ws: WebSocketConnection) -> None:
        await ws.accept()
        role = ws.query_params.get("role", "guest")
        ws.state["role"] = role
        await manager.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            await manager.disconnect(ws)

    client = TestClient(app)

    async with (
        client.websocket_connect("/ws/filtered?role=admin") as ws_admin,
        client.websocket_connect("/ws/filtered?role=guest") as ws_guest,
    ):
        # Broadcast only to admins using filter_fn predicate
        await manager.broadcast(
            "Admin Alert",
            filter_fn=lambda c: c.state.get("role") == "admin",
        )

        admin_msg = await ws_admin.receive_text()
        assert admin_msg == "Admin Alert"

        # Broadcast to guests
        await manager.broadcast(
            "Welcome Guest",
            filter_fn=lambda c: c.state.get("role") == "guest",
        )
        guest_msg = await ws_guest.receive_text()
        assert guest_msg == "Welcome Guest"


@pytest.mark.asyncio
async def test_manager_auto_cleans_up_failed_connections_during_broadcast() -> None:
    manager = WebSocketManager()
    app = Application()

    @app.websocket("/ws/long_lived")
    async def long_lived(ws: WebSocketConnection) -> None:
        await ws.accept()
        await manager.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            await manager.disconnect(ws)

    # Simulate a dead/disconnected connection already in manager
    scope: dict[str, Any] = {"type": "websocket", "path": "/fake"}
    async def fake_receive() -> dict[str, Any]:
        return {}
    async def fake_send(msg: dict[str, Any]) -> None:
        pass

    dead_conn = WebSocketConnection(scope, fake_receive, fake_send)
    # Marked as disconnected
    dead_conn.client_state = WebSocketState.DISCONNECTED
    await manager.connect(dead_conn)
    assert manager.is_connected(dead_conn)

    client = TestClient(app)
    async with client.websocket_connect("/ws/long_lived") as ws_live:
        assert manager.active_count == 2

        # Broadcast should succeed for the alive connection and clean up dead ones
        await manager.broadcast("Ping!")
        msg = await ws_live.receive_text()
        assert msg == "Ping!"

        # The dead connection should now be automatically pruned
        assert not manager.is_connected(dead_conn)
        assert manager.active_count == 1

    # After live connection disconnects, active count is 0
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_manager_di_container_and_global_helper() -> None:
    WebSocketManager.reset_instance()
    app = Application()

    resolved_manager = app.container.resolve(WebSocketManager)
    assert resolved_manager is not None
    assert app.websocket_manager is resolved_manager
    assert websocket_manager() is resolved_manager

    WebSocketManager.reset_instance()


@pytest.mark.asyncio
async def test_manager_broadcast_pruning_on_send_failure() -> None:
    """
    Verify that when conn.send() raises an exception during broadcast,
    the healthy connection still receives the message, and the failed connection
    is automatically pruned from the manager and its room.
    """
    manager = WebSocketManager()
    healthy_received: list[str] = []

    scope: dict[str, Any] = {"type": "websocket", "path": "/test"}

    async def fake_receive() -> dict[str, Any]:
        return {}

    async def healthy_send(msg: dict[str, Any]) -> None:
        if "text" in msg:
            healthy_received.append(msg["text"])

    async def failing_send(msg: dict[str, Any]) -> None:
        raise ConnectionResetError("Remote client crashed during transmission")

    healthy_conn = WebSocketConnection(scope, fake_receive, healthy_send)
    healthy_conn.client_state = WebSocketState.CONNECTED

    failing_conn = WebSocketConnection(scope, fake_receive, failing_send)
    failing_conn.client_state = WebSocketState.CONNECTED

    # Connect both to room 'vip'
    await manager.connect(healthy_conn, room="vip")
    await manager.connect(failing_conn, room="vip")

    assert manager.active_count == 2
    assert len(manager.get_connections("vip")) == 2

    # Broadcast to room 'vip'
    await manager.broadcast("Alert: System update", room="vip")

    # 1. Healthy connection received message
    assert healthy_received == ["Alert: System update"]

    # 2. Failing connection was pruned from manager and room
    assert not manager.is_connected(failing_conn)
    assert manager.is_connected(healthy_conn)
    assert manager.active_count == 1
    assert manager.get_connections("vip") == [healthy_conn]


@pytest.mark.asyncio
async def test_manager_simultaneous_disconnect_and_broadcast() -> None:
    """
    Verify that concurrent disconnects while broadcast is running
    execute safely without race conditions or deadlocks.
    """
    manager = WebSocketManager()
    scope: dict[str, Any] = {"type": "websocket", "path": "/race"}

    async def fake_receive() -> dict[str, Any]:
        return {}

    async def slow_send(msg: dict[str, Any]) -> None:
        await asyncio.sleep(0.01)

    conns = [
        WebSocketConnection(scope, fake_receive, slow_send)
        for _ in range(20)
    ]
    for c in conns:
        c.client_state = WebSocketState.CONNECTED
        await manager.connect(c, room="race_room")

    assert manager.active_count == 20

    async def disconnector() -> None:
        for c in conns[:10]:
            await asyncio.sleep(0.002)
            await manager.disconnect(c)

    async def broadcaster() -> None:
        for _ in range(5):
            await manager.broadcast("ping", room="race_room")
            await asyncio.sleep(0.003)

    await asyncio.gather(disconnector(), broadcaster())

    assert manager.active_count == 10
    assert len(manager.get_connections("race_room")) == 10


@pytest.mark.asyncio
async def test_manager_multi_room_membership_and_repeated_disconnect() -> None:
    """
    Verify joining and leaving multiple rooms, repeated connect/disconnect,
    and ensuring all rooms are cleaned up on final disconnect.
    """
    manager = WebSocketManager()
    scope: dict[str, Any] = {"type": "websocket", "path": "/multi"}

    async def dummy_receive() -> dict[str, Any]:
        return {}

    async def dummy_send(msg: dict[str, Any]) -> None:
        pass

    conn = WebSocketConnection(scope, dummy_receive, dummy_send)
    conn.client_state = WebSocketState.CONNECTED

    # Connect to room1
    await manager.connect(conn, room="room1")
    # Repeated connect should be idempotent
    await manager.connect(conn, room="room1")
    assert manager.active_count == 1
    assert len(manager.get_connections("room1")) == 1

    # Join room2 and room3
    await manager.join_room("room2", conn)
    await manager.join_room("room3", conn)
    assert len(manager.get_connections("room1")) == 1
    assert len(manager.get_connections("room2")) == 1
    assert len(manager.get_connections("room3")) == 1

    # Leave room2 only (connection remains in room1 and room3)
    await manager.leave_room("room2", conn)
    assert len(manager.get_connections("room2")) == 0
    assert len(manager.get_connections("room1")) == 1
    assert len(manager.get_connections("room3")) == 1
    assert manager.is_connected(conn)

    # Disconnect should remove from all remaining rooms
    await manager.disconnect(conn)
    assert not manager.is_connected(conn)
    assert manager.active_count == 0
    assert len(manager.get_connections("room1")) == 0
    assert len(manager.get_connections("room3")) == 0

    # Repeated disconnect should be idempotent
    await manager.disconnect(conn)
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_manager_filter_fn_exception_isolated() -> None:
    """
    Verify that an exception raised by filter_fn on one connection
    does not abort broadcasting to other valid connections.
    """
    manager = WebSocketManager()
    received: list[str] = []

    scope: dict[str, Any] = {"type": "websocket", "path": "/filter"}

    async def dummy_receive() -> dict[str, Any]:
        return {}

    async def send_ok(msg: dict[str, Any]) -> None:
        if "text" in msg:
            received.append(msg["text"])

    conn_error = WebSocketConnection(scope, dummy_receive, send_ok)
    conn_error.client_state = WebSocketState.CONNECTED
    conn_error.state["fail"] = True

    conn_ok = WebSocketConnection(scope, dummy_receive, send_ok)
    conn_ok.client_state = WebSocketState.CONNECTED
    conn_ok.state["fail"] = False

    await manager.connect(conn_error)
    await manager.connect(conn_ok)

    def faulty_filter(c: WebSocketConnection) -> bool:
        if c.state.get("fail"):
            raise ValueError("Filter evaluation failed intentionally")
        return True

    # Broadcast should isolate the filter error and deliver to conn_ok
    await manager.broadcast("Filtered Hello", filter_fn=faulty_filter)
    assert received == ["Filtered Hello"]


@pytest.mark.asyncio
async def test_manager_broadcast_empty_or_nonexistent_room() -> None:
    """
    Verify broadcasting when there are zero connections or to a room
    that does not exist returns smoothly without error.
    """
    manager = WebSocketManager()
    assert manager.active_count == 0

    # Broadcast to empty manager
    await manager.broadcast("hello")

    # Broadcast to non-existent room
    await manager.broadcast("hello", room="nonexistent_room")
    assert manager.get_connections("nonexistent_room") == []
