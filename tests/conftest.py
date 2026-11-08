from collections.abc import AsyncGenerator

import pytest

from swiftpy import Application
from swiftpy.database.pool import DatabasePool


@pytest.fixture(autouse=True)
async def ensure_db_pool() -> AsyncGenerator[DatabasePool | None, None]:
    app = Application()
    pool = app.container.resolve(DatabasePool)
    try:
        await pool.init()
    except (OSError, RuntimeError, TimeoutError):
        yield None
        return

    try:
        yield pool
    finally:
        await pool.close()
