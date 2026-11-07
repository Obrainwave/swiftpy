from __future__ import annotations

import pytest

from swiftpy.database.orm.model import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query


class TerminalUser(Model):
    __table__ = "test_terminal_users"
    __fillable__ = ("name", "score", "active")

    id: int
    name: str
    score: int
    active: bool


@pytest.fixture(autouse=True)
async def setup_terminal_table(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS "test_terminal_users" CASCADE;
            CREATE TABLE "test_terminal_users" (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                score INT NOT NULL DEFAULT 0,
                active BOOLEAN NOT NULL DEFAULT TRUE
            );
            INSERT INTO "test_terminal_users" (name, score, active) VALUES
                ('Alice', 10, TRUE),
                ('Bob', 20, TRUE),
                ('Charlie', 30, TRUE),
                ('Diana', 40, FALSE),
                ('Eve', 50, TRUE);
            """
        )


@pytest.mark.asyncio
async def test_query_builder_terminals_do_not_mutate_state() -> None:
    q = (
        Query.table("test_terminal_users")
        .select("id", "name", "score")
        .where("active", True)
        .order_by("score", "ASC")
    )
    sql_before, bindings_before = q.to_sql()

    total = await q.count()
    assert total == 4
    assert q.to_sql() == (sql_before, bindings_before)

    score_sum = await q.sum("score")
    assert score_sum == 110
    assert q.to_sql() == (sql_before, bindings_before)

    score_min = await q.min("score")
    assert score_min == 10
    assert q.to_sql() == (sql_before, bindings_before)

    score_max = await q.max("score")
    assert score_max == 50
    assert q.to_sql() == (sql_before, bindings_before)

    score_avg = await q.avg("score")
    assert float(score_avg) == 27.5
    assert q.to_sql() == (sql_before, bindings_before)

    has_rows = await q.exists()
    assert has_rows is True
    assert q.to_sql() == (sql_before, bindings_before)

    first_row = await q.first()
    assert first_row is not None
    assert first_row["name"] == "Alice"
    assert q.to_sql() == (sql_before, bindings_before)

    all_rows = await q.get()
    assert len(all_rows) == 4
    assert [r["name"] for r in all_rows] == ["Alice", "Bob", "Charlie", "Eve"]
    assert q.to_sql() == (sql_before, bindings_before)

    empty_q = Query.table("test_terminal_users").where("name", "NonExistent")
    assert await empty_q.exists() is False
    assert await empty_q.count() == 0
    assert await empty_q.first() is None
    assert await empty_q.get() == []


@pytest.mark.asyncio
async def test_model_query_builder_terminals_and_pagination_pattern() -> None:
    mq = TerminalUser.where("active", True).order_by("id", "ASC")
    sql_before, bindings_before = mq.builder.to_sql()

    # 1. count() + first() + exists() do not mutate ModelQueryBuilder
    total = await mq.count()
    assert total == 4
    assert mq.builder.to_sql() == (sql_before, bindings_before)

    assert await mq.exists() is True
    assert mq.builder.to_sql() == (sql_before, bindings_before)

    first_user = await mq.first()
    assert first_user is not None
    assert first_user.name == "Alice"
    assert mq.builder.to_sql() == (sql_before, bindings_before)

    all_active = await mq.get()
    assert len(all_active) == 4

    # 2. Pagination pattern: total + page from the same base query
    base_query = TerminalUser.where("active", True).order_by("score", "DESC")
    total_count = await base_query.count()
    page_items = await base_query.clone().limit(2).get()

    assert total_count == 4
    assert len(page_items) == 2
    assert [u.name for u in page_items] == ["Eve", "Charlie"]
    # Base query remains un-limited and reusable
    full_items = await base_query.get()
    assert len(full_items) == 4
