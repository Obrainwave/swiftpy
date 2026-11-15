from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest

from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query
from swiftpy.database.transaction import get_active_connection
from swiftpy.testing.fixtures import transactional_test


@pytest.fixture
async def temp_table(ensure_db_pool: DatabasePool) -> AsyncGenerator[str, None]:
    table_name = f"test_rollback_{uuid.uuid4().hex[:8]}"
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                title VARCHAR(100) NOT NULL
            );
            """
        )
    try:
        yield table_name
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{table_name}";')


@pytest.mark.asyncio
async def test_transactional_test_rollback_leaves_no_trace(
    ensure_db_pool: DatabasePool,
    temp_table: str,
) -> None:
    # 1. Inside transactional_test context: insert a record via QueryBuilder
    async with transactional_test(pool=ensure_db_pool):
        query = Query().table(temp_table)
        inserted_id = await query.insert(
            {"title": "Temporary Title Inside Transaction"}
        )
        assert inserted_id > 0

        # Verify the record exists during the transaction
        records = await Query().table(temp_table).get()
        assert len(records) == 1
        assert records[0]["title"] == "Temporary Title Inside Transaction"

    # 2. Outside transactional_test context: verify transaction was rolled back cleanly
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f'SELECT * FROM "{temp_table}";')
        # Database must be pristine: zero residual records!
        assert len(rows) == 0


@pytest.mark.asyncio
async def test_transactional_test_rollback_on_exception(
    ensure_db_pool: DatabasePool,
    temp_table: str,
) -> None:
    # Pre-populate one baseline record outside transactional_test
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO \"{temp_table}\" (title) VALUES ('Baseline Record');"
        )

    # Execute transactional_test where the body raises an unhandled exception
    with pytest.raises(RuntimeError, match="Simulated test crash"):
        async with transactional_test(pool=ensure_db_pool):
            await (
                Query().table(temp_table).insert({"title": "Record During Failed Test"})
            )
            records = await Query().table(temp_table).get()
            assert len(records) == 2
            raise RuntimeError("Simulated test crash")

    # Verify that the unhandled exception rolled back and left only the baseline record
    async with pool.acquire() as conn:
        rows = await conn.fetch(f'SELECT * FROM "{temp_table}";')
        assert len(rows) == 1
        assert rows[0]["title"] == "Baseline Record"


@pytest.mark.asyncio
async def test_transactional_test_rollback_for_updates_and_deletes(
    ensure_db_pool: DatabasePool,
    temp_table: str,
) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO \"{temp_table}\" (title) VALUES ('Original Row 1'), ('Original Row 2');"
        )

    # In transactional context: perform UPDATE, DELETE, and INSERT
    async with transactional_test(pool=ensure_db_pool):
        # Update Row 1
        await (
            Query()
            .table(temp_table)
            .where("title", "Original Row 1")
            .update({"title": "Updated Row 1"})
        )
        # Delete Row 2
        await Query().table(temp_table).where("title", "Original Row 2").delete()
        # Insert Row 3
        await Query().table(temp_table).insert({"title": "Temporary Row 3"})

        # Inside transaction: verify state reflects all mutations
        records = await Query().table(temp_table).order_by("title").get()
        titles = [r["title"] for r in records]
        assert titles == ["Temporary Row 3", "Updated Row 1"]

    # Outside transaction: verify rollback restored the pre-test database state completely
    async with pool.acquire() as conn:
        rows = await conn.fetch(f'SELECT * FROM "{temp_table}" ORDER BY title;')
        titles = [r["title"] for r in rows]
        assert titles == ["Original Row 1", "Original Row 2"]


@pytest.mark.asyncio
async def test_transactional_test_context_cleanup_and_direct_connection_verification(
    ensure_db_pool: DatabasePool,
    temp_table: str,
) -> None:
    # 1. Before entry: active connection must be None
    assert get_active_connection() is None

    # 2. Inside transactional_test: verify active connection is bound
    async with transactional_test(pool=ensure_db_pool) as conn:
        active_conn = get_active_connection()
        assert active_conn is not None
        assert active_conn is conn

        # Direct mechanism verification: QueryBuilder._connection() must return the
        # exact same active transaction connection object, NOT a different pooled connection!
        qb_conn = await Query().table(temp_table)._connection()
        try:
            assert qb_conn is conn, (
                "QueryBuilder did not use the active transaction connection!"
            )
        finally:
            await Query().table(temp_table)._release(qb_conn)

    # 3. After normal exit: ContextVar token must be reset to None
    assert get_active_connection() is None

    # 4. After exception exit: ContextVar token must also be reset to None
    with pytest.raises(ValueError):
        async with transactional_test(pool=ensure_db_pool):
            assert get_active_connection() is not None
            raise ValueError("Test error for cleanup verification")

    assert get_active_connection() is None


@pytest.mark.asyncio
async def test_concurrent_independent_transactions(
    ensure_db_pool: DatabasePool,
    temp_table: str,
) -> None:
    """Verify two concurrent tasks use independent connections and transactions."""
    task_connections: list[object] = []

    async def worker(task_name: str, sleep_duration: float) -> list[dict[str, object]]:
        async with transactional_test(pool=ensure_db_pool) as conn:
            task_connections.append(conn)
            # Insert record unique to this task
            await (
                Query().table(temp_table).insert({"title": f"Record from {task_name}"})
            )
            await asyncio.sleep(sleep_duration)

            # Query table within this task's transaction
            records = await Query().table(temp_table).get()
            return records

    results = await asyncio.gather(
        worker("Task-A", 0.03),
        worker("Task-B", 0.01),
    )

    # Verify both tasks used distinct physical connections
    assert len(task_connections) == 2
    assert task_connections[0] is not task_connections[1]

    # Verify each task saw only its own transaction
    assert len(results[0]) == 1
    assert results[0][0]["title"] == "Record from Task-A"

    assert len(results[1]) == 1
    assert results[1][0]["title"] == "Record from Task-B"

    # Outside: zero residual records
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f'SELECT * FROM "{temp_table}";')
        assert len(rows) == 0
