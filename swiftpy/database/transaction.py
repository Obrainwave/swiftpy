from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.pool import DatabasePool


class TransactionClosedError(RuntimeError):
    """Raised when a transaction connection handle is used after the transaction block has exited."""


class TransactionConnection:
    """
    Safe handle wrapping an active transaction's asyncpg.Connection (P3-D4).

    Enforces:
      1. Explicit closed state: once the transaction block exits, the handle is closed
         and any subsequent access (e.g. from child tasks that inherited the ContextVar)
         raises TransactionClosedError instead of touching the released pool connection.
      2. Per-transaction asyncio.Lock: concurrent operations on the same transaction
         connection are serialized automatically instead of raising asyncpg.InterfaceError.
         Note: Database transactions share a single connection and are not for query parallelism.
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn: asyncpg.Connection | None = conn
        self._lock = asyncio.Lock()
        self.closed: bool = False

    def _ensure_open(self) -> asyncpg.Connection:
        if self.closed or self._conn is None:
            raise TransactionClosedError(
                "Cannot use transaction connection after the transaction block has exited."
            )
        return self._conn

    def _close(self) -> None:
        self.closed = True
        self._conn = None

    async def fetch(
        self, query: str, *args: Any, timeout: float | None = None
    ) -> list[Any]:
        self._ensure_open()
        async with self._lock:
            conn = self._ensure_open()
            rows = await conn.fetch(query, *args, timeout=timeout)
            return list(rows)

    async def fetchrow(
        self, query: str, *args: Any, timeout: float | None = None
    ) -> Any:
        self._ensure_open()
        async with self._lock:
            conn = self._ensure_open()
            return await conn.fetchrow(query, *args, timeout=timeout)

    async def fetchval(
        self,
        query: str,
        *args: Any,
        column: int = 0,
        timeout: float | None = None,
    ) -> Any:
        self._ensure_open()
        async with self._lock:
            conn = self._ensure_open()
            return await conn.fetchval(query, *args, column=column, timeout=timeout)

    async def execute(
        self, query: str, *args: Any, timeout: float | None = None
    ) -> str:
        self._ensure_open()
        async with self._lock:
            conn = self._ensure_open()
            res = await conn.execute(query, *args, timeout=timeout)
            return str(res)

    async def executemany(
        self, command: str, args: Any, *, timeout: float | None = None
    ) -> None:
        self._ensure_open()
        async with self._lock:
            conn = self._ensure_open()
            await conn.executemany(command, args, timeout=timeout)

    @asynccontextmanager
    async def transaction(
        self, **kwargs: Any
    ) -> AsyncGenerator[TransactionConnection, None]:
        self._ensure_open()
        async with self._lock:
            conn = self._ensure_open()
            tx = conn.transaction(**kwargs)
            await tx.start()
        try:
            yield self
        except BaseException:
            async with self._lock:
                if self._conn is not None:
                    await tx.rollback()
            raise
        else:
            async with self._lock:
                conn = self._ensure_open()
                await tx.commit()

    def __getattr__(self, name: str) -> Any:
        conn = self._ensure_open()
        return getattr(conn, name)


_active_connection: ContextVar[Any] = ContextVar(
    "swiftpy_active_connection",
    default=None,
)


def get_active_connection() -> Any:
    conn = _active_connection.get()
    if conn is not None and getattr(conn, "closed", False):
        raise TransactionClosedError(
            "Cannot use transaction connection after the transaction block has exited."
        )
    return conn


class Database:
    """
    Database transaction manager.
    Note: Transactions share a single database connection and serialize concurrent
    operations via an internal lock; they are not intended for query parallelism.
    """

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[Any, None]:
        existing = get_active_connection()

        if existing is not None:
            async with existing.transaction():
                yield existing
            return

        container = get_container()

        db_pool = container.resolve(DatabasePool)

        pool = db_pool.get_pool()

        async with pool.acquire() as conn:
            handle = TransactionConnection(conn)
            token = _active_connection.set(handle)

            try:
                async with handle.transaction():
                    yield handle
            finally:
                handle._close()
                _active_connection.reset(token)


db = Database()

__all__ = [
    "Database",
    "TransactionClosedError",
    "TransactionConnection",
    "db",
    "get_active_connection",
]
