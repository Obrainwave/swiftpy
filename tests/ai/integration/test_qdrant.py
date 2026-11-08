from __future__ import annotations

import contextlib
import os
import uuid

try:
    import httpx
except ImportError:
    httpx = None

import pytest

from swiftpy.ai.exceptions import AIProviderError
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.vectorstore.base import VectorRecord
from swiftpy.ai.vectorstore.qdrant_adapter import QdrantAdapter


async def _check_qdrant_live(url: str) -> bool:
    if httpx is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{url.rstrip('/')}/collections")
            return bool(resp.status_code == 200)
    except (httpx.RequestError, OSError, TimeoutError):
        return False


@pytest.mark.asyncio
async def test_qdrant_live_integration() -> None:
    """Live integration test against a running Qdrant instance."""
    qdrant_url = os.getenv("SWIFTPY_QDRANT_URL", "http://localhost:6333")
    run_live = bool(os.getenv("SWIFTPY_TEST_QDRANT") or os.getenv("RUN_INTEGRATION_TESTS"))

    if not run_live or not await _check_qdrant_live(qdrant_url):
        pytest.skip("Live Qdrant instance not available or SWIFTPY_TEST_QDRANT not set.")

    coll_name = f"swiftpy_itest_{uuid.uuid4().hex[:8]}"
    pool = AIHttpPool()
    adapter = QdrantAdapter(
        collection_name=coll_name,
        dimensions=4,
        metric="cosine",
        base_url=qdrant_url,
        http_pool=pool,
    )

    try:
        # 1. Create isolated collection
        await adapter.create_collection(coll_name, dimensions=4, metric="cosine")

        # 2. Batch upsert
        records = [
            VectorRecord(id="doc_ai", vector=[1.0, 0.0, 0.0, 0.0], metadata={"topic": "ai"}, content="AI Doc"),
            VectorRecord(id="doc_web", vector=[0.0, 1.0, 0.0, 0.0], metadata={"topic": "web"}, content="Web Doc"),
            VectorRecord(id="doc_db", vector=[0.0, 0.0, 1.0, 0.0], metadata={"topic": "db"}, content="DB Doc"),
        ]
        await adapter.upsert_batch(records)

        # 3. Count
        count = await adapter.count()
        assert count == 3

        # 4. Get single record and verify metadata isolation
        item = await adapter.get("doc_ai")
        assert item is not None
        assert item.id == "doc_ai"
        assert item.content == "AI Doc"
        assert item.metadata == {"topic": "ai"}
        assert "_orig_id" not in item.metadata
        assert "_content" not in item.metadata

        # 5. Search nearest neighbor
        results = await adapter.search([1.0, 0.0, 0.0, 0.0], limit=2)
        assert len(results) >= 1
        assert results[0].id == "doc_ai"
        assert abs(results[0].score - 1.0) < 1e-3

        # 6. Filter search
        filtered = await adapter.search([0.0, 1.0, 0.0, 0.0], limit=5, filter={"topic": "web"})
        assert len(filtered) == 1
        assert filtered[0].id == "doc_web"

        # 7. Delete
        assert await adapter.delete("doc_web") is True
        assert await adapter.get("doc_web") is None
        assert await adapter.count() == 2

    finally:
        with contextlib.suppress(AIProviderError, OSError):
            await adapter.drop_collection(coll_name)
        await pool.aclose()
