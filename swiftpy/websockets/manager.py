from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from swiftpy.core.container import get_container
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
    ) -> None:
        """
        Broadcast a message to active connections.
        - If `room` is provided, delivers only to connections in that room.
        - If `filter_fn` is provided, delivers only to connections matching the predicate.
        - If neither is provided, delivers to all active connections.
        Failed or disconnected connections are automatically cleaned up.
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

        if not recipients:
            return

        async def _safe_send(conn: WebSocketConnection) -> tuple[WebSocketConnection, Exception | None]:
            try:
                await conn.send(message)
                return conn, None
            except (WebSocketDisconnect, WebSocketException) as exc:
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


__all__ = ["WebSocketManager"]
