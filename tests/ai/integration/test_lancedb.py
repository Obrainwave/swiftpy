from __future__ import annotations

import contextlib
import os
import uuid

import httpx
import pytest

from swiftpy.ai.exceptions import AIProviderError
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.vectorstore.base import VectorRecord
from swiftpy.ai.vectorstore.lancedb_adapter import LanceDbAdapter


async def _check_lancedb_live(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{url.rstrip('/')}/v1/table")
            return bool(resp.status_code in (200, 404))
    except (httpx.RequestError, OSError, TimeoutError):
        return False


@pytest.mark.asyncio
async def test_lancedb_live_integration() -> None:
    """Live integration test against a running LanceDB REST API service."""
    lance_url = os.getenv("SWIFTPY_LANCEDB_URL", "http://localhost:8080")
    run_live = bool(os.getenv("SWIFTPY_TEST_LANCEDB") or os.getenv("RUN_INTEGRATION_TESTS"))

    if not run_live or not await _check_lancedb_live(lance_url):
        pytest.skip("Live LanceDB instance not available or SWIFTPY_TEST_LANCEDB not set.")

    coll_name = f"swiftpy_itest_{uuid.uuid4().hex[:8]}"
    pool = AIHttpPool()
    adapter = LanceDbAdapter(
        collection_name=coll_name,
        dimensions=4,
        metric="cosine",
        base_url=lance_url,
        http_pool=pool,
    )

    try:
        # 1. Create table
        await adapter.create_collection(coll_name, dimensions=4, metric="cosine")

        # 2. Upsert
        records = [
            VectorRecord(id="doc_lance_1", vector=[1.0, 0.0, 0.0, 0.0], metadata={"owner": "alice"}, content="Lance 1"),
            VectorRecord(id="doc_lance_2", vector=[0.0, 1.0, 0.0, 0.0], metadata={"owner": "bob"}, content="Lance 2"),
        ]
        await adapter.upsert_batch(records)

        # 3. Count
        count = await adapter.count()
        assert count == 2

        # 4. Search
        results = await adapter.search([1.0, 0.0, 0.0, 0.0], limit=2)
        assert len(results) >= 1
        assert results[0].id == "doc_lance_1"

        # 5. Delete
        assert await adapter.delete("doc_lance_2") is True
        assert await adapter.count() == 1

    finally:
        with contextlib.suppress(AIProviderError, OSError):
            await adapter.drop_collection(coll_name)
        await pool.aclose()
