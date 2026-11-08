from __future__ import annotations

import os
import uuid

import pytest

from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.vectorstore.pinecone_adapter import PineconeAdapter


@pytest.mark.asyncio
async def test_pinecone_live_integration() -> None:
    """Live integration test against a real Pinecone index (requires PINECONE_API_KEY)."""
    api_key = os.getenv("PINECONE_API_KEY")
    index_host = os.getenv("PINECONE_INDEX_HOST")
    run_live = bool(os.getenv("SWIFTPY_TEST_PINECONE") and api_key and index_host)

    if not run_live:
        pytest.skip("PINECONE_API_KEY or PINECONE_INDEX_HOST not set, skipping live Pinecone test.")

    test_ns = f"ns_{uuid.uuid4().hex[:8]}"
    pool = AIHttpPool()
    adapter = PineconeAdapter(
        collection_name="integration-test",
        dimensions=4,
        metric="cosine",
        api_key=api_key,
        base_url=index_host,
        namespace=test_ns,
        http_pool=pool,
    )

    try:
        # 1. Upsert into isolated namespace
        rec_id = f"pine_{uuid.uuid4().hex[:6]}"
        await adapter.upsert(
            id=rec_id,
            vector=[1.0, 0.0, 0.0, 0.0],
            metadata={"source": "live_test"},
            content="Pinecone live record",
        )

        # 2. Get from namespace
        item = await adapter.get(rec_id)
        assert item is not None
        assert item.id == rec_id
        assert item.content == "Pinecone live record"
        assert item.metadata.get("source") == "live_test"

        # 3. Search
        results = await adapter.search([1.0, 0.0, 0.0, 0.0], limit=1)
        assert len(results) >= 1
        assert results[0].id == rec_id

        # 4. Delete
        assert await adapter.delete(rec_id) is True
        assert await adapter.get(rec_id) is None

    finally:
        await pool.aclose()
