from __future__ import annotations

import pytest

from app.providers.app_service_provider import AppServiceProvider
from app.providers.database_service_provider import DatabaseServiceProvider
from swiftpy.core.bootstrap import create_app
from swiftpy.database.pool import DatabasePool


@pytest.fixture(autouse=True)
async def ensure_db_pool():
    app = create_app(providers=[AppServiceProvider, DatabaseServiceProvider])
    pool = app.container.resolve(DatabasePool)
    await pool.init()
    try:
        yield pool
    finally:
        await pool.close()
