from __future__ import annotations

import contextlib
import os
import uuid

import httpx
import pytest

from swiftpy.ai.exceptions import AIProviderError
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.vectorstore.base import VectorRecord
from swiftpy.ai.vectorstore.chroma_adapter import ChromaAdapter


async def _check_chroma_live(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{url.rstrip('/')}/api/v1/heartbeat")
            return bool(resp.status_code == 200)
    except (httpx.RequestError, OSError, TimeoutError):
        return False


@pytest.mark.asyncio
async def test_chroma_live_integration() -> None:
    """Live integration test against a running ChromaDB server."""
    chroma_url = os.getenv("SWIFTPY_CHROMA_URL", "http://localhost:8000")
    run_live = bool(os.getenv("SWIFTPY_TEST_CHROMA") or os.getenv("RUN_INTEGRATION_TESTS"))

    if not run_live or not await _check_chroma_live(chroma_url):
        pytest.skip("Live Chroma instance not available or SWIFTPY_TEST_CHROMA not set.")

    coll_name = f"swiftpy_itest_{uuid.uuid4().hex[:8]}"
    pool = AIHttpPool()
    adapter = ChromaAdapter(
        collection_name=coll_name,
        dimensions=4,
        metric="cosine",
        base_url=chroma_url,
        http_pool=pool,
    )

    try:
        # 1. Create collection
        await adapter.create_collection(coll_name, dimensions=4, metric="cosine")

        # 2. Upsert batch
        records = [
            VectorRecord(id="doc_alpha", vector=[1.0, 0.0, 0.0, 0.0], metadata={"cluster": "A"}, content="Alpha"),
            VectorRecord(id="doc_beta", vector=[0.0, 1.0, 0.0, 0.0], metadata={"cluster": "B"}, content="Beta"),
        ]
        await adapter.upsert_batch(records)

        # 3. Count
        count = await adapter.count()
        assert count == 2

        # 4. Get
        item = await adapter.get("doc_alpha")
        assert item is not None
        assert item.id == "doc_alpha"
        assert item.content == "Alpha"
        assert item.metadata.get("cluster") == "A"

        # 5. Search
        results = await adapter.search([1.0, 0.0, 0.0, 0.0], limit=2)
        assert len(results) >= 1
        assert results[0].id == "doc_alpha"
        assert abs(results[0].score - 1.0) < 1e-3

        # 6. Delete
        assert await adapter.delete("doc_beta") is True
        assert await adapter.count() == 1

    finally:
        with contextlib.suppress(AIProviderError, OSError):
            await adapter.drop_collection(coll_name)
        await pool.aclose()
