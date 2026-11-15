from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Any
from weakref import WeakSet

from swiftpy.core.container import get_container
from swiftpy.events.redis_bus import RedisEventBus
from swiftpy.http.response import encode_json
from swiftpy.websockets.connection import (
    WebSocketConnection,
    WebSocketDisconnect,
    WebSocketException,
)

logger = logging.getLogger("swiftpy.websockets")


class WebSocketManager:
    """
    Manages active WebSocket connections.
    Supports rooms, filtering, and concurrent broadcast distribution.
    """

    _instance: WebSocketManager | None = None

    def __init__(self) -> None:
        self._connections: set[WebSocketConnection] = set()
        self._rooms: dict[str, set[WebSocketConnection]] = defaultdict(set)
        self._connection_rooms: dict[WebSocketConnection, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._instance_id: str = uuid.uuid4().hex
        self._subscribed_buses: WeakSet[RedisEventBus] = WeakSet()

    def subscribe_to_bus(self, bus: RedisEventBus) -> None:
        """Subscribe this manager to distributed WebSocket broadcasts on the given RedisEventBus."""
        if bus in self._subscribed_buses:
            return
        bus.subscribe("websocket.broadcast", self._handle_distributed_broadcast)
        self._subscribed_buses.add(bus)

    def unsubscribe_from_bus(self, bus: RedisEventBus) -> None:
        """Unsubscribe this manager from distributed WebSocket broadcasts on the given RedisEventBus."""
        if bus in self._subscribed_buses:
            bus.unsubscribe("websocket.broadcast", self._handle_distributed_broadcast)
            self._subscribed_buses.discard(bus)

    async def _handle_distributed_broadcast(self, payload: Any) -> None:
        """Deliver a cross-worker broadcast event locally while ignoring messages sent by this worker."""
        if not isinstance(payload, dict):
            return
        if payload.get("sender_id") == self._instance_id:
            return
        if "message" not in payload:
            return
        room = payload.get("room")
        await self.broadcast(
            payload["message"],
            room=room if isinstance(room, str) or room is None else str(room),
            distribute=False,
        )

    @classmethod
    def get_instance(cls) -> WebSocketManager:
        """Retrieve the active application's WebSocketManager from the container, or fallback singleton."""
        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
        except RuntimeError:
            pass

        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the fallback singleton instance and clear the active container's manager if present."""
        cls._instance = None
        try:
            container = get_container()
            if container.has(cls):
                fresh = cls()
                container.instance(cls, fresh)
        except RuntimeError:
            pass

    @property
    def active_count(self) -> int:
        """The total number of currently active connections."""
        return len(self._connections)

    def is_connected(self, connection: WebSocketConnection) -> bool:
        """Check if a specific connection is actively registered."""
        return connection in self._connections

    def get_connections(self, room: str | None = None) -> list[WebSocketConnection]:
        """
        Get a list of active connections.
        If a room is specified, returns active connections in that room.
        """
        if room is not None:
            return list(self._rooms.get(room, set()))
        return list(self._connections)

    async def connect(
        self,
        connection: WebSocketConnection,
        room: str | None = None,
    ) -> None:
        """
        Register an active WebSocket connection.
        Optionally assign the connection to a room.
        """
        async with self._lock:
            self._connections.add(connection)
            if room is not None:
                self._rooms[room].add(connection)
                self._connection_rooms[connection].add(room)

    async def disconnect(
        self,
        connection: WebSocketConnection,
        room: str | None = None,
    ) -> None:
        """
        Unregister a WebSocket connection.
        Removes the connection from active connections and all rooms.
        """
        async with self._lock:
            self._connections.discard(connection)
            if connection in self._connection_rooms:
                for r in list(self._connection_rooms[connection]):
                    if r in self._rooms:
                        self._rooms[r].discard(connection)
                        if not self._rooms[r]:
                            del self._rooms[r]
                del self._connection_rooms[connection]
            if room is not None and room in self._rooms:
                self._rooms[room].discard(connection)
                if not self._rooms[room]:
                    del self._rooms[room]

    async def join_room(self, room: str, connection: WebSocketConnection) -> None:
        """Add an already connected WebSocket to a specific room."""
        async with self._lock:
            self._connections.add(connection)
            self._rooms[room].add(connection)
            self._connection_rooms[connection].add(room)

    async def leave_room(self, room: str, connection: WebSocketConnection) -> None:
        """Remove a WebSocket from a specific room without disconnecting it."""
        async with self._lock:
            if room in self._rooms:
                self._rooms[room].discard(connection)
                if not self._rooms[room]:
                    del self._rooms[room]
            if connection in self._connection_rooms:
                self._connection_rooms[connection].discard(room)
                if not self._connection_rooms[connection]:
                    del self._connection_rooms[connection]

    async def broadcast(
        self,
        message: str | bytes | dict[str, Any] | list[Any],
        *,
        room: str | None = None,
        filter_fn: Callable[[WebSocketConnection], bool] | None = None,
        timeout: float = 5.0,
        max_concurrency: int = 100,
        distribute: bool = False,
    ) -> None:
        """
        Broadcast a message to active connections.
        - If `room` is provided, delivers only to connections in that room.
        - If `filter_fn` is provided, delivers only to connections matching the predicate.
        - If neither is provided, delivers to all active connections.
        - `timeout`: Maximum seconds to wait per connection send (prevents stalled clients from delaying others).
        - `max_concurrency`: Bounded concurrency using asyncio.Semaphore.
        - `distribute`: If True, also publishes the broadcast across workers via RedisEventBus.
        Failed, timed out, or disconnected connections are automatically cleaned up.
        """
        async with self._lock:
            if room is not None:
                candidates = list(self._rooms.get(room, set()))
            else:
                candidates = list(self._connections)

        if filter_fn is not None:
            recipients = []
            for conn in candidates:
                try:
                    if filter_fn(conn):
                        recipients.append(conn)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Error evaluating filter_fn on WebSocket connection: %s", exc)
        else:
            recipients = candidates

        # 1. Single serialization for JSON and text payloads (P9-W3, WS-2)
        prepared_text: str | None = None
        prepared_bytes: bytes | None = None
        distributed_message: Any = message

        if isinstance(message, (dict, list)):
            prepared_text = encode_json(message).decode("utf-8")
            distributed_message = json.loads(prepared_text)
        elif isinstance(message, str):
            prepared_text = message
        elif isinstance(message, bytes):
            prepared_bytes = message
        else:
            prepared_text = str(message)

        if recipients:
            sem = asyncio.Semaphore(max(1, max_concurrency))

            async def _safe_send(conn: WebSocketConnection) -> tuple[WebSocketConnection, Exception | None]:
                try:
                    async with sem:
                        if prepared_text is not None:
                            await asyncio.wait_for(conn.send_text(prepared_text), timeout=timeout)
                        elif prepared_bytes is not None:
                            await asyncio.wait_for(conn.send_bytes(prepared_bytes), timeout=timeout)
                    return conn, None
                except (WebSocketDisconnect, WebSocketException) as exc:
                    return conn, exc
                except TimeoutError as exc:
                    logger.warning(
                        "Timeout (%ss) broadcasting to WebSocket connection; evicting stalled connection",
                        timeout,
                    )
                    return conn, exc
                except Exception as exc:  # noqa: BLE001
                    # Failures on one client must not abort broadcast to remaining clients
                    logger.warning("Error broadcasting to WebSocket connection: %s", exc)
                    return conn, exc

            results = await asyncio.gather(*[_safe_send(conn) for conn in recipients])

            failed_conns = [conn for conn, exc in results if exc is not None]
            if failed_conns:
                for conn in failed_conns:
                    await self.disconnect(conn)

        # 2. Cross-worker distributed fan-out via RedisEventBus (P9-W3, P6-Q6, WS-2)
        if distribute:
            try:
                container = get_container()
                if container.has(RedisEventBus):
                    bus = container.resolve(RedisEventBus)
                    await bus.publish(
                        "websocket.broadcast",
                        {
                            "message": distributed_message,
                            "room": room,
                            "sender_id": self._instance_id,
                        },
                        local=False,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error publishing distributed WebSocket broadcast: %s", exc)


__all__ = ["WebSocketManager"]
