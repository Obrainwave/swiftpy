from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swiftpy.core.container import get_container
from swiftpy.database.exceptions import RecordNotFoundError
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query, QueryError


@pytest.fixture
def mock_db_pool() -> tuple[AsyncMock, MagicMock]:
    """Fixture providing a mock database connection and pool registered in the container."""
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_conn)
    mock_pool.release = AsyncMock()

    mock_db = MagicMock(spec=DatabasePool)
    mock_db.get_pool = MagicMock(return_value=mock_pool)

    container = get_container()
    container.instance(DatabasePool, mock_db)
    return mock_conn, mock_pool


def test_where_boolean_operators_and_or() -> None:
    qb = Query.table("users").where("status", "active").or_where("role", "admin")
    sql, bindings = qb.to_sql()
    assert 'WHERE "status" = $1 OR "role" = $2' in sql
    assert bindings == ["active", "admin"]


def test_where_in_and_or_where_in() -> None:
    qb = (
        Query.table("orders")
        .where_in("status", ["pending", "processing"])
        .or_where_in("country", ["US", "CA"])
    )
    sql, bindings = qb.to_sql()
    assert 'WHERE "status" IN ($1, $2) OR "country" IN ($3, $4)' in sql
    assert bindings == ["pending", "processing", "US", "CA"]


def test_where_not_in_and_or_where_not_in() -> None:
    qb = (
        Query.table("users")
        .where_not_in("id", [1, 2])
        .or_where_not_in("group", ["test", "banned"])
    )
    sql, bindings = qb.to_sql()
    assert 'WHERE "id" NOT IN ($1, $2) OR "group" NOT IN ($3, $4)' in sql
    assert bindings == [1, 2, "test", "banned"]


def test_where_between_and_not_between() -> None:
    qb = (
        Query.table("products")
        .where_between("price", [10, 50])
        .where_not_between("stock", (0, 5))
    )
    sql, bindings = qb.to_sql()
    assert 'WHERE "price" BETWEEN $1 AND $2 AND "stock" NOT BETWEEN $3 AND $4' in sql
    assert bindings == [10, 50, 0, 5]


def test_or_where_between_and_or_where_not_between() -> None:
    qb = (
        Query.table("users")
        .where("active", True)
        .or_where_between("age", [18, 30])
        .or_where_not_between("score", [0, 50])
    )
    sql, bindings = qb.to_sql()
    assert (
        'WHERE "active" = $1 OR "age" BETWEEN $2 AND $3 OR "score" NOT BETWEEN $4 AND $5'
        in sql
    )
    assert bindings == [True, 18, 30, 0, 50]


def test_where_between_invalid_sequence() -> None:
    with pytest.raises(QueryError, match="exactly two elements"):
        Query.table("users").where_between("age", [18])

    with pytest.raises(QueryError, match="exactly two elements"):
        Query.table("users").where_between("age", [18, 25, 30])


def test_where_null_and_or_where_null() -> None:
    qb = Query.table("users").where_null("deleted_at").or_where_null("banned_at")
    sql, bindings = qb.to_sql()
    assert 'WHERE "deleted_at" IS NULL OR "banned_at" IS NULL' in sql
    assert bindings == []


def test_where_not_null_and_or_where_not_null() -> None:
    qb = (
        Query.table("users")
        .where_not_null("verified_at")
        .or_where_not_null("activated_at")
    )
    sql, bindings = qb.to_sql()
    assert 'WHERE "verified_at" IS NOT NULL OR "activated_at" IS NOT NULL' in sql
    assert bindings == []


def test_where_raw_and_or_where_raw() -> None:
    qb = (
        Query.table("metrics")
        .where_raw("cpu_usage > ?", [80])
        .or_where_raw("mem_usage > ?", [90])
    )
    sql, bindings = qb.to_sql()
    assert "WHERE cpu_usage > $1 OR mem_usage > $2" in sql
    assert bindings == [80, 90]


def test_distinct_and_distinct_on() -> None:
    qb1 = Query.table("users").distinct().select("email")
    sql1, bindings1 = qb1.to_sql()
    assert 'SELECT DISTINCT "email" FROM "users"' in sql1
    assert bindings1 == []

    qb2 = Query.table("orders").distinct("customer_id").select("customer_id", "total")
    sql2, bindings2 = qb2.to_sql()
    assert 'SELECT DISTINCT ON ("customer_id") "customer_id", "total" FROM "orders"' in sql2
    assert bindings2 == []


def test_latest_and_oldest() -> None:
    qb_latest = Query.table("posts").latest()
    sql_latest, _ = qb_latest.to_sql()
    assert 'ORDER BY "created_at" DESC' in sql_latest

    qb_custom_latest = Query.table("posts").latest("published_at")
    sql_custom, _ = qb_custom_latest.to_sql()
    assert 'ORDER BY "published_at" DESC' in sql_custom

    qb_oldest = Query.table("posts").oldest()
    sql_oldest, _ = qb_oldest.to_sql()
    assert 'ORDER BY "created_at" ASC' in sql_oldest


def test_in_random_order() -> None:
    qb = Query.table("quotes").in_random_order()
    sql, _ = qb.to_sql()
    assert "ORDER BY RANDOM()" in sql

    # Reordering overrides RANDOM()
    qb_reordered = qb.order_by("id", "ASC")
    sql_reordered, _ = qb_reordered.to_sql()
    assert 'ORDER BY "id" ASC' in sql_reordered
    assert "RANDOM()" not in sql_reordered


def test_when_and_unless_conditionals() -> None:
    filter_role = True
    qb = (
        Query.table("users")
        .when(filter_role, lambda q, val: q.where("role", "admin"))
        .when(False, lambda q, val: q.where("is_active", True))
        .unless(False, lambda q, val: q.where("country", "US"))
        .unless(True, lambda q, val: q.where("country", "CA"))
    )
    sql, bindings = qb.to_sql()
    assert 'WHERE "role" = $1 AND "country" = $2' in sql
    assert bindings == ["admin", "US"]

    # When with default callback
    qb_default = Query.table("items").when(
        False,
        lambda q, _: q.where("type", "A"),
        default=lambda q, _: q.where("type", "B"),
    )
    sql_d, bindings_d = qb_default.to_sql()
    assert 'WHERE "type" = $1' in sql_d
    assert bindings_d == ["B"]


@pytest.mark.asyncio
async def test_first_or_fail_found_and_not_found(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    qb = Query.table("users").where("id", 1)

    # Found case
    mock_conn.fetch = AsyncMock(return_value=[{"id": 1, "name": "Alice"}])
    result = await qb.first_or_fail()
    assert result == {"id": 1, "name": "Alice"}

    # Not found case
    mock_conn.fetch = AsyncMock(return_value=[])
    with pytest.raises(RecordNotFoundError, match="No query results for table \\[users\\]"):
        await qb.first_or_fail()


@pytest.mark.asyncio
async def test_value_and_pluck(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    qb = Query.table("users")
    mock_conn.fetch = AsyncMock(
        return_value=[
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ]
    )

    # Value
    single_val = await qb.value("name")
    assert single_val == "Alice"

    # Pluck list
    names = await qb.pluck("name")
    assert names == ["Alice", "Bob"]

    # Pluck dict with key
    mapping = await qb.pluck("name", key="id")
    assert mapping == {1: "Alice", 2: "Bob"}


@pytest.mark.asyncio
async def test_increment_and_decrement(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    qb = Query.table("articles").where("id", 42)
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")

    # Increment
    status = await qb.increment("views", 5)
    assert status == "UPDATE 1"
    exec_sql, *exec_args = mock_conn.execute.call_args[0]
    assert 'UPDATE "articles" SET "views" = "views" + $1 WHERE "id" = $2' in exec_sql
    assert exec_args == [5, 42]

    # Decrement with extra fields
    status_dec = await qb.decrement("stock", 2, extra={"status": "in_stock"})
    assert status_dec == "UPDATE 1"
    exec_sql2, *exec_args2 = mock_conn.execute.call_args[0]
    assert 'SET "stock" = "stock" - $1, "status" = $2 WHERE "id" = $3' in exec_sql2
    assert exec_args2 == [2, "in_stock", 42]

    # Guard against negative increment
    with pytest.raises(ValueError, match="must be non-negative"):
        await qb.increment("views", -1)

    # Guard against non-numeric
    with pytest.raises(TypeError, match="must be a numeric"):
        await qb.increment("views", json.loads('"five"'))

    # Guard against unconstrained mass update
    qb_unguarded = Query.table("articles")
    with pytest.raises(QueryError, match="Refusing to execute increment without a WHERE clause"):
        await qb_unguarded.increment("views", 1)


@pytest.mark.asyncio
async def test_chunk_and_chunk_by_id(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    qb = Query.table("logs")

    # Simulate 5 items across chunk size 2
    mock_pages = [
        [{"id": 1, "msg": "a"}, {"id": 2, "msg": "b"}],
        [{"id": 3, "msg": "c"}, {"id": 4, "msg": "d"}],
        [{"id": 5, "msg": "e"}],
        [],
    ]
    page_iter = iter(mock_pages)

    mock_conn.fetch = AsyncMock(side_effect=lambda *args, **kwargs: next(page_iter))

    collected: list[int] = []

    async def process_batch(items: list[dict[str, Any]]) -> None:
        for item in items:
            collected.append(item["id"])

    finished = await qb.chunk(2, process_batch)
    assert finished is True
    assert collected == [1, 2, 3, 4, 5]

    # Early exit when callback returns False
    page_iter2 = iter(mock_pages)
    mock_conn.fetch = AsyncMock(side_effect=lambda *args, **kwargs: next(page_iter2))
    collected2: list[int] = []

    async def abort_early(items: list[dict[str, Any]]) -> bool:
        for item in items:
            collected2.append(item["id"])
        return False  # Stop after first chunk

    finished_early = await qb.chunk(2, abort_early)
    assert finished_early is False
    assert collected2 == [1, 2]


@pytest.mark.asyncio
async def test_upsert_compilation_and_execution(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    qb = Query.table("metrics")
    mock_conn.execute = AsyncMock(return_value="INSERT 0 2")

    rows = [
        {"metric_id": 1, "date": "2026-11-11", "val": 100},
        {"metric_id": 2, "date": "2026-11-11", "val": 200},
    ]

    affected = await qb.upsert(rows, unique_by=["metric_id", "date"], update_columns=["val"])
    assert affected == 2

    exec_sql, *exec_args = mock_conn.execute.call_args[0]
    assert 'INSERT INTO "metrics" ("metric_id", "date", "val")' in exec_sql
    assert "VALUES ($1, $2, $3), ($4, $5, $6)" in exec_sql
    assert 'ON CONFLICT ("metric_id", "date") DO UPDATE SET "val" = EXCLUDED."val"' in exec_sql
    assert exec_args == [1, "2026-11-11", 100, 2, "2026-11-11", 200]


def test_where_group_and_or_where_group_precedence() -> None:
    query = (
        Query.table("users")
        .where("status", "active")
        .where_group(
            lambda q: (
                q.where("role", "admin")
                .or_where("role", "moderator")
            )
        )
        .where("age", ">=", 18)
    )
    sql, bindings = query.to_sql()
    assert 'WHERE "status" = $1 AND ("role" = $2 OR "role" = $3) AND "age" >= $4' in sql
    assert bindings == ["active", "admin", "moderator", 18]

    # or_where_group
    query_or = (
        Query.table("users")
        .where("status", "active")
        .or_where_group(
            lambda q: q.where("points", ">", 100).where("tier", "vip")
        )
    )
    sql_or, bindings_or = query_or.to_sql()
    assert 'WHERE "status" = $1 OR ("points" > $2 AND "tier" = $3)' in sql_or
    assert bindings_or == ["active", 100, "vip"]


def test_deeply_nested_where_groups_with_raw_and_mixed_connectors() -> None:
    query = (
        Query.table("audit_logs")
        .where("severity", "error")
        .where_group(
            lambda q1: q1.where("service", "billing").or_where_group(
                lambda q2: q2.where("service", "auth").where_raw("error_code LIKE ?", ["AUTH_%"])
            )
        )
        .or_where("override", True)
    )
    sql, bindings = query.to_sql()
    assert (
        'WHERE "severity" = $1 AND ("service" = $2 OR ("service" = $3 AND error_code LIKE $4)) OR "override" = $5'
        in sql
    )
    assert bindings == ["error", "billing", "auth", "AUTH_%", True]


def test_empty_where_group_is_omitted() -> None:
    # 1. Group callback adds nothing
    q1 = Query.table("users").where("active", True).where_group(lambda q: None)
    sql1, bindings1 = q1.to_sql()
    assert sql1 == 'SELECT * FROM "users" WHERE "active" = $1'
    assert bindings1 == [True]

    # 2. Only an empty group exists
    q2 = Query.table("users").where_group(lambda q: None)
    sql2, bindings2 = q2.to_sql()
    assert sql2 == 'SELECT * FROM "users"'
    assert bindings2 == []

    # 3. Empty group between two where clauses does not leave dangling AND/OR
    q3 = (
        Query.table("users")
        .where("a", 1)
        .where_group(lambda q: None)
        .where("b", 2)
    )
    sql3, bindings3 = q3.to_sql()
    assert sql3 == 'SELECT * FROM "users" WHERE "a" = $1 AND "b" = $2'
    assert bindings3 == [1, 2]


def test_where_in_empty_list_compiles_to_false() -> None:
    qb = Query.table("users").where_in("id", [])
    sql, bindings = qb.to_sql()
    assert 'WHERE FALSE' in sql
    assert bindings == []

    qb_chained = Query.table("users").where("status", "active").or_where_in("id", [])
    sql2, bindings2 = qb_chained.to_sql()
    assert 'WHERE "status" = $1 OR FALSE' in sql2
    assert bindings2 == ["active"]


def test_where_not_in_empty_list_compiles_to_true() -> None:
    qb = Query.table("users").where_not_in("id", [])
    sql, bindings = qb.to_sql()
    assert 'WHERE TRUE' in sql
    assert bindings == []

    qb_chained = Query.table("users").where("status", "active").where_not_in("id", [])
    sql2, bindings2 = qb_chained.to_sql()
    assert 'WHERE "status" = $1 AND TRUE' in sql2
    assert bindings2 == ["active"]


def test_where_raw_placeholder_mismatch() -> None:
    with pytest.raises(QueryError, match="Placeholder count mismatch in where_raw"):
        Query.table("users").where_raw("age > ? AND score > ?", [18])

    with pytest.raises(QueryError, match="Placeholder count mismatch in where_raw"):
        Query.table("users").where_raw("active = true", [True])


def test_upsert_validation_and_duplicate_conflict_keys() -> None:
    # 1. Duplicate conflict keys in same batch
    qb = Query.table("users")
    batch_with_duplicates = [
        {"email": "dup@example.com", "name": "One"},
        {"email": "dup@example.com", "name": "Two"},
    ]
    with pytest.raises(QueryError, match="Duplicate conflict key .* detected at row 1"):
        asyncio.run(qb.upsert(batch_with_duplicates, unique_by="email"))

    # 2. Key mismatch across rows
    mismatched_batch = [
        {"email": "a@example.com", "name": "A"},
        {"email": "b@example.com", "other": "B"},
    ]
    with pytest.raises(QueryError, match="mismatched keys"):
        asyncio.run(qb.upsert(mismatched_batch, unique_by="email"))

    # 3. Missing conflict key in row data
    with pytest.raises(QueryError, match="Conflict key column 'uuid' is not present"):
        asyncio.run(qb.upsert([{"email": "a@example.com"}], unique_by="uuid"))


@pytest.mark.asyncio
async def test_upsert_do_nothing_when_update_columns_empty(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    mock_conn.execute = AsyncMock(return_value="INSERT 0 0")
    qb = Query.table("tags")

    affected = await qb.upsert(
        [{"name": "python"}, {"name": "asyncpg"}],
        unique_by="name",
        update_columns=[],
    )
    assert affected == 0
    exec_sql, *_ = mock_conn.execute.call_args[0]
    assert 'ON CONFLICT ("name") DO NOTHING' in exec_sql


@pytest.mark.asyncio
async def test_increment_and_decrement_extended_validations(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    qb = Query.table("accounts").where("id", 1)

    # 1. NaN and Infinity rejected
    with pytest.raises(ValueError, match="cannot be NaN or Infinity"):
        await qb.increment("balance", float("nan"))

    with pytest.raises(ValueError, match="cannot be NaN or Infinity"):
        await qb.decrement("balance", float("inf"))

    # 2. Target column passed in extra rejected
    with pytest.raises(QueryError, match="cannot be passed in extra"):
        await qb.increment("balance", 10, extra={"balance": 50})

    # 3. Invalid identifier in extra rejected
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        await qb.increment("balance", 10, extra={"invalid-col; DROP TABLE": 1})

    # 4. Zero increment/decrement allowed
    res_zero = await qb.increment("balance", 0)
    assert res_zero == "UPDATE 1"

    # 5. Decimal amount allowed
    res_dec = await qb.decrement("balance", 12.75)
    assert res_dec == "UPDATE 1"


@pytest.mark.asyncio
async def test_chunk_preserves_query_cloning_without_limit_offset_interference(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    # Create a query with pre-existing LIMIT and OFFSET
    qb = Query.table("items").limit(10).offset(5)

    mock_pages = [
        [{"id": 1}, {"id": 2}],
        [{"id": 3}],
        [],
    ]
    page_iter = iter(mock_pages)
    mock_conn.fetch = AsyncMock(side_effect=lambda *args, **kwargs: next(page_iter))

    visited: list[int] = []

    # Synchronous callback returning None
    def sync_visitor(batch: list[dict[str, Any]]) -> None:
        for r in batch:
            visited.append(r["id"])

    finished = await qb.chunk(2, sync_visitor)
    assert finished is True
    assert visited == [1, 2, 3]

    # Pre-existing limit/offset on qb must remain completely untouched
    assert qb._limit_value == 10
    assert qb._offset_value == 5

    # chunk_by_id missing ID column raises QueryError
    mock_conn.fetch = AsyncMock(return_value=[{"code": "XYZ"}])
    with pytest.raises(QueryError, match="not present in query projection for chunk_by_id"):
        await Query.table("items").chunk_by_id(2, sync_visitor, column="id")
