from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from swiftpy import Application
from swiftpy.core.config import Config
from swiftpy.core.container import (
    Container,
    bind_container,
    get_container,
    set_container,
)
from swiftpy.database import QueryDumpError
from swiftpy.database.identifiers import split_json_path
from swiftpy.database.orm.model import Model, ModelQueryBuilder
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import (
    Query,
    QueryBuilder,
    QueryError,
    _substitute_bindings,
)
from swiftpy.database.transaction import (
    _active_connection,
    db,
    get_active_connection,
)


class _CustomDebugObject:
    def __str__(self) -> str:
        return "custom-debug'object"


async def _open_pg_pool(pool: DatabasePool) -> DatabasePool:
    try:
        await pool.init()
    except (OSError, RuntimeError, TimeoutError) as exc:
        pytest.skip(f"PostgreSQL service is not available in local test environment: {exc}")
    return pool


@pytest.fixture
def ensure_db_pool() -> None:
    """Disable root autouse DB initialization for unit and mocked tests in this module."""
    return


@pytest_asyncio.fixture
async def pg_pool() -> AsyncGenerator[DatabasePool, None]:
    """Explicit PostgreSQL pool fixture used exclusively by integration tests."""
    try:
        prev_container: Container | None = get_container()
    except RuntimeError:
        prev_container = None
    prev_config = Config._instance
    app = Application()
    pool = app.container.resolve(DatabasePool)
    try:
        await _open_pg_pool(pool)
        yield pool
    finally:
        await pool.close()
        if prev_container is not None:
            set_container(prev_container)
        Config._instance = prev_config


@pytest.fixture
def mock_db_pool() -> Generator[tuple[AsyncMock, MagicMock], None, None]:
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_conn)
    mock_pool.release = AsyncMock()
    mock_db = MagicMock(spec=DatabasePool)
    mock_db.get_pool = MagicMock(return_value=mock_pool)

    isolated_container = Container()
    isolated_container.instance(DatabasePool, mock_db)
    with bind_container(isolated_container):
        yield mock_conn, mock_pool


class HelperUser(Model):
    __table__ = "helper_users"
    __guarded__: ClassVar[list[str]] = []
    id: int
    name: str
    email: str


class HelperOrder(Model):
    __table__ = "helper_orders"
    __guarded__: ClassVar[list[str]] = []
    id: int
    user_id: int


class PgV036JsonItem(Model):
    __table__ = "swiftpy_v036_json_items"
    __guarded__: ClassVar[list[str]] = []
    id: int
    label: str
    metadata: dict[str, Any] | list[Any] | None = None
    legacy: dict[str, Any] | list[Any] | None = None


class PgV036LockItem(Model):
    __table__ = "swiftpy_v036_lock_items"
    __guarded__: ClassVar[list[str]] = []
    id: int
    status: str
    balance: int


# ---------------------------------------------------------------------------
# Infrastructure isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_pool_does_not_swallow_postgres_errors() -> None:
    bad_pool = MagicMock(spec=DatabasePool)
    bad_pool.init = AsyncMock(side_effect=asyncpg.InvalidPasswordError("bad password"))
    with pytest.raises(asyncpg.InvalidPasswordError, match="bad password"):
        await _open_pg_pool(bad_pool)

    unavailable_pool = MagicMock(spec=DatabasePool)
    unavailable_pool.init = AsyncMock(side_effect=ConnectionRefusedError("refused"))
    with pytest.raises(pytest.skip.Exception, match="not available"):
        await _open_pg_pool(unavailable_pool)


@pytest.mark.asyncio
async def test_mock_db_pool_setup_uses_isolated_container(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    mock_conn.fetch = AsyncMock(return_value=[])
    assert await Query.table("helper_users").get() == []
    assert isinstance(get_container().get_singleton(DatabasePool), MagicMock)


def test_mock_db_pool_does_not_leak_into_subsequent_tests() -> None:
    try:
        container = get_container()
    except RuntimeError:
        container = None
    if container is not None:
        singleton = container.get_singleton(DatabasePool)
        assert not isinstance(singleton, MagicMock)


# ---------------------------------------------------------------------------
# JSON path parser, injection, and boundary tests
# ---------------------------------------------------------------------------


def test_split_json_path_accepts_and_rejects_strict_forms() -> None:
    assert split_json_path("users.options->meal->0") == (
        "users.options",
        ("meal", "0"),
    )
    assert split_json_path("public.users.options->a") == (
        "public.users.options",
        ("a",),
    )
    assert split_json_path("options", require_path=False) == ("options", ())
    assert split_json_path("options->0") == ("options", ("0",))
    assert split_json_path("options->42") == ("options", ("42",))
    assert split_json_path("a" * 63 + "->" + "x" * 63) == ("a" * 63, ("x" * 63,))
    assert split_json_path("a" + "->x" * 16)[1] == ("x",) * 16

    invalid_expressions = (
        "",
        "   ",
        "options",
        "a->",
        "->a",
        "a->->b",
        "a->>b",
        "a -> b",
        "a->b->",
        'a->"b"',
        "a->b;drop",
        "a->-1",
        "a->01",
        "a->00",
        "a->x\n",
        "a->x\r\n",
        "a->x\x00",
        "a->*",
        "users.*->a",
        "a->" + "x" * 64,
        "a" * 64 + "->x",
        "a" + "->x" * 17,
        "options->a'; DROP TABLE users; --",
        "options->a OR 1=1",
        "options->a/*comment*/",
        "options->a--comment",
        "options->$1",
        "options->(a)",
        "options->a\\b",
    )
    for expr in invalid_expressions:
        with pytest.raises(ValueError, match="Invalid JSON path expression"):
            split_json_path(expr)

    with pytest.raises(TypeError, match="must be a string"):
        split_json_path(cast(Any, 123))
    with pytest.raises(TypeError, match="must be a string"):
        split_json_path(cast(Any, None))


def test_json_path_injection_and_boundary_rejections() -> None:
    with pytest.raises(QueryError, match="Unsupported operator"):
        Query.table("settings").where("data->a", "= 1 OR 1=1", "x")

    for bad_op_val in (None, True, ["a"], {"a": 1}, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(QueryError):
            Query.table("settings").where("data->score", ">", bad_op_val)

    for bad_like_val in (None, 123, True, ["a"]):
        with pytest.raises(QueryError, match="string value"):
            Query.table("settings").where("data->label", "LIKE", bad_like_val)

    with pytest.raises(QueryError, match="JSON scalars"):
        Query.table("settings").where_in("data->a", Query.table("other"))
    with pytest.raises(QueryError, match="JSON scalars"):
        Query.table("settings").where_in("data->a", [{"nested": 1}])
    with pytest.raises(QueryError, match="serializable"):
        Query.table("settings").where_in("data->a", [float("nan")])

    payload = "'; DROP TABLE users; -- $1"
    sql, bindings = Query.table("settings").where("data->a", payload).to_sql()
    assert "DROP TABLE" not in sql
    assert bindings == [["a"], json.dumps(payload)]

    with pytest.raises(QueryError, match="non-negative integer"):
        Query.table("settings").where_json_length("data->tags", -1)
    with pytest.raises(QueryError, match="non-negative integer"):
        Query.table("settings").where_json_length("data->tags", True)
    with pytest.raises(QueryError, match="non-negative integer"):
        Query.table("settings").where_json_length("data->tags", "2")
    with pytest.raises(QueryError, match="Unsupported operator"):
        Query.table("settings").where_json_length("data->tags", "LIKE", 2)
    with pytest.raises(ValueError, match="boolean operator must be"):
        Query.table("settings").where_json_contains("data", {"a": 1}, boolean="XOR")
    with pytest.raises(ValueError, match="Invalid JSON path expression"):
        Query.table("settings").where_json_contains_key("data")


# ---------------------------------------------------------------------------
# where_column, where_exists, where_past, where_future tests
# ---------------------------------------------------------------------------


def test_where_column_exists_groups_and_time_helpers_compile() -> None:
    query = (
        Query.table("users")
        .where("active", True)
        .where_column("users.id", "=", "orders.user_id")
        .or_where_exists(
            Query.table("orders")
            .where_column("orders.user_id", "=", "users.id")
            .where("status", "paid")
        )
        .where_future("created_at")
    )
    sql, bindings = query.to_sql()
    assert (
        'WHERE "active" = $1 AND "users"."id" = "orders"."user_id" '
        'OR EXISTS (SELECT 1 FROM "orders" WHERE "orders"."user_id" = "users"."id" '
        'AND "status" = $2) AND "created_at" > CURRENT_TIMESTAMP'
    ) in sql
    assert bindings == [True, "paid"]

    for operator in ("=", "!=", "<>", "<", ">", "<=", ">="):
        col_sql, col_bindings = (
            Query.table("users")
            .where_column("left_id", operator, "right_id")
            .to_sql()
        )
        assert f'"left_id" {operator} "right_id"' in col_sql
        assert col_bindings == []

    or_col_sql, _ = (
        Query.table("users")
        .where("active", True)
        .or_where_column("left_id", "<>", "right_id")
        .to_sql()
    )
    assert 'WHERE "active" = $1 OR "left_id" <> "right_id"' in or_col_sql

    past_sql, _ = (
        Query.table("jobs")
        .where("enabled", True)
        .where_past("due_at")
        .or_where_past("jobs.expired_at")
        .or_where_future("jobs.starts_at")
        .to_sql()
    )
    assert (
        'WHERE "enabled" = $1 AND "due_at" < CURRENT_TIMESTAMP '
        'OR "jobs"."expired_at" < CURRENT_TIMESTAMP '
        'OR "jobs"."starts_at" > CURRENT_TIMESTAMP'
    ) in past_sql

    complex_sql, complex_bindings = (
        Query.table("users")
        .where("state", "active")
        .where_not_exists(
            Query.table("orders")
            .where_column("orders.user_id", "=", "users.id")
            .where("total", ">", 100)
        )
        .or_where_exists(Query.table("returns").where("approved", True))
        .or_where_not_exists(Query.table("cancellations").where("reason", "duplicate"))
        .where("id", 7)
        .to_sql()
    )
    assert 'NOT EXISTS (SELECT 1 FROM "orders"' in complex_sql
    assert 'OR EXISTS (SELECT 1 FROM "returns"' in complex_sql
    assert 'OR NOT EXISTS (SELECT 1 FROM "cancellations"' in complex_sql
    assert complex_bindings == ["active", 100, True, "duplicate", 7]

    grouped, grouped_bindings = Query.table("users").where_group(
        lambda builder: builder.where_exists(
            Query.table("orders").where_column("orders.user_id", "=", "users.id")
        ).or_where("name", "Ada")
    ).to_sql()
    assert 'WHERE (EXISTS (SELECT 1 FROM "orders" WHERE' in grouped
    assert grouped_bindings == ["Ada"]


def test_exists_snapshots_subquery_and_rejects_invalid_inputs() -> None:
    subquery = Query.table("orders").where("status", "paid")
    query = Query.table("users").where_exists(subquery)
    before, before_bindings = query.to_sql()
    subquery.where("cancelled", False)
    assert query.to_sql() == (before, before_bindings)

    cloned = query.clone()
    cloned.where("id", 5)
    assert query.to_sql() == (before, before_bindings)

    with pytest.raises(TypeError):
        Query.table("users").where_exists(cast(Any, "not a query"))
    with pytest.raises(QueryError, match="locked"):
        Query.table("users").where_exists(Query.table("orders").lock_for_update())
    with pytest.raises(QueryError, match="different table"):
        Query.table("users").where_exists(Query.table("users").where("id", 1))
    with pytest.raises(ValueError, match="boolean operator must be"):
        Query.table("users").where_exists(Query.table("orders"), boolean="XOR")

    with pytest.raises(ValueError):
        Query.table("users").where_column("id", "=", "other;drop")
    with pytest.raises(ValueError):
        Query.table("users").where_column("a;b", "=", "id")
    with pytest.raises(TypeError, match="operator must be a string"):
        Query.table("users").where_column("id", cast(Any, 1), "other_id")
    with pytest.raises(QueryError, match="Unsupported operator"):
        Query.table("users").where_column("id", "LIKE", "other_id")
    with pytest.raises(ValueError, match="boolean operator must be"):
        Query.table("users").where_column("id", "=", "other_id", boolean="NAND")

    with pytest.raises(ValueError):
        Query.table("users").where_future("a b")
    with pytest.raises(ValueError, match="boolean operator must be"):
        Query.table("users").where_past("created_at", boolean="XOR")

    union_subquery = (
        Query.table("orders")
        .select("id", "status")
        .union(Query.table("archived_orders").select("id", "status"))
    )
    union_sql, _ = Query.table("users").where_exists(union_subquery).to_sql()
    assert 'EXISTS ((SELECT 1 FROM "orders") UNION (SELECT 1 FROM "archived_orders"))' in union_sql


@pytest.mark.asyncio
async def test_exists_satisfies_write_where_guard(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")

    await (
        Query.table("users")
        .where_exists(Query.table("orders").where("paid", True))
        .update({"active": True})
    )
    update_sql, *update_bindings = mock_conn.execute.await_args.args
    assert 'UPDATE "users" SET "active" = $1 WHERE EXISTS (SELECT 1 FROM "orders" WHERE "paid" = $2)' == update_sql
    assert update_bindings == [True, True]

    await (
        Query.table("users")
        .where_not_exists(Query.table("orders").where("paid", False))
        .delete()
    )
    delete_sql, *delete_bindings = mock_conn.execute.await_args.args
    assert 'DELETE FROM "users" WHERE NOT EXISTS (SELECT 1 FROM "orders" WHERE "paid" = $1)' == delete_sql
    assert delete_bindings == [False]


# ---------------------------------------------------------------------------
# where_any / where_all / where_none tests
# ---------------------------------------------------------------------------


def test_where_any_all_none_operators_grouping_and_cloning() -> None:
    # 1. IN and NOT IN (including empty lists)
    in_sql, in_bindings = (
        Query.table("users")
        .where_any(["role", "backup_role"], "IN", ["admin", "owner"])
        .where_all(["tier", "plan"], "NOT IN", ["free"])
        .where_none(["flag_a", "flag_b"], "IN", [])
        .to_sql()
    )
    assert in_sql == (
        'SELECT * FROM "users" WHERE ("role" IN ($1, $2) OR "backup_role" IN ($3, $4)) '
        'AND ("tier" NOT IN ($5) AND "plan" NOT IN ($6)) '
        "AND NOT (FALSE OR FALSE)"
    )
    assert in_bindings == ["admin", "owner", "admin", "owner", "free", "free"]

    # 2. None values produce IS NULL / IS NOT NULL per column
    null_sql, null_bindings = (
        Query.table("users")
        .where_any(["deleted_at", "archived_at"], None)
        .or_where_all(["verified_at", "activated_at"], "!=", None)
        .or_where_none(["locked_at", "banned_at"], None)
        .to_sql()
    )
    assert null_sql == (
        'SELECT * FROM "users" WHERE ("deleted_at" IS NULL OR "archived_at" IS NULL) '
        'OR ("verified_at" IS NOT NULL AND "activated_at" IS NOT NULL) '
        'OR NOT ("locked_at" IS NULL OR "banned_at" IS NULL)'
    )
    assert null_bindings == []

    # 3. Nested combinations with where_group, empty groups, and mixed AND/OR precedence
    nested = (
        Query.table("users")
        .where("tenant_id", 10)
        .where_group(
            lambda g: g.where_group(lambda empty: None)
            .where_any(["name", "email"], "like", "Ada%")
            .or_where_all(["active", "verified"], True)
            .where_group(lambda empty_two: empty_two.where_group(lambda deep: None))
        )
        .or_where_none(["suspended", "deleted"], True)
        .where("archived", False)
    )
    nested_sql, nested_bindings = nested.to_sql()
    assert nested_sql == (
        'SELECT * FROM "users" WHERE "tenant_id" = $1 '
        'AND (("name" LIKE $2 ESCAPE \'\\\' OR "email" LIKE $3 ESCAPE \'\\\') '
        'OR ("active" = $4 AND "verified" = $5)) '
        'OR NOT ("suspended" = $6 OR "deleted" = $7) '
        'AND "archived" = $8'
    )
    assert nested_bindings == [10, "Ada%", "Ada%", True, True, True, True, False]

    # 4. Empty nested group alone is cleanly omitted
    empty_only = (
        Query.table("users")
        .where("a", 1)
        .where_group(lambda g: g.where_group(lambda inner: None))
        .where_all(["b", "c"], 2)
    )
    assert empty_only.to_sql() == (
        'SELECT * FROM "users" WHERE "a" = $1 AND ("b" = $2 AND "c" = $3)',
        [1, 2, 2],
    )

    # 5. Cloning after adding groups stays independent
    cloned = nested.clone()
    cloned.or_where_any(["nickname", "handle"], "ada")
    assert nested.to_sql() == (nested_sql, nested_bindings)
    cloned_sql, cloned_bindings = cloned.to_sql()
    assert 'OR ("nickname" = $9 OR "handle" = $10)' in cloned_sql
    assert cloned_bindings == [*nested_bindings, "ada", "ada"]

    # 6. Negative & validation cases (atomic validation: failed call does not mutate builder)
    base = Query.table("users").where("id", 1)
    before_err = base.to_sql()
    with pytest.raises(QueryError, match="Cannot compare NULL"):
        base.where_any(["a", "b"], "<", None)
    with pytest.raises(QueryError, match="Cannot compare NULL"):
        base.where_all(["a", "b"], "LIKE", None)
    with pytest.raises(QueryError, match="must not be empty"):
        base.where_any([], "x")
    with pytest.raises(TypeError, match="list or tuple"):
        base.where_any(cast(Any, "email"), "x")
    with pytest.raises(TypeError, match="Every column"):
        base.where_any(["email", cast(Any, 1)], "x")
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        base.where_all(["email", "bad;col"], "x")
    with pytest.raises(QueryError, match="Unsupported operator"):
        base.where_none(["email"], "MATCH", "x")
    with pytest.raises(ValueError, match="boolean operator must be"):
        base.where_all(["email"], "x", boolean="XOR")
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        base.where_group(lambda g: g.where_any(["bad;col"], 1))
    assert base.to_sql() == before_err


# ---------------------------------------------------------------------------
# JSON compilation, encoding, and update tests
# ---------------------------------------------------------------------------


def test_json_where_and_helpers_compile_complete_matrix() -> None:
    query = (
        Query.table("users")
        .where("settings->theme", "dark")
        .where("settings->count", 42)
        .where("settings->enabled", True)
        .where("settings->nullable", None)
        .where("settings->nullable", "!=", None)
        .where("settings->flag", "<>", False)
        .where("settings->score", ">=", 2.5)
        .where("settings->label", "<", "z")
        .where("settings->label", "ilike", "%ada%")
        .where_in("settings->enabled", [True, False, None])
        .or_where_not_in("users.settings->tier", ["free", "trial"])
        .where_json_contains("settings", {"theme": "dark"})
        .or_where_json_contains("settings->roles", "editor")
        .where_json_doesnt_contain("settings->missing", "x")
        .or_where_json_doesnt_contain("settings->tags", ["banned"])
        .where_json_contains_key("settings->theme")
        .or_where_json_contains_key("settings->tags->0")
        .where_json_doesnt_contain_key("settings->missing")
        .or_where_json_doesnt_contain_key("settings->gone->deep")
        .where_json_length("settings->tags", ">=", 2)
        .or_where_json_length("settings->items", 0)
    )
    sql, bindings = query.to_sql()
    assert '("settings"::jsonb #> $1::text[]) = $2::jsonb' in sql
    assert "COALESCE(jsonb_typeof((\"settings\"::jsonb #> $7::text[])), 'null') = 'null'" in sql
    assert "COALESCE(jsonb_typeof((\"settings\"::jsonb #> $8::text[])), 'null') <> 'null'" in sql
    assert '("settings"::jsonb #> $9::text[]) <> $10::jsonb' in sql
    assert "CASE WHEN jsonb_typeof((\"settings\"::jsonb #> $11::text[])) = 'number' THEN ((\"settings\"::jsonb #>> $11::text[]))::numeric END >= $12" in sql
    assert "CASE WHEN jsonb_typeof((\"settings\"::jsonb #> $13::text[])) = 'string' THEN (\"settings\"::jsonb #>> $13::text[]) END < $14" in sql
    assert "CASE WHEN jsonb_typeof((\"settings\"::jsonb #> $15::text[])) = 'string' THEN \"settings\"::jsonb #>> $15::text[] END ILIKE $16 ESCAPE '\\'" in sql
    assert '("settings"::jsonb #> $17::text[]) IN ($18::jsonb, $19::jsonb, $20::jsonb)' in sql
    assert 'OR ("users"."settings"::jsonb #> $21::text[]) NOT IN ($22::jsonb, $23::jsonb)' in sql
    assert '"settings"::jsonb @> $24::jsonb' in sql
    assert 'OR ("settings"::jsonb #> $25::text[]) @> $26::jsonb' in sql
    assert 'NOT COALESCE(("settings"::jsonb #> $27::text[]) @> $28::jsonb, FALSE)' in sql
    assert 'AND ("settings"::jsonb #> $31::text[]) IS NOT NULL' in sql
    assert 'OR ("settings"::jsonb #> $32::text[]) IS NOT NULL' in sql
    assert '("settings"::jsonb #> $33::text[]) IS NULL' in sql
    assert 'OR ("settings"::jsonb #> $34::text[]) IS NULL' in sql
    assert 'CASE WHEN jsonb_typeof(("settings"::jsonb #> $35::text[])) = \'array\' THEN jsonb_array_length(("settings"::jsonb #> $35::text[])) END >= $36' in sql
    assert 'OR CASE WHEN jsonb_typeof(("settings"::jsonb #> $37::text[])) = \'array\' THEN jsonb_array_length(("settings"::jsonb #> $37::text[])) END = $38' in sql
    assert len(bindings) == 38


def test_json_null_empty_in_and_value_encoding_rules() -> None:
    sql, bindings = Query.table("settings").where("data->x", None).to_sql()
    assert "COALESCE(jsonb_typeof((\"data\"::jsonb #> $1::text[])), 'null') = 'null'" in sql
    assert bindings == [["x"]]

    sql_in_empty, bindings_in_empty = Query.table("settings").where_in("data->x", []).to_sql()
    assert sql_in_empty.endswith("WHERE FALSE")
    assert bindings_in_empty == []

    sql_not_in_empty, bindings_not_in_empty = (
        Query.table("settings").where("id", 1).or_where_not_in("data->x", []).to_sql()
    )
    assert sql_not_in_empty == 'SELECT * FROM "settings" WHERE "id" = $1 OR TRUE'
    assert bindings_not_in_empty == [1]

    for invalid_val in (
        Decimal("1.2"),
        datetime(2026, 1, 1, tzinfo=UTC),
        {"bad_set": {1, 2}},
        float("nan"),
        float("inf"),
        float("-inf"),
    ):
        with pytest.raises(QueryError, match="serializable"):
            Query.table("settings").where("data->x", invalid_val)
        with pytest.raises(QueryError, match="serializable"):
            Query.table("settings").where_json_contains("data", invalid_val)

    source = {"enabled": True}
    captured = Query.table("settings").where_json_contains("data", source)
    source["enabled"] = False
    _, captured_bindings = captured.to_sql()
    assert captured_bindings == ['{"enabled":true}']


@pytest.mark.asyncio
async def test_json_update_compiles_one_bound_statement(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    query = Query.table("settings").where("id", 9)

    await query.update(
        {
            "data->enabled": True,
            "name": "updated",
            "data->nested->count": 3,
            "data->cleared": None,
            "extra->flag": "yes",
        }
    )
    assert mock_conn.execute.await_count == 1
    sql, *bindings = mock_conn.execute.await_args.args
    assert (
        '"data" = jsonb_set(jsonb_set(jsonb_set(COALESCE("data"::jsonb, \'{}\'::jsonb), '
        "$1::text[], $2::jsonb, true), $3::text[], $4::jsonb, true), $5::text[], $6::jsonb, true)"
    ) in sql
    assert '"name" = $7' in sql
    assert '"extra" = jsonb_set(COALESCE("extra"::jsonb, \'{}\'::jsonb), $8::text[], $9::jsonb, true)' in sql
    assert '"id" = $10' in sql
    assert bindings == [
        ["enabled"],
        "true",
        ["nested", "count"],
        "3",
        ["cleared"],
        "null",
        "updated",
        ["flag"],
        '"yes"',
        9,
    ]

    with pytest.raises(QueryError, match="whole column"):
        await Query.table("settings").where("id", 1).update(
            {"data": {}, "data->enabled": True}
        )
    with pytest.raises(QueryError, match="whole column"):
        await Query.table("settings").where("id", 1).update(
            {"data->enabled": True, "data": {}}
        )
    with pytest.raises(ValueError, match="Invalid JSON path"):
        await Query.table("settings").where("id", 1).update({"data->": True})
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        await Query.table("settings").where("id", 1).update({"users.data->a": True})
    with pytest.raises(QueryError, match="empty data"):
        await Query.table("settings").where("id", 1).update({})

    await Query.table("settings").where("id", 9).update({"name": "plain"})
    plain_sql, *plain_bindings = mock_conn.execute.await_args.args
    assert plain_sql == 'UPDATE "settings" SET "name" = $1 WHERE "id" = $2'
    assert plain_bindings == ["plain", 9]
    with pytest.raises(QueryError, match="without a WHERE"):
        await Query.table("settings").update({"data->enabled": True})


# ---------------------------------------------------------------------------
# Pessimistic locking & pagination tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locks_pagination_and_write_guards(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, mock_pool = mock_db_pool

    base = Query.table("users").where("id", 1)
    locked_update = base.clone().shared_lock().lock_for_update().limit(2)
    locked_share = base.clone().lock_for_update().shared_lock().offset(1)
    assert locked_update.to_sql() == (
        'SELECT * FROM "users" WHERE "id" = $1 LIMIT $2 FOR UPDATE',
        [1, 2],
    )
    assert locked_share.to_sql() == (
        'SELECT * FROM "users" WHERE "id" = $1 OFFSET $2 FOR SHARE',
        [1, 1],
    )
    assert base.to_sql() == ('SELECT * FROM "users" WHERE "id" = $1', [1])

    # Conflicts in to_sql()
    for conflict_builder, match_text in (
        (Query.table("users").distinct().shared_lock(), "DISTINCT"),
        (Query.table("users").group_by("status").lock_for_update(), "GROUP BY"),
        (Query.table("users").having("count", ">", 1).lock_for_update(), "HAVING"),
        (Query.table("users").having_raw("COUNT(*) > ?", [1]).shared_lock(), "HAVING"),
    ):
        with pytest.raises(QueryError, match=match_text):
            conflict_builder.to_sql()

    # Outside transaction: read terminals fail before acquiring connection
    for read_op in (
        lambda: Query.table("users").lock_for_update().get(),
        lambda: Query.table("users").lock_for_update().first(),
        lambda: Query.table("users").lock_for_update().value("id"),
        lambda: Query.table("users").lock_for_update().pluck("id"),
        lambda: Query.table("users").lock_for_update().simple_paginate(),
        lambda: Query.table("users").shared_lock().paginate(),
        lambda: HelperUser.lock_for_update().get(),
        lambda: HelperUser.shared_lock().paginate(),
    ):
        with pytest.raises(QueryError, match="transaction"):
            await read_op()
    mock_pool.acquire.assert_not_awaited()

    # Inside mocked transaction: reads retain lock, aggregates & count queries drop it
    mock_conn.closed = False
    token = _active_connection.set(mock_conn)
    try:
        executed_sql: list[str] = []

        async def fake_fetch(sql: str, *_args: Any) -> list[dict[str, Any]]:
            executed_sql.append(sql)
            if "agg_result" in sql:
                return [{"agg_result": 3}]
            return [{"id": 1, "name": "Ada", "email": "ada@example.com"}]

        mock_conn.fetch = AsyncMock(side_effect=fake_fetch)

        await Query.table("users").lock_for_update().first()
        assert executed_sql[-1].endswith("LIMIT $1 FOR UPDATE")

        await Query.table("users").shared_lock().value("name")
        assert executed_sql[-1].endswith("LIMIT $1 FOR SHARE")

        await Query.table("users").lock_for_update().pluck("id")
        assert executed_sql[-1].endswith("FOR UPDATE")

        await Query.table("users").shared_lock().simple_paginate(per_page=2, page=1)
        assert executed_sql[-1].endswith("LIMIT $1 OFFSET $2 FOR SHARE")

        executed_sql.clear()
        await Query.table("users").lock_for_update().paginate(per_page=2, page=1)
        assert len(executed_sql) == 2
        assert "FOR UPDATE" not in executed_sql[0]
        assert executed_sql[1].endswith("FOR UPDATE")

        executed_sql.clear()
        await HelperUser.lock_for_update().paginate(per_page=2, page=1)
        assert len(executed_sql) == 2
        assert "FOR UPDATE" not in executed_sql[0]
        assert executed_sql[1].endswith("FOR UPDATE")

        executed_sql.clear()
        await HelperUser.shared_lock().simple_paginate(per_page=2, page=1)
        assert len(executed_sql) == 1
        assert executed_sql[0].endswith("FOR SHARE")

        executed_sql.clear()
        assert await Query.table("users").lock_for_update().count() == 3
        assert await Query.table("users").lock_for_update().sum("id") == 3
        assert await Query.table("users").lock_for_update().min("id") == 3
        assert await Query.table("users").lock_for_update().max("id") == 3
        assert await Query.table("users").lock_for_update().avg("id") == 3
        assert await Query.table("users").lock_for_update().exists() is True
        assert all("FOR UPDATE" not in sql for sql in executed_sql)
    finally:
        _active_connection.reset(token)

    # Write rejection on both QueryBuilder and ModelQueryBuilder
    write_ops: list[Callable[[], Awaitable[Any]]] = [
        lambda: Query.table("users").lock_for_update().update({"name": "x"}),
        lambda: Query.table("users").shared_lock().delete(),
        lambda: Query.table("users").lock_for_update().increment("count"),
        lambda: Query.table("users").shared_lock().decrement("count"),
        lambda: Query.table("users").lock_for_update().upsert([{"id": 1}], "id"),
        lambda: HelperUser.lock_for_update().update({"name": "x"}),
        lambda: HelperUser.shared_lock().delete(),
        lambda: HelperUser.lock_for_update().increment("id"),
        lambda: HelperUser.shared_lock().decrement("id"),
        lambda: HelperUser.lock_for_update().upsert([{"id": 1}], "id"),
    ]
    for operation in write_ops:
        with pytest.raises(QueryError, match="row lock"):
            await operation()
    mock_pool.acquire.assert_not_awaited()


# ---------------------------------------------------------------------------
# Union compilation, snapshot, clone, and terminal guard tests
# ---------------------------------------------------------------------------


def test_union_compilation_branches_snapshot_clone_and_conflicts() -> None:
    branch_1 = Query.table("archived_users").where("active", False).order_by("score", "DESC").limit(3)
    branch_2 = Query.table("invited_users").where("invited", True).where("role", "guest")
    base = (
        Query.table("users")
        .where("active", True)
        .union(branch_1)
        .union_all(branch_2)
        .order_by("users.id", "DESC")
        .limit(5)
        .offset(2)
    )
    sql, bindings = base.to_sql()
    assert sql == (
        'SELECT * FROM ('
        '(SELECT * FROM "users" WHERE "active" = $1) '
        'UNION (SELECT * FROM "archived_users" WHERE "active" = $2 ORDER BY "score" DESC LIMIT $3) '
        'UNION ALL (SELECT * FROM "invited_users" WHERE "invited" = $4 AND "role" = $5)'
        ') AS "swiftpy_union" ORDER BY "id" DESC LIMIT $6 OFFSET $7'
    )
    assert bindings == [True, False, 3, True, "guest", 5, 2]

    # Snapshot independence: mutating branches after union() does not change base
    branch_1.where("deleted", False)
    branch_2.limit(9)
    assert base.to_sql() == (sql, bindings)

    # Clone independence: mutating clone does not change base
    cloned = base.clone()
    cloned.where("verified", True).in_random_order().limit(10)
    cloned_sql, cloned_bindings = cloned.to_sql()
    assert base.to_sql() == (sql, bindings)
    assert "ORDER BY RANDOM() LIMIT $7 OFFSET $8" in cloned_sql
    assert cloned_bindings == [True, True, False, 3, True, "guest", 10, 2]

    # Negative union errors
    with pytest.raises(TypeError, match="QueryBuilder"):
        Query.table("users").union(cast(Any, "not a query"))
    with pytest.raises(QueryError, match="Nested union"):
        Query.table("users").union(
            Query.table("orders").union(Query.table("archived_orders"))
        )
    with pytest.raises(QueryError, match="locked"):
        Query.table("users").union(Query.table("orders").lock_for_update())
    with pytest.raises(QueryError, match="locked"):
        Query.table("users").lock_for_update().union(Query.table("orders"))
    with pytest.raises(QueryError, match="UNION"):
        Query.table("users").union(Query.table("orders")).shared_lock().to_sql()


@pytest.mark.parametrize(
    "invoke",
    [
        lambda q: q.count(),
        lambda q: q.sum("id"),
        lambda q: q.min("id"),
        lambda q: q.max("id"),
        lambda q: q.avg("id"),
        lambda q: q.exists(),
        lambda q: q.paginate(),
        lambda q: q.pluck("id"),
        lambda q: q.value("id"),
        lambda q: q.update({"name": "x"}),
        lambda q: q.delete(),
        lambda q: q.increment("count"),
        lambda q: q.decrement("count"),
        lambda q: q.upsert([{"id": 1}], "id"),
        lambda q: q.chunk(1, lambda _rows: None),
        lambda q: q.chunk_by_id(1, lambda _rows: None),
    ],
)
@pytest.mark.asyncio
async def test_union_terminals_fail_before_io(
    mock_db_pool: tuple[AsyncMock, MagicMock],
    invoke: Callable[[QueryBuilder], Awaitable[Any]],
) -> None:
    mock_conn, mock_pool = mock_db_pool
    query = Query.table("users").union(Query.table("archived_users"))
    with pytest.raises(QueryError, match="union query"):
        await invoke(query)
    mock_conn.fetch.assert_not_awaited()
    mock_conn.execute.assert_not_awaited()
    mock_pool.acquire.assert_not_awaited()


# ---------------------------------------------------------------------------
# Debug helpers & SQL placeholder scanner tests
# ---------------------------------------------------------------------------


def test_debug_sql_scanner_handles_comments_dollar_quotes_escapes_and_placeholders() -> None:
    # 1. Line comments (including EOF without newline) and block comments (including nested)
    commented_sql = (
        "SELECT $1 -- ignore $99 in line comment\n"
        "/* ignore $88 /* nested $77 */ still comment $66 */ "
        "+ $2 -- trailing $55"
    )
    assert (
        _substitute_bindings(commented_sql, [10, 20])
        == "SELECT 10 -- ignore $99 in line comment\n"
        "/* ignore $88 /* nested $77 */ still comment $66 */ "
        "+ 20 -- trailing $55"
    )

    # 2. PostgreSQL dollar-quoted strings ($$...$$ and $tag$...$tag$)
    dollar_sql = (
        "SELECT $body$This contains $1 and $99 and should not be substituted$body$, "
        "$$another $2 literal$$, $tag_1$ok $3$tag_1$, $1"
    )
    assert (
        _substitute_bindings(dollar_sql, ["bound"])
        == "SELECT $body$This contains $1 and $99 and should not be substituted$body$, "
        "$$another $2 literal$$, $tag_1$ok $3$tag_1$, 'bound'"
    )

    # 3. Escaped single quotes (standard SQL '' and PostgreSQL E'...' escape strings) + double quotes
    escaped_sql = (
        r"SELECT 'it''s $1', '''', E'escaped \' $1 and \\', E'back\\' || $1, "
        '"col""$1" WHERE x = $2'
    )
    assert (
        _substitute_bindings(escaped_sql, ["A", "B"])
        == r"SELECT 'it''s $1', '''', E'escaped \' $1 and \\', E'back\\' || 'A', "
        '"col""$1" WHERE x = \'B\''
    )

    # 4. Consecutive, repeated, and multi-digit placeholders ($10, $100)
    assert _substitute_bindings("SELECT $1$2, $1, $2, $1", [1, 2]) == "SELECT 12, 1, 2, 1"

    hundred_bindings = list(range(1, 105))
    multi_digit_sql = "SELECT $1, $10, $100, $104"
    assert (
        _substitute_bindings(multi_digit_sql, hundred_bindings)
        == "SELECT 1, 10, 100, 104"
    )

    # 5. Invalid ($0) and out-of-range placeholder indexes
    with pytest.raises(QueryError, match=r"\$0 has no corresponding binding"):
        _substitute_bindings("SELECT $0", [1])
    with pytest.raises(QueryError, match=r"\$2 has no corresponding binding"):
        _substitute_bindings("SELECT $2", [1])
    with pytest.raises(QueryError, match=r"\$1 has no corresponding binding"):
        _substitute_bindings("SELECT $1", [])


def test_debug_sql_logging_and_model_helpers(
    caplog: pytest.LogCaptureFixture,
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    _, mock_pool = mock_db_pool
    caplog.set_level("INFO", logger="swiftpy.database.query_builder")
    query = (
        Query.table("users")
        .select_raw("'$1' AS literal, \"$1\" AS identifier")
        .where("name", "O'Brien")
        .where("id", "IN", list(range(10)))
    )
    rendered = query.to_raw_sql()
    assert "'$1' AS literal" in rendered
    assert '"$1" AS identifier' in rendered
    assert "'O''Brien'" in rendered
    assert "IN (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)" in rendered

    assert query.dump() is query
    assert query.dump_raw_sql() is query
    assert len(caplog.records) == 2
    assert all("SELECT" in record.message for record in caplog.records)
    with pytest.raises(QueryDumpError):
        query.dd()
    with pytest.raises(QueryDumpError):
        query.dd_raw_sql()

    caplog.clear()
    mq = HelperUser.where("name", "Ada")
    assert mq.dump() is mq
    assert mq.dump_raw_sql() is mq
    assert len(caplog.records) == 2
    assert mq.to_raw_sql() == 'SELECT * FROM "helper_users" WHERE "name" = \'Ada\''
    with pytest.raises(QueryDumpError):
        mq.dd()
    with pytest.raises(QueryDumpError):
        mq.dd_raw_sql()
    mock_pool.acquire.assert_not_awaited()


def test_debug_sql_literals_cover_supported_and_unusual_values() -> None:
    cases: tuple[tuple[Any, str], ...] = (
        (None, "NULL"),
        (True, "TRUE"),
        (False, "FALSE"),
        (12, "12"),
        (1.25, "1.25"),
        (float("nan"), "'NaN'::float8"),
        (float("inf"), "'Infinity'::float8"),
        (float("-inf"), "'-Infinity'::float8"),
        (Decimal("2.50"), "2.50"),
        (Decimal("NaN"), "'NaN'::numeric"),
        (Decimal("Infinity"), "'Infinity'::numeric"),
        (Decimal("-Infinity"), "'-Infinity'::numeric"),
        ("O'Brien", "'O''Brien'"),
        # Diagnostic-only rendering: NUL cannot exist in ordinary PG text literals.
        ("nul\x00byte", "'nul\\x00byte'"),
        ("café", "'café'"),
        (b"\x00a", "'\\x0061'::bytea"),
        (datetime(2025, 1, 2, tzinfo=UTC), "'2025-01-02T00:00:00+00:00'"),
        (date(2025, 1, 2), "'2025-01-02'"),
        (time(12, 30), "'12:30:00'"),
        (
            UUID("12345678-1234-5678-1234-567812345678"),
            "'12345678-1234-5678-1234-567812345678'",
        ),
        ([], "'{}'"),
        ([1, [2]], "ARRAY[1, ARRAY[2]]"),
        ({"enabled": True}, '\'{"enabled":true}\'::jsonb'),
        (_CustomDebugObject(), "'custom-debug''object'"),
        (bytearray(b"ab"), "'bytearray(b''ab'')'"),
    )
    for value, expected in cases:
        query = Query.table("debug_values").where_raw("value = ?", [value])
        assert query.to_raw_sql().endswith(f"WHERE value = {expected}")

    # Diagnostic rendering for dict with non-JSON-serializable value raises QueryError
    with pytest.raises(QueryError, match="serializable"):
        Query.table("debug_values").where_raw("v = ?", [{"bad": object()}]).to_raw_sql()


# ---------------------------------------------------------------------------
# Comprehensive ORM parity tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "where_column",
        "or_where_column",
        "where_exists",
        "where_not_exists",
        "or_where_exists",
        "or_where_not_exists",
        "where_any",
        "where_all",
        "where_none",
        "or_where_any",
        "or_where_all",
        "or_where_none",
        "where_past",
        "where_future",
        "or_where_past",
        "or_where_future",
        "where_json_contains",
        "where_json_doesnt_contain",
        "where_json_contains_key",
        "where_json_doesnt_contain_key",
        "where_json_length",
        "or_where_json_contains",
        "or_where_json_doesnt_contain",
        "or_where_json_contains_key",
        "or_where_json_doesnt_contain_key",
        "or_where_json_length",
        "shared_lock",
        "lock_for_update",
    ],
)
def test_model_and_model_query_builder_expose_helper(method: str) -> None:
    assert callable(getattr(HelperUser.query(), method))
    assert callable(getattr(HelperUser, method))


@pytest.mark.parametrize("method", ["to_sql", "to_raw_sql", "dump", "dump_raw_sql", "dd", "dd_raw_sql"])
def test_model_query_builder_exposes_debug_and_sql_helpers(method: str) -> None:
    assert callable(getattr(HelperUser.query(), method))


def test_orm_forwarding_equivalence_and_boolean_preservation() -> None:
    order_sub_mqb = HelperOrder.query().where_column(
        "helper_orders.user_id", "=", "helper_users.id"
    )
    order_sub_qb = Query.table("helper_orders").where_column(
        "helper_orders.user_id", "=", "helper_users.id"
    )

    pairs: list[tuple[ModelQueryBuilder[HelperUser], QueryBuilder]] = [
        (
            HelperUser.where_column("helper_users.id", ">", "helper_users.org_id").where_column(
                "helper_users.id", "=", "helper_users.parent_id", boolean="OR"
            ).or_where_column("helper_users.a", "<>", "helper_users.b"),
            Query.table("helper_users")
            .where_column("helper_users.id", ">", "helper_users.org_id")
            .where_column("helper_users.id", "=", "helper_users.parent_id", boolean="OR")
            .or_where_column("helper_users.a", "<>", "helper_users.b"),
        ),
        (
            HelperUser.where_exists(order_sub_mqb)
            .where_not_exists(order_sub_qb, boolean="OR")
            .or_where_exists(order_sub_mqb)
            .or_where_not_exists(order_sub_qb),
            Query.table("helper_users")
            .where_exists(order_sub_qb)
            .where_not_exists(order_sub_qb, boolean="OR")
            .or_where_exists(order_sub_qb)
            .or_where_not_exists(order_sub_qb),
        ),
        (
            HelperUser.where_any(["name", "email"], "ada")
            .where_all(["name", "email"], "like", "A%", boolean="OR")
            .where_none(["name", "email"], "IN", ["x", "y"])
            .or_where_any(["name", "email"], "bob")
            .or_where_all(["name", "email"], "!=", None)
            .or_where_none(["name", "email"], "like", "%z%"),
            Query.table("helper_users")
            .where_any(["name", "email"], "ada")
            .where_all(["name", "email"], "like", "A%", boolean="OR")
            .where_none(["name", "email"], "IN", ["x", "y"])
            .or_where_any(["name", "email"], "bob")
            .or_where_all(["name", "email"], "!=", None)
            .or_where_none(["name", "email"], "like", "%z%"),
        ),
        (
            HelperUser.where_past("created_at")
            .where_future("expires_at", boolean="OR")
            .or_where_past("archived_at")
            .or_where_future("renew_at"),
            Query.table("helper_users")
            .where_past("created_at")
            .where_future("expires_at", boolean="OR")
            .or_where_past("archived_at")
            .or_where_future("renew_at"),
        ),
        (
            HelperUser.where("prefs->theme", "dark")
            .or_where("prefs->score", ">", 5)
            .where_in("prefs->role", ["admin", "editor"])
            .or_where_in("prefs->tier", [1, 2])
            .where_not_in("prefs->status", ["banned"])
            .or_where_not_in("prefs->region", ["xx"])
            .where_json_contains("prefs->roles", "editor")
            .where_json_doesnt_contain("prefs->roles", "banned", boolean="OR")
            .or_where_json_contains("prefs", {"active": True})
            .or_where_json_doesnt_contain("prefs->tags", ["spam"])
            .where_json_contains_key("prefs->theme")
            .where_json_doesnt_contain_key("prefs->old", boolean="OR")
            .or_where_json_contains_key("prefs->lang")
            .or_where_json_doesnt_contain_key("prefs->legacy")
            .where_json_length("prefs->tags", 2)
            .where_json_length("prefs->tags", ">=", 1, boolean="OR")
            .or_where_json_length("prefs->items", 3)
            .or_where_json_length("prefs->items", "<", 10),
            Query.table("helper_users")
            .where("prefs->theme", "dark")
            .or_where("prefs->score", ">", 5)
            .where_in("prefs->role", ["admin", "editor"])
            .or_where_in("prefs->tier", [1, 2])
            .where_not_in("prefs->status", ["banned"])
            .or_where_not_in("prefs->region", ["xx"])
            .where_json_contains("prefs->roles", "editor")
            .where_json_doesnt_contain("prefs->roles", "banned", boolean="OR")
            .or_where_json_contains("prefs", {"active": True})
            .or_where_json_doesnt_contain("prefs->tags", ["spam"])
            .where_json_contains_key("prefs->theme")
            .where_json_doesnt_contain_key("prefs->old", boolean="OR")
            .or_where_json_contains_key("prefs->lang")
            .or_where_json_doesnt_contain_key("prefs->legacy")
            .where_json_length("prefs->tags", 2)
            .where_json_length("prefs->tags", ">=", 1, boolean="OR")
            .or_where_json_length("prefs->items", 3)
            .or_where_json_length("prefs->items", "<", 10),
        ),
        (
            HelperUser.query().shared_lock().lock_for_update(),
            Query.table("helper_users").shared_lock().lock_for_update(),
        ),
        (
            HelperUser.where_group(
                lambda q: q.where_any(["name", "email"], "ada").or_where_json_contains_key("prefs->theme")
            ),
            Query.table("helper_users").where_group(
                lambda q: q.where_any(["name", "email"], "ada").or_where_json_contains_key("prefs->theme")
            ),
        ),
    ]

    for mqb, qb in pairs:
        assert isinstance(mqb, ModelQueryBuilder)
        assert mqb.to_sql() == qb.to_sql()
        assert mqb.to_raw_sql() == qb.to_raw_sql()

    # ModelQueryBuilder methods return the same builder instance (`self`)
    chain = HelperUser.query()
    assert chain.where_column("id", "=", "id") is chain
    assert chain.where_any(["id"], 1) is chain
    assert chain.where_past("created_at") is chain
    assert chain.where_json_contains_key("prefs->theme") is chain
    assert chain.shared_lock() is chain
    assert chain.lock_for_update() is chain

    # Snapshot independence when passing a ModelQueryBuilder into where_exists
    mutable_sub = HelperOrder.where("user_id", 1)
    outer = HelperUser.where_exists(mutable_sub)
    snapshot_before = outer.to_sql()
    mutable_sub.where("id", 99)
    assert outer.to_sql() == snapshot_before


@pytest.mark.asyncio
async def test_orm_update_forwards_json_paths(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    await HelperUser.where("id", 4).update({"prefs->theme": "light", "name": "Ada"})
    sql, *bindings = mock_conn.execute.await_args.args
    assert 'UPDATE "helper_users" SET "prefs" = jsonb_set(COALESCE("prefs"::jsonb, \'{}\'::jsonb), $1::text[], $2::jsonb, true), "name" = $3 WHERE "id" = $4' == sql
    assert bindings == [["theme"], '"light"', "Ada", 4]


@pytest.mark.asyncio
async def test_compound_reads_execute_once(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    mock_conn.fetch = AsyncMock(return_value=[])

    queries = (
        Query.table("users").where_exists(Query.table("orders").where("paid", True)),
        Query.table("users").union(Query.table("archived_users")),
        Query.table("users").where_json_contains("settings", {"theme": "dark"}),
    )
    for query in queries:
        await query.get()
    assert mock_conn.fetch.await_count == 3


# ---------------------------------------------------------------------------
# PostgreSQL integration & concurrency tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_where_any_all_none_and_past_future(pg_pool: DatabasePool) -> None:
    pool = pg_pool.get_pool()
    now = datetime.now(UTC)
    past = now - timedelta(hours=2)
    future = now + timedelta(hours=2)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS "swiftpy_v036_predicates";
            CREATE TABLE "swiftpy_v036_predicates" (
                id integer PRIMARY KEY,
                primary_email text,
                backup_email text,
                status text NOT NULL,
                tier text NOT NULL,
                due_at timestamptz
            );
            """
        )
        await conn.executemany(
            'INSERT INTO "swiftpy_v036_predicates" VALUES ($1, $2, $3, $4, $5, $6)',
            [
                (1, "ada@example.com", None, "active", "pro", past),
                (2, None, "grace@example.com", "active", "free", future),
                (3, "linus@example.com", "linus@alt.org", "banned", "pro", None),
                (4, "ada@example.com", "ada@alt.org", "pending", "pro", past),
            ],
        )
    try:
        assert [
            r["id"]
            for r in await Query.table("swiftpy_v036_predicates")
            .where_any(["primary_email", "backup_email"], "like", "ada%")
            .order_by("id")
            .get()
        ] == [1, 4]

        assert [
            r["id"]
            for r in await Query.table("swiftpy_v036_predicates")
            .where_all(["status", "tier"], "IN", ["active", "pro"])
            .order_by("id")
            .get()
        ] == [1]

        assert [
            r["id"]
            for r in await Query.table("swiftpy_v036_predicates")
            .where_none(["primary_email", "backup_email"], None)
            .order_by("id")
            .get()
        ] == [3, 4]

        assert [
            r["id"]
            for r in await Query.table("swiftpy_v036_predicates")
            .where("status", "banned")
            .or_where_all(["status", "tier"], "IN", ["active", "free"])
            .order_by("id")
            .get()
        ] == [2, 3]

        assert [
            r["id"]
            for r in await Query.table("swiftpy_v036_predicates")
            .where_past("due_at")
            .order_by("id")
            .get()
        ] == [1, 4]

        assert [
            r["id"]
            for r in await Query.table("swiftpy_v036_predicates")
            .where_future("due_at")
            .order_by("id")
            .get()
        ] == [2]

        assert [
            r["id"]
            for r in await Query.table("swiftpy_v036_predicates")
            .where_past("due_at")
            .or_where_future("due_at")
            .order_by("id")
            .get()
        ] == [1, 2, 4]
    finally:
        async with pool.acquire() as conn:
            await conn.execute('DROP TABLE IF EXISTS "swiftpy_v036_predicates";')


@pytest.mark.asyncio
async def test_pg_json_comparisons_edge_cases_and_updates(pg_pool: DatabasePool) -> None:
    pool = pg_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS "swiftpy_v036_json_orders";
            DROP TABLE IF EXISTS "swiftpy_v036_json_items";
            CREATE TABLE "swiftpy_v036_json_items" (
                id integer PRIMARY KEY,
                label text NOT NULL,
                metadata jsonb,
                legacy json
            );
            CREATE TABLE "swiftpy_v036_json_orders" (
                id integer PRIMARY KEY,
                item_id integer
            );
            INSERT INTO "swiftpy_v036_json_items" VALUES
                (1, 'item-1', '{"enabled":true,"score":15,"label":"Alpha","tags":["alpha","beta"],"matrix":[[1,2],[3,4]],"nested":{"role":"admin","tier":2},"nullable":null,"empty_obj":{},"empty_arr":[]}', '{"ok":true,"nested":{"count":1}}'),
                (2, 'item-2', '{"enabled":false,"score":"mixed","label":123,"tags":["gamma"],"nested":{"role":"viewer","tier":1}}', '{"ok":false}'),
                (3, 'item-3', NULL, NULL),
                (4, 'item-4', '{"score":5,"label":"Beta","tags":[],"nullable":"present"}', '{"ok":true}'),
                (5, 'item-5', '{}', '{}');
            INSERT INTO "swiftpy_v036_json_orders" VALUES (1, 1), (2, 1);
            """
        )
    try:
        async def ids_for(qb: QueryBuilder) -> list[int]:
            rows = await qb.order_by("id", "ASC").get()
            return [int(r["id"]) for r in rows]

        # Comparison table rows (D3)
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->label", "Alpha")) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->score", 5)) == [4]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->enabled", True)) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->enabled", False)) == [2]
        # JSON null AND missing key AND SQL NULL match = None
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->nullable", None)) == [1, 2, 3, 5]
        # != None matches only non-null values
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->nullable", "!=", None)) == [4]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->nullable", "<>", None)) == [4]
        # != / <> non-None excludes missing keys and SQL NULL (SQL semantics)
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->enabled", "!=", True)) == [2]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->enabled", "<>", True)) == [2]
        # Mixed-type numeric & string comparisons never raise cast errors
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->score", ">", 10)) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->score", "<=", 10)) == [4]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->label", ">", "Alpha")) == [4]
        # LIKE / ILIKE on mixed-type rows
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->label", "LIKE", "Al%")) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->label", "ILIKE", "be%")) == [4]
        # IN / NOT IN / empty IN / empty NOT IN
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_in("metadata->score", [5, 15])) == [1, 4]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_not_in("metadata->score", [15])) == [2, 4]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_in("metadata->score", [])) == []
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_not_in("metadata->score", [])) == [1, 2, 3, 4, 5]

        # JSON edge cases: nested objects, nested arrays, array index, missing intermediate paths, empty objs/arrays
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->nested->role", "admin")) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->matrix->1->0", 3)) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->tags->0", "gamma")) == [2]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->missing->deep", None)) == [1, 2, 3, 4, 5]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->empty_obj", {})) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where("metadata->empty_arr", [])) == [1]
        # Containment: scalar in array, array subset, nested object
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_json_contains("metadata->tags", "alpha")) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_json_contains("metadata->tags", ["alpha", "beta"])) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_json_contains("metadata", {"nested": {"role": "admin"}})) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_json_doesnt_contain("metadata->enabled", True)) == [2, 3, 4, 5]
        # Key existence: JSON null vs SQL NULL & missing path
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_json_contains_key("metadata->nullable")) == [1, 4]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_json_doesnt_contain_key("metadata->nullable")) == [2, 3, 5]
        # Length: array only, != never matches non-arrays/missing/NULL
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_json_length("metadata->tags", 2)) == [1]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_json_length("metadata->tags", 0)) == [4]
        assert await ids_for(Query.table("swiftpy_v036_json_items").where_json_length("metadata->nested", "!=", 2)) == []

        # Correlated EXISTS + ORM hydration with JSON filters
        assert await ids_for(
            Query.table("swiftpy_v036_json_items").where_exists(
                Query.table("swiftpy_v036_json_orders").where_column(
                    "swiftpy_v036_json_orders.item_id", "=", "swiftpy_v036_json_items.id"
                )
            )
        ) == [1]
        orm_items = await PgV036JsonItem.where_json_contains("metadata->tags", "alpha").get()
        assert [item.id for item in orm_items] == [1]

        # JSON updates: all 10 cases
        # 1. Updating a SQL NULL JSON column
        await Query.table("swiftpy_v036_json_items").where("id", 3).update(
            {"metadata->created_from_null": True, "legacy->created_from_null": "ok"}
        )
        row3 = await Query.table("swiftpy_v036_json_items").where("id", 3).first()
        assert row3 is not None
        assert json.loads(row3["metadata"]) == {"created_from_null": True}
        assert json.loads(row3["legacy"]) == {"created_from_null": "ok"}

        # 2, 3, 4, 5, 6, 8: existing nested property, multiple properties same col,
        # different JSON cols, JSON null, missing top-level property, json + jsonb
        await Query.table("swiftpy_v036_json_items").where("id", 1).update(
            {
                "metadata->nested->tier": 9,
                "metadata->enabled": False,
                "metadata->added": "yes",
                "metadata->empty_obj": None,
                "legacy->nested->count": 42,
                "legacy->updated": True,
            }
        )
        row1 = await Query.table("swiftpy_v036_json_items").where("id", 1).first()
        assert row1 is not None
        m1 = json.loads(row1["metadata"])
        assert m1["nested"] == {"role": "admin", "tier": 9}
        assert m1["enabled"] is False
        assert m1["added"] == "yes"
        assert "empty_obj" in m1 and m1["empty_obj"] is None
        assert json.loads(row1["legacy"]) == {"ok": True, "nested": {"count": 42}, "updated": True}

        # 7. Missing intermediate parent does not create intermediate object (PostgreSQL jsonb_set behavior)
        await Query.table("swiftpy_v036_json_items").where("id", 5).update(
            {"metadata->missing_parent->child": "noop"}
        )
        row5 = await Query.table("swiftpy_v036_json_items").where("id", 5).first()
        assert row5 is not None and json.loads(row5["metadata"]) == {}

        # 9. Plain-column update leaves JSON intact
        await Query.table("swiftpy_v036_json_items").where("id", 4).update({"label": "plain-updated"})
        row4 = await Query.table("swiftpy_v036_json_items").where("id", 4).first()
        assert row4 is not None
        assert row4["label"] == "plain-updated"
        assert json.loads(row4["metadata"]) == {"score": 5, "label": "Beta", "tags": [], "nullable": "present"}

        # 10. Failed update does not partially modify JSON data (NOT NULL violation rolls back entire UPDATE)
        with pytest.raises(asyncpg.NotNullViolationError):
            await Query.table("swiftpy_v036_json_items").where("id", 4).update(
                {"metadata->score": 999, "label": None}
            )
        row4_after = await Query.table("swiftpy_v036_json_items").where("id", 4).first()
        assert row4_after is not None
        assert json.loads(row4_after["metadata"])["score"] == 5
        assert row4_after["label"] == "plain-updated"
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                'DROP TABLE IF EXISTS "swiftpy_v036_json_orders"; '
                'DROP TABLE IF EXISTS "swiftpy_v036_json_items";'
            )


@pytest.mark.asyncio
async def test_pg_pessimistic_locking_concurrency_and_pagination(
    pg_pool: DatabasePool,
) -> None:
    pool = pg_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS "swiftpy_v036_lock_items";
            CREATE TABLE "swiftpy_v036_lock_items" (
                id integer PRIMARY KEY,
                status text NOT NULL,
                balance integer NOT NULL
            );
            INSERT INTO "swiftpy_v036_lock_items" VALUES
                (1, 'open', 100),
                (2, 'open', 200),
                (3, 'closed', 300);
            """
        )
    try:
        # 1. lock_for_update() blocks both raw FOR UPDATE and an independent concurrent db.transaction()
        async with db.transaction():
            locked = await Query.table("swiftpy_v036_lock_items").where("id", 1).lock_for_update().first()
            assert locked is not None and locked["balance"] == 100

            async with pool.acquire() as conn_b:
                await conn_b.execute("SET lock_timeout = '150ms'")
                try:
                    with pytest.raises(asyncpg.LockNotAvailableError):
                        await conn_b.fetchrow(
                            'SELECT * FROM "swiftpy_v036_lock_items" WHERE id = 1 FOR UPDATE'
                        )
                finally:
                    await conn_b.execute("RESET lock_timeout")

            async def contender_update() -> None:
                # Simulate a separate request/task with no inherited outer transaction context.
                reset_tok = _active_connection.set(None)
                try:
                    async with db.transaction():
                        active = get_active_connection()
                        await active.execute("SET LOCAL lock_timeout = '150ms'")
                        await Query.table("swiftpy_v036_lock_items").where("id", 1).lock_for_update().first()
                finally:
                    _active_connection.reset(reset_tok)

            with pytest.raises(asyncpg.LockNotAvailableError):
                await asyncio.wait_for(contender_update(), timeout=5.0)

            await Query.table("swiftpy_v036_lock_items").where("id", 1).update({"balance": 150})

        # Once committed, another transaction acquires the lock and sees updated balance
        async with db.transaction():
            after_commit = await PgV036LockItem.where("id", 1).lock_for_update().first()
            assert after_commit is not None and after_commit.balance == 150

        # 2. shared_lock() allows concurrent FOR SHARE while blocking FOR UPDATE until commit
        async with db.transaction():
            shared = await Query.table("swiftpy_v036_lock_items").where("id", 2).shared_lock().first()
            assert shared is not None and shared["balance"] == 200

            async with pool.acquire() as conn_share:
                await conn_share.execute("SET lock_timeout = '150ms'")
                try:
                    row_shared = await conn_share.fetchrow(
                        'SELECT * FROM "swiftpy_v036_lock_items" WHERE id = 2 FOR SHARE'
                    )
                    assert row_shared is not None and row_shared["balance"] == 200

                    with pytest.raises(asyncpg.LockNotAvailableError):
                        await conn_share.fetchrow(
                            'SELECT * FROM "swiftpy_v036_lock_items" WHERE id = 2 FOR UPDATE'
                        )
                finally:
                    await conn_share.execute("RESET lock_timeout")

        # After shared_lock transaction commits, FOR UPDATE succeeds
        async with pool.acquire() as conn_after:
            await conn_after.execute("SET lock_timeout = '150ms'")
            try:
                row_unlocked = await conn_after.fetchrow(
                    'SELECT * FROM "swiftpy_v036_lock_items" WHERE id = 2 FOR UPDATE'
                )
                assert row_unlocked is not None
            finally:
                await conn_after.execute("RESET lock_timeout")

        # 3. Live PostgreSQL pagination + simple_paginate + value + pluck under transaction
        async with db.transaction():
            qb_page = (
                await Query.table("swiftpy_v036_lock_items")
                .where("status", "open")
                .order_by("id")
                .lock_for_update()
                .paginate(per_page=1, page=1)
            )
            assert qb_page.total == 2
            assert [row["id"] for row in qb_page.items] == [1]

            orm_page = (
                await PgV036LockItem.where("status", "open")
                .order_by("id")
                .shared_lock()
                .paginate(per_page=1, page=2)
            )
            assert orm_page.total == 2
            assert [item.id for item in orm_page.items] == [2]

            simple = (
                await PgV036LockItem.order_by("id")
                .lock_for_update()
                .simple_paginate(per_page=2, page=1)
            )
            assert simple.has_more_pages is True
            assert [item.id for item in simple.items] == [1, 2]

            val = await Query.table("swiftpy_v036_lock_items").where("id", 1).lock_for_update().value("balance")
            assert val == 150
            plucked = await Query.table("swiftpy_v036_lock_items").order_by("id").shared_lock().pluck("id")
            assert plucked == [1, 2, 3]
    finally:
        async with pool.acquire() as conn:
            await conn.execute('DROP TABLE IF EXISTS "swiftpy_v036_lock_items";')


@pytest.mark.asyncio
async def test_pg_union_and_union_all_execution(pg_pool: DatabasePool) -> None:
    pool = pg_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS "swiftpy_v036_u_all";
            DROP TABLE IF EXISTS "swiftpy_v036_u_a";
            DROP TABLE IF EXISTS "swiftpy_v036_u_b";
            DROP TABLE IF EXISTS "swiftpy_v036_u_c";
            CREATE TABLE "swiftpy_v036_u_a" (id integer PRIMARY KEY, name text NOT NULL, score integer NOT NULL);
            CREATE TABLE "swiftpy_v036_u_b" (id integer PRIMARY KEY, name text NOT NULL, score integer NOT NULL);
            CREATE TABLE "swiftpy_v036_u_c" (id integer PRIMARY KEY, name text NOT NULL, score integer NOT NULL);
            CREATE TABLE "swiftpy_v036_u_all" (id integer PRIMARY KEY, label text NOT NULL);
            INSERT INTO "swiftpy_v036_u_a" VALUES (1, 'Ada', 90), (2, 'Bob', 50);
            INSERT INTO "swiftpy_v036_u_b" VALUES (2, 'Bob', 50), (3, 'Cara', 80), (4, 'Dan', 70);
            INSERT INTO "swiftpy_v036_u_c" VALUES (1, 'Ada', 90), (5, 'Eve', 95);
            INSERT INTO "swiftpy_v036_u_all" VALUES (1, 'one'), (2, 'two'), (3, 'three'), (5, 'five'), (9, 'nine');
            """
        )
    try:
        # 1. UNION deduplicates vs UNION ALL retains duplicates
        dedup_rows = (
            await Query.table("swiftpy_v036_u_a")
            .select("id", "name")
            .union(Query.table("swiftpy_v036_u_b").select("id", "name"))
            .order_by("id", "ASC")
            .get()
        )
        assert [(r["id"], r["name"]) for r in dedup_rows] == [
            (1, "Ada"),
            (2, "Bob"),
            (3, "Cara"),
            (4, "Dan"),
        ]

        dup_rows = (
            await Query.table("swiftpy_v036_u_a")
            .select("id", "name")
            .union_all(Query.table("swiftpy_v036_u_b").select("id", "name"))
            .order_by("id", "ASC")
            .get()
        )
        assert [r["id"] for r in dup_rows] == [1, 2, 2, 3, 4]

        # 2. Three branches, different binding counts, branch-level ORDER BY / LIMIT + outer pagination
        three_branch = (
            Query.table("swiftpy_v036_u_a")
            .select("id", "name", "score")
            .where("score", ">=", 50)
            .union(
                Query.table("swiftpy_v036_u_b")
                .select("id", "name", "score")
                .where("score", ">=", 70)
                .where("id", "!=", 4)
                .order_by("score", "DESC")
                .limit(1)
            )
            .union_all(
                Query.table("swiftpy_v036_u_c")
                .select("id", "name", "score")
                .where("id", ">", 0)
                .where("name", "LIKE", "%")
                .where("score", "<=", 95)
            )
            .order_by("swiftpy_v036_u_a.id", "ASC")
        )
        all_three = await three_branch.get()
        assert [(r["id"], r["name"]) for r in all_three] == [
            (1, "Ada"),
            (1, "Ada"),
            (2, "Bob"),
            (3, "Cara"),
            (5, "Eve"),
        ]

        page1 = await three_branch.simple_paginate(per_page=2, page=1)
        assert page1.has_more_pages is True
        assert [r["id"] for r in page1.items] == [1, 1]
        page3 = await three_branch.simple_paginate(per_page=2, page=3)
        assert page3.has_more_pages is False
        assert [r["id"] for r in page3.items] == [5]

        # 3. Union used as IN subquery and EXISTS subquery
        union_ids = (
            Query.table("swiftpy_v036_u_a")
            .select("id")
            .where("id", 1)
            .union(Query.table("swiftpy_v036_u_c").select("id").where("id", 5))
        )
        in_rows = (
            await Query.table("swiftpy_v036_u_all")
            .where_in("id", union_ids)
            .order_by("id")
            .get()
        )
        assert [r["id"] for r in in_rows] == [1, 5]

        exists_union = (
            Query.table("swiftpy_v036_u_a")
            .where_column("swiftpy_v036_u_a.id", "=", "swiftpy_v036_u_all.id")
            .union(
                Query.table("swiftpy_v036_u_c").where_column(
                    "swiftpy_v036_u_c.id", "=", "swiftpy_v036_u_all.id"
                )
            )
        )
        exists_rows = (
            await Query.table("swiftpy_v036_u_all")
            .where_exists(exists_union)
            .order_by("id")
            .get()
        )
        assert [r["id"] for r in exists_rows] == [1, 2, 5]

        # 4. Incompatible column count & incompatible column types raise PostgreSQL errors
        with pytest.raises(asyncpg.PostgresError):
            await (
                Query.table("swiftpy_v036_u_a")
                .select("id", "name")
                .union(Query.table("swiftpy_v036_u_b").select("id"))
                .get()
            )
        with pytest.raises(asyncpg.PostgresError):
            await (
                Query.table("swiftpy_v036_u_a")
                .select("id")
                .union(Query.table("swiftpy_v036_u_b").select("name"))
                .get()
            )
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                'DROP TABLE IF EXISTS "swiftpy_v036_u_all"; '
                'DROP TABLE IF EXISTS "swiftpy_v036_u_a"; '
                'DROP TABLE IF EXISTS "swiftpy_v036_u_b"; '
                'DROP TABLE IF EXISTS "swiftpy_v036_u_c";'
            )
