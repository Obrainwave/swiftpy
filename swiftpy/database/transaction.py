from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.pool import DatabasePool

logger = logging.getLogger(__name__)


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
        self._tx_depth: int = 0
        self._commit_callbacks: list[list[Callable[[], Any]]] = []

    def on_commit(self, callback: Callable[[], Any]) -> None:
        """Register a callback to be executed after the outermost transaction successfully commits."""
        self._ensure_open()
        if not self._commit_callbacks:
            self._commit_callbacks.append([])
        self._commit_callbacks[-1].append(callback)

    async def _run_commit_callbacks(self, callbacks: list[Callable[[], Any]]) -> None:
        for cb in callbacks:
            try:
                res = cb()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                logger.exception("Error executing on_commit callback %r", cb)

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
            self._tx_depth += 1
            self._commit_callbacks.append([])
        try:
            yield self
        except BaseException:
            async with self._lock:
                self._tx_depth -= 1
                if self._commit_callbacks:
                    self._commit_callbacks.pop()
                if self._conn is not None:
                    await tx.rollback()
            raise
        else:
            callbacks_to_run: list[Callable[[], Any]] = []
            async with self._lock:
                self._tx_depth -= 1
                level_callbacks = (
                    self._commit_callbacks.pop() if self._commit_callbacks else []
                )
                if self._tx_depth > 0:
                    if self._commit_callbacks:
                        self._commit_callbacks[-1].extend(level_callbacks)
                else:
                    callbacks_to_run = level_callbacks
                conn = self._ensure_open()
                await tx.commit()

            if callbacks_to_run:
                await self._run_commit_callbacks(callbacks_to_run)

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


def on_commit(callback: Callable[[], Any]) -> None:
    """
    Register a callback to run after the active transaction successfully commits.
    If no database transaction is active, the callback is executed immediately.
    """
    conn = get_active_connection()
    if conn is not None and hasattr(conn, "on_commit"):
        conn.on_commit(callback)
    else:
        res = callback()
        if inspect.isawaitable(res):
            try:
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[Any] = asyncio.ensure_future(res, loop=loop)

                def _log_err(t: asyncio.Future[Any]) -> None:
                    if not t.cancelled() and t.exception():
                        logger.exception(
                            "Async on_commit callback failed", exc_info=t.exception()
                        )

                fut.add_done_callback(_log_err)
            except RuntimeError:
                pass


class Database:
    """
    Database transaction manager.
    Note: Transactions share a single database connection and serialize concurrent
    operations via an internal lock; they are not intended for query parallelism.
    """

    def on_commit(self, callback: Callable[[], Any]) -> None:
        """Register a callback to run after the active transaction commits, or immediately if none."""
        on_commit(callback)

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
    "on_commit",
]
