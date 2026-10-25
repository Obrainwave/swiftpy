from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg
import pytest

from swiftpy.core.bootstrap import get_container
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import _active_connection

logger = logging.getLogger("swiftpy.testing.fixtures")


@asynccontextmanager
async def transactional_test(
    pool: DatabasePool | None = None,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """
    Wrap database operations in an isolated transaction that is rolled back on exit.
    Ensures that tests leave zero residual records or changes in the database.
    """
    if pool is None:
        container = get_container()
        pool = container.resolve(DatabasePool)

    db_pool = pool.get_pool()
    conn = await db_pool.acquire()
    tx = conn.transaction()
    await tx.start()
    token = _active_connection.set(conn)

    try:
        yield conn
    finally:
        try:
            await tx.rollback()
        except (asyncpg.PostgresError, OSError):
            logger.debug("Transaction rollback encountered an error", exc_info=True)
        finally:
            _active_connection.reset(token)
            await db_pool.release(conn)


@pytest.fixture
async def db_transaction(
    ensure_db_pool: DatabasePool,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """
    Pytest fixture wrapping the test execution in a rolled-back transaction.
    """
    async with transactional_test(pool=ensure_db_pool) as conn:
        yield conn


__all__ = ["db_transaction", "transactional_test"]
