from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swiftpy.core.container import Container, set_container
from swiftpy.events.redis_bus import RedisEventBus
from swiftpy.websockets.connection import (
    WebSocketConnection,
    WebSocketDisconnect,
    WebSocketState,
)
from swiftpy.websockets.manager import WebSocketManager


class MockConnection(WebSocketConnection):
    """A lightweight mock WebSocket connection for broadcast tests."""

    def __init__(
        self,
        conn_id: str,
        *,
        delay: float = 0.0,
        fail: bool = False,
    ) -> None:
        self.conn_id = conn_id
        self.delay = delay
        self.fail = fail
        self.received_messages: list[str | bytes] = []
        self.client_state = WebSocketState.CONNECTED
        self.close_code: int | None = None
        self.close_reason: str = ""
        self.state: dict[str, Any] = {}
        self.scope: dict[str, Any] = {"type": "websocket", "path": "/ws"}

    async def send_text(self, text: str) -> None:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise WebSocketDisconnect(code=1006, reason="Connection dropped")
        self.received_messages.append(text)

    async def send_bytes(self, data: bytes) -> None:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise WebSocketDisconnect(code=1006, reason="Connection dropped")
        self.received_messages.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.client_state = WebSocketState.DISCONNECTED
        self.close_code = code
        self.close_reason = reason


@pytest.mark.asyncio
async def test_broadcast_serializes_json_once() -> None:
    """
    Broadcasting a dictionary or list payload must call json.dumps() exactly once,
    not once per recipient (P9-W3).
    """
    manager = WebSocketManager()
    connections = [MockConnection(f"client_{i}") for i in range(10)]
    for conn in connections:
        await manager.connect(conn)

    assert manager.active_count == 10

    payload = {"type": "news_flash", "id": 12345, "headline": "Breaking news"}

    with patch("swiftpy.websockets.manager.json.dumps", side_effect=json.dumps) as mock_dumps:
        await manager.broadcast(payload)
        assert mock_dumps.call_count == 1

    # Verify all 10 recipients received identical JSON text
    expected_text = json.dumps(payload)
    for conn in connections:
        assert len(conn.received_messages) == 1
        assert conn.received_messages[0] == expected_text


@pytest.mark.asyncio
async def test_stalled_client_does_not_block_fast_clients() -> None:
    """
    A slow or hung client must not delay delivery to other fast clients.
    The stalled client is cancelled after timeout and evicted from the manager.
    """
    manager = WebSocketManager()

    fast_client_1 = MockConnection("fast_1", delay=0.0)
    stalled_client = MockConnection("stalled", delay=2.0)  # delays 2.0s
    fast_client_2 = MockConnection("fast_2", delay=0.0)

    await manager.connect(fast_client_1)
    await manager.connect(stalled_client)
    await manager.connect(fast_client_2)

    assert manager.active_count == 3

    start_time = asyncio.get_running_loop().time()
    # timeout=0.1s ensures stalled client is cut off quickly
    await manager.broadcast("ping", timeout=0.1)
    elapsed = asyncio.get_running_loop().time() - start_time

    # Broadcast should have finished in ~0.1 - 0.3s, well under 2.0s
    assert elapsed < 1.0

    # Fast clients received the message
    assert fast_client_1.received_messages == ["ping"]
    assert fast_client_2.received_messages == ["ping"]
    assert stalled_client.received_messages == []

    # Stalled client was evicted from the manager
    assert not manager.is_connected(stalled_client)
    assert manager.is_connected(fast_client_1)
    assert manager.is_connected(fast_client_2)
    assert manager.active_count == 2


@pytest.mark.asyncio
async def test_failed_client_eviction_during_broadcast() -> None:
    """
    A connection that raises WebSocketDisconnect during send must be cleanly evicted
    without interrupting broadcast to other active connections.
    """
    manager = WebSocketManager()

    good_client = MockConnection("good")
    broken_client = MockConnection("broken", fail=True)

    await manager.connect(good_client, room="lobby")
    await manager.connect(broken_client, room="lobby")

    assert manager.active_count == 2
    assert len(manager.get_connections(room="lobby")) == 2

    await manager.broadcast("announcement", room="lobby")

    assert good_client.received_messages == ["announcement"]
    assert not manager.is_connected(broken_client)
    assert manager.is_connected(good_client)
    assert manager.active_count == 1
    assert manager.get_connections(room="lobby") == [good_client]


@pytest.mark.asyncio
async def test_bounded_concurrency_semaphore() -> None:
    """
    Broadcasting with max_concurrency limits simultaneous in-flight send operations.
    """
    manager = WebSocketManager()
    max_simultaneous = 0
    current_simultaneous = 0

    class ConcurrencyTrackingConnection(MockConnection):
        async def send_text(self, text: str) -> None:
            nonlocal current_simultaneous, max_simultaneous
            current_simultaneous += 1
            max_simultaneous = max(max_simultaneous, current_simultaneous)
            await asyncio.sleep(0.02)
            current_simultaneous -= 1
            self.received_messages.append(text)

    conns = [ConcurrencyTrackingConnection(f"client_{i}") for i in range(12)]
    for c in conns:
        await manager.connect(c)

    # Set max_concurrency to 3
    await manager.broadcast("throttled", max_concurrency=3)

    assert max_simultaneous <= 3
    for c in conns:
        assert c.received_messages == ["throttled"]


@pytest.mark.asyncio
async def test_distributed_broadcast_publishes_to_event_bus() -> None:
    """
    When distribute=True, broadcast publishes message to RedisEventBus with local=False.
    """
    container = Container()
    set_container(container)

    mock_bus = MagicMock(spec=RedisEventBus)
    mock_bus.publish = AsyncMock()
    container.instance(RedisEventBus, mock_bus)

    manager = WebSocketManager()
    local_client = MockConnection("local")
    await manager.connect(local_client, room="chat")

    await manager.broadcast(
        {"action": "chat_message", "text": "hello distributed"},
        room="chat",
        distribute=True,
    )

    # Local recipient received it
    assert len(local_client.received_messages) == 1

    # Bus received publish call
    mock_bus.publish.assert_awaited_once_with(
        "websocket.broadcast",
        {
            "message": {"action": "chat_message", "text": "hello distributed"},
            "room": "chat",
        },
        local=False,
    )
