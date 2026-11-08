from __future__ import annotations

import uuid

import pytest

from swiftpy.ai.vectorstore.pgvector_adapter import PgVectorAdapter
from swiftpy.core.config import Config
from swiftpy.core.container import get_container
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import db


async def _check_pgvector_connection() -> bool:
    try:
        config = Config()
        pool = DatabasePool(config)
        await pool.init()
        raw_pool = pool.get_pool()
        conn = await raw_pool.acquire()
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            return True
        finally:
            await raw_pool.release(conn)
            await pool.close()
    except (OSError, RuntimeError, TimeoutError):
        return False


@pytest.mark.asyncio
async def test_pgvector_live_database_crud_and_search() -> None:
    """
    Integration test against a live PostgreSQL instance with pgvector.
    Executed when PostgreSQL with pgvector extension is running.
    """
    if not await _check_pgvector_connection():
        pytest.skip("PostgreSQL with pgvector is not available in the local execution environment.")

    config = Config()
    pool = DatabasePool(config)
    await pool.init()

    coll_name = f"test_{uuid.uuid4().hex[:8]}"
    adapter = PgVectorAdapter(collection_name=coll_name, dimensions=3, metric="cosine", pool=pool)

    try:
        # 1. Create collection with HNSW index and vector extension
        await adapter.create_collection(coll_name, 3, metric="cosine")

        # 2. Insert records
        await adapter.upsert("doc_a", [1.0, 0.0, 0.0], {"topic": "ai", "env": "prod"}, content="AI Doc")
        await adapter.upsert("doc_b", [0.0, 1.0, 0.0], {"topic": "web", "env": "prod"}, content="Web Doc")
        await adapter.upsert("doc_c", [0.0, 0.0, 1.0], {"topic": "db", "env": "stage"}, content="DB Doc")

        assert await adapter.count() == 3

        # 3. Retrieve record with get()
        item = await adapter.get("doc_a")
        assert item is not None
        assert item.id == "doc_a"
        assert item.content == "AI Doc"
        assert item.metadata["topic"] == "ai"

        # 4. Search with cosine similarity
        results = await adapter.search([1.0, 0.0, 0.0], limit=2)
        assert len(results) == 2
        assert results[0].id == "doc_a"
        assert pytest.approx(results[0].score, rel=1e-3) == 1.0

        # 5. Search with JSONB containment filter
        filtered = await adapter.search([1.0, 0.0, 0.0], limit=5, filter={"env": "prod"})
        assert len(filtered) == 2
        assert {r.id for r in filtered} == {"doc_a", "doc_b"}

        # 6. Update existing record
        await adapter.upsert("doc_a", [0.707, 0.707, 0.0], {"topic": "ai_updated", "env": "prod"}, content="Updated")
        updated_item = await adapter.get("doc_a")
        assert updated_item is not None
        assert updated_item.metadata["topic"] == "ai_updated"
        assert updated_item.content == "Updated"

        # 7. Delete record
        assert await adapter.delete("doc_b") is True
        assert await adapter.delete("doc_b") is False
        assert await adapter.count() == 2

        # 8. Batch delete
        deleted = await adapter.delete_batch(["doc_a", "doc_c"])
        assert deleted == 2
        assert await adapter.count() == 0

    finally:
        await adapter.drop_collection(coll_name)
        await pool.close()


@pytest.mark.asyncio
async def test_pgvector_transaction_rollback_and_commit() -> None:
    """Verify that PgVectorAdapter operations participate in db_transaction and rollback cleanly."""
    if not await _check_pgvector_connection():
        pytest.skip("PostgreSQL with pgvector is not available in the local execution environment.")

    config = Config()
    pool = DatabasePool(config)
    await pool.init()
    container = get_container()
    container.instance(DatabasePool, pool)

    coll_name = f"test_tx_{uuid.uuid4().hex[:8]}"
    adapter = PgVectorAdapter(collection_name=coll_name, dimensions=3, pool=pool)
    await adapter.create_collection(coll_name, 3)

    try:
        # 1. Rollback test: operation inside rolled-back transaction must not persist
        try:
            async with db.transaction():
                await adapter.upsert("tx_doc_rollback", [1.0, 0.0, 0.0], {"state": "uncommitted"})
                # Simulate error triggering transaction rollback
                raise RuntimeError("Intentional rollback")
        except RuntimeError:
            pass

        assert await adapter.get("tx_doc_rollback") is None
        assert await adapter.count() == 0

        # 2. Commit test: operation inside successful transaction persists
        async with db.transaction():
            await adapter.upsert("tx_doc_commit", [0.0, 1.0, 0.0], {"state": "committed"})

        persisted = await adapter.get("tx_doc_commit")
        assert persisted is not None
        assert persisted.metadata["state"] == "committed"
        assert await adapter.count() == 1

    finally:
        await adapter.drop_collection(coll_name)
        await pool.close()
