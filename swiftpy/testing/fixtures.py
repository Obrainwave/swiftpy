from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from swiftpy.core.config import Config
from swiftpy.core.container import Container, get_container, set_container
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import TransactionConnection, _active_connection

_pytest_fixture: Any
try:
    from pytest import fixture as _pytest_fixture
except ImportError:
    _pytest_fixture = None

logger = logging.getLogger("swiftpy.testing.fixtures")


def _build_standalone_config() -> Config:
    """Load configuration and apply DB_* fallbacks when no config/ directory exists."""
    config = Config.load()
    if not config.has("database.connections"):
        default_conn = str(config.get("database.default", "postgresql"))
        base = f"database.connections.{default_conn}"
        env_defaults: list[tuple[str, str, Any]] = [
            ("DB_HOST", f"{base}.host", "127.0.0.1"),
            ("DB_PORT", f"{base}.port", 5432),
            ("DB_DATABASE", f"{base}.database", "swiftpy"),
            ("DB_USERNAME", f"{base}.user", "postgres"),
            ("DB_PASSWORD", f"{base}.password", ""),
        ]
        for env_key, cfg_key, fallback in env_defaults:
            raw = os.getenv(env_key)
            val: Any = raw if raw is not None else fallback
            if cfg_key.endswith(".port"):
                val = int(val)
            config.set(cfg_key, val)
    return config


@asynccontextmanager
async def transactional_test(
    pool: DatabasePool | None = None,
) -> AsyncGenerator[TransactionConnection, None]:
    """
    Wrap database operations in an isolated transaction that is rolled back on exit.
    Self-contained: initializes an active DatabasePool automatically if one is not yet bound/ready.
    """
    initialized_here = False
    if pool is None:
        try:
            container = get_container()
        except RuntimeError:
            container = Container()
            set_container(container)

        if container.has(DatabasePool):
            pool = container.resolve(DatabasePool)
        else:
            config = (
                container.resolve(Config)
                if container.has(Config)
                else _build_standalone_config()
            )
            if not container.has(Config):
                container.instance(Config, config)
            pool = DatabasePool(config)
            container.instance(DatabasePool, pool)

    if not pool.is_initialized:
        await pool.init()
        initialized_here = True

    db_pool = pool.get_pool()
    conn = await db_pool.acquire()
    handle = TransactionConnection(conn)
    tx = conn.transaction()
    await tx.start()
    token = _active_connection.set(handle)

    try:
        yield handle
    finally:
        try:
            await handle.rollback_transaction(tx)
        except (asyncpg.PostgresError, OSError):
            logger.debug("Transaction rollback encountered an error", exc_info=True)
        finally:
            handle.close()
            _active_connection.reset(token)
            await db_pool.release(conn)
            if initialized_here:
                await pool.close()


async def _db_transaction_impl() -> AsyncGenerator[TransactionConnection, None]:
    """
    Pytest fixture wrapping test execution in a rolled-back database transaction.
    Works standalone without requiring external project-specific pool fixtures.
    """
    async with transactional_test() as conn:
        yield conn


def _missing_pytest_fixture() -> AsyncGenerator[TransactionConnection, None]:
    raise RuntimeError(
        "pytest is required to use the 'db_transaction' fixture."
    )


db_transaction: Callable[[], AsyncGenerator[TransactionConnection, None]] | object
if _pytest_fixture is not None:
    db_transaction = _pytest_fixture(name="db_transaction")(_db_transaction_impl)
else:
    db_transaction = _missing_pytest_fixture


__all__ = ["db_transaction", "transactional_test"]
