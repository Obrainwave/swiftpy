from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None

import pytest

import swiftpy.ai.vectorstore  # noqa: F401
from swiftpy.ai.pool import AIHttpPool


@pytest.fixture(autouse=True)
async def ensure_db_pool() -> AsyncGenerator[None, None]:
    """Override parent DB pool fixture since AI tests do not require PostgreSQL."""
    yield


@pytest.fixture(autouse=True)
async def reset_ai_http_pool() -> AsyncGenerator[None, None]:
    """Ensure clean AIHttpPool state before and after each test."""
    pool = AIHttpPool.get_instance()
    await pool.aclose()
    AIHttpPool.set_instance(None)
    try:
        yield
    finally:
        pool = AIHttpPool.get_instance()
        await pool.aclose()
        AIHttpPool.set_instance(None)


@pytest.fixture
async def mock_client_factory() -> AsyncGenerator[Callable[[Callable[[Any], Any]], Any], None]:
    """
    Factory creating an httpx.AsyncClient with MockTransport that is registered
    for automatic asynchronous cleanup.
    """
    if httpx is None:
        pytest.skip("httpx is not installed in the local environment.")

    clients: list[httpx.AsyncClient] = []

    def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        clients.append(client)
        return client

    try:
        yield _factory
    finally:
        for c in clients:
            if not c.is_closed:
                await c.aclose()
