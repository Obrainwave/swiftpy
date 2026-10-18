from collections.abc import AsyncGenerator

import pytest

from swiftpy import Application
from swiftpy.database.pool import DatabasePool


@pytest.fixture(autouse=True)
async def ensure_db_pool() -> AsyncGenerator[DatabasePool, None]:
    app = Application()
    pool = app.container.resolve(DatabasePool)
    await pool.init()
    try:
        yield pool
    finally:
        await pool.close()
