from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar

import asyncpg

from swiftpy.core.bootstrap import get_container
from swiftpy.database.pool import DatabasePool

_active_connection: ContextVar[asyncpg.Connection | None] = ContextVar(
    "swiftpy_active_connection",
    default=None,
)


def get_active_connection() -> asyncpg.Connection | None:
    return _active_connection.get()


class Database:
    """
    Database transaction manager.
    """

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[asyncpg.Connection, None]:
        existing = _active_connection.get()

        if existing is not None:
            async with existing.transaction():
                yield existing
            return

        container = get_container()

        db_pool = container.resolve(DatabasePool)

        pool = db_pool.get_pool()

        async with pool.acquire() as conn:
            token = _active_connection.set(conn)

            try:
                async with conn.transaction():
                    yield conn
            finally:
                _active_connection.reset(token)


db = Database()
