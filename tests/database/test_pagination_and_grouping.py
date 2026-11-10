from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swiftpy.core.container import get_container
from swiftpy.database.orm.model import Model
from swiftpy.database.pagination import Paginator, SimplePaginator
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query

# ============================================================================
# Dummy Model for ORM tests
# ============================================================================


class SampleOrder(Model):
    __table__ = "orders"
    __fillable__ = ("id", "status", "country", "amount")

    id: int
    status: str
    country: str
    amount: float


# ============================================================================
# 1. Limit and Offset Tests
# ============================================================================


def test_limit_and_offset_valid_values() -> None:
    """Verify limit and offset accept valid positive integers and zero."""
    qb = Query.table("users").limit(10).offset(20)
    sql, bindings = qb.to_sql()

    assert "LIMIT $1 OFFSET $2" in sql
    assert bindings == [10, 20]

    # Zero is allowed for both limit and offset
    qb_zero = Query.table("users").limit(0).offset(0)
    sql_z, bindings_z = qb_zero.to_sql()
    assert "LIMIT $1 OFFSET $2" in sql_z
    assert bindings_z == [0, 0]


def test_limit_and_offset_reject_booleans() -> None:
    """Verify that bool (even though isinstance(True, int)) is strictly rejected."""
    qb = Query.table("users")

    with pytest.raises(TypeError, match="LIMIT must be an integer"):
        qb.limit(True)

    with pytest.raises(TypeError, match="LIMIT must be an integer"):
        qb.limit(False)

    with pytest.raises(TypeError, match="OFFSET must be an integer"):
        qb.offset(True)

    with pytest.raises(TypeError, match="OFFSET must be an integer"):
        qb.offset(False)


def test_limit_and_offset_reject_negatives_and_invalid_types() -> None:
    """Verify negative numbers and non-integers raise appropriate exceptions."""
    qb = Query.table("users")

    with pytest.raises(ValueError, match="LIMIT cannot be negative"):
        qb.limit(-1)

    with pytest.raises(ValueError, match="OFFSET cannot be negative"):
        qb.offset(-10)

    with pytest.raises(TypeError, match="LIMIT must be an integer"):
        qb.limit("10")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="OFFSET must be an integer"):
        qb.offset(3.14)  # type: ignore[arg-type]


def test_take_and_skip_aliases() -> None:
    """Verify Laravel-style take() and skip() aliases match limit() and offset()."""
    qb = Query.table("users").take(15).skip(30)
    sql, bindings = qb.to_sql()

    assert "LIMIT $1 OFFSET $2" in sql
    assert bindings == [15, 30]


# ============================================================================
# 2. Paginator Boundary Conditions & Input Validation Tests
# ============================================================================


@pytest.mark.parametrize(
    ("total", "per_page", "page", "expected_last_page"),
    [
        (0, 10, 1, 1),
        (10, 10, 1, 1),
        (11, 10, 1, 2),
        (25, 10, 2, 3),
        (25, 10, 3, 3),
        (5, 1, 1, 5),
        (20, 10, 1, 2),
        (30, 10, 3, 3),
        (1, 10, 1, 1),
    ],
)
def test_paginator_last_page_calculation(
    total: int,
    per_page: int,
    page: int,
    expected_last_page: int,
) -> None:
    """Verify last_page calculation directly across total counts and per_page configurations."""
    paginator: Paginator[Any] = Paginator(
        items=[],
        total=total,
        per_page=per_page,
        current_page=page,
    )
    assert paginator.last_page == expected_last_page


def test_paginator_page_1_full_page() -> None:
    """Verify Page 1 with a full page of items has correct indices and navigation."""
    items = [{"id": i} for i in range(1, 11)]
    paginator = Paginator(items=items, total=25, per_page=10, current_page=1)

    assert paginator.current_page == 1
    assert paginator.per_page == 10
    assert paginator.total == 25
    assert paginator.last_page == 3
    assert paginator.has_more_pages is True
    assert paginator.has_previous_page is False
    assert paginator.first_item == 1
    assert paginator.last_item == 10
    assert len(paginator) == 10


def test_paginator_last_page_fewer_records() -> None:
    """Verify last page with fewer records than per_page."""
    items = [{"id": i} for i in range(21, 26)]  # 5 items
    paginator = Paginator(items=items, total=25, per_page=10, current_page=3)

    assert paginator.current_page == 3
    assert paginator.last_page == 3
    assert paginator.has_more_pages is False
    assert paginator.has_previous_page is True
    assert paginator.first_item == 21
    assert paginator.last_item == 25
    assert len(paginator) == 5


def test_paginator_page_beyond_last_page() -> None:
    """Verify requesting a page beyond last_page preserves page number and returns empty results."""
    paginator: Paginator[Any] = Paginator(items=[], total=15, per_page=10, current_page=5)

    assert paginator.current_page == 5
    assert paginator.last_page == 2
    assert paginator.has_more_pages is False
    assert paginator.has_previous_page is True
    assert paginator.first_item is None
    assert paginator.last_item is None
    assert len(paginator) == 0

    payload = paginator.to_dict()
    assert payload["current_page"] == 5
    assert payload["last_page"] == 2
    assert payload["from"] is None
    assert payload["to"] is None
    assert payload["data"] == []


def test_paginator_per_page_equals_one() -> None:
    """Verify pagination when per_page = 1."""
    items = [{"id": 2}]
    paginator = Paginator(items=items, total=3, per_page=1, current_page=2)

    assert paginator.current_page == 2
    assert paginator.last_page == 3
    assert paginator.first_item == 2
    assert paginator.last_item == 2
    assert paginator.has_more_pages is True
    assert paginator.has_previous_page is True


def test_paginator_total_zero_last_page_one() -> None:
    """Verify total = 0 defaults last_page to 1 with None item range."""
    paginator: Paginator[Any] = Paginator(items=[], total=0, per_page=15, current_page=1)

    assert paginator.total == 0
    assert paginator.last_page == 1
    assert paginator.has_more_pages is False
    assert paginator.has_previous_page is False
    assert paginator.first_item is None
    assert paginator.last_item is None


def test_paginator_exact_multiple_of_per_page() -> None:
    """Verify exact multiple of per_page (e.g. 20 items with 10 per page -> 2 pages)."""
    items = [{"id": i} for i in range(11, 21)]
    paginator = Paginator(items=items, total=20, per_page=10, current_page=2)

    assert paginator.last_page == 2
    assert paginator.has_more_pages is False
    assert paginator.has_previous_page is True
    assert paginator.first_item == 11
    assert paginator.last_item == 20


def test_paginator_reject_invalid_inputs() -> None:
    """Verify Paginator validates constructor arguments and rejects invalid values."""
    with pytest.raises(TypeError, match="per_page must be an integer"):
        Paginator(items=[], total=10, per_page=True, current_page=1)

    with pytest.raises(TypeError, match="per_page must be an integer"):
        Paginator(items=[], total=10, per_page=3.14, current_page=1)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="per_page must be >= 1"):
        Paginator(items=[], total=10, per_page=0, current_page=1)

    with pytest.raises(ValueError, match="per_page must be >= 1"):
        Paginator(items=[], total=10, per_page=-5, current_page=1)

    with pytest.raises(TypeError, match="current_page must be an integer"):
        Paginator(items=[], total=10, per_page=10, current_page=False)

    with pytest.raises(ValueError, match="current_page must be >= 1"):
        Paginator(items=[], total=10, per_page=10, current_page=0)

    with pytest.raises(TypeError, match="total must be an integer"):
        Paginator(items=[], total=True, per_page=10, current_page=1)

    with pytest.raises(ValueError, match="total cannot be negative"):
        Paginator(items=[], total=-1, per_page=10, current_page=1)

    with pytest.raises(ValueError, match="last_page must be >= 1"):
        Paginator(items=[], total=10, per_page=10, current_page=1, last_page=0)


def test_simple_paginator_boundary_and_validation() -> None:
    """Verify SimplePaginator boundary conditions, empty results, and input validation."""
    empty: SimplePaginator[Any] = SimplePaginator(items=[], per_page=10, current_page=1, has_more=False)
    assert empty.first_item is None
    assert empty.last_item is None
    assert empty.has_more_pages is False
    assert empty.has_previous_page is False
    assert empty.to_dict()["from"] is None
    assert empty.to_dict()["to"] is None

    items = ["item1", "item2"]
    simple = SimplePaginator(items=items, per_page=5, current_page=2, has_more=True)
    assert simple.first_item == 6
    assert simple.last_item == 7
    assert simple.has_more_pages is True
    assert simple.has_previous_page is True

    with pytest.raises(ValueError, match="per_page must be >= 1"):
        SimplePaginator(items=[], per_page=0, current_page=1, has_more=False)

    with pytest.raises(TypeError, match="per_page must be an integer"):
        SimplePaginator(items=[], per_page=True, current_page=1, has_more=False)

    with pytest.raises(ValueError, match="current_page must be >= 1"):
        SimplePaginator(items=[], per_page=10, current_page=0, has_more=False)

    with pytest.raises(TypeError, match="has_more must be a boolean"):
        SimplePaginator(items=[], per_page=10, current_page=1, has_more="yes")  # type: ignore[arg-type]


# ============================================================================
# 3. GROUP BY and HAVING Compilation Edge Cases
# ============================================================================


def test_group_by_single_and_multiple_columns() -> None:
    """Verify group_by correctly quotes identifiers and formats SQL."""
    qb1 = Query.table("orders").select("status").group_by("status")
    sql1, bindings1 = qb1.to_sql()
    assert 'SELECT "status" FROM "orders" GROUP BY "status"' in sql1
    assert bindings1 == []

    qb2 = Query.table("orders").select("status", "country").group_by("status", "country")
    sql2, _ = qb2.to_sql()
    assert 'GROUP BY "status", "country"' in sql2

    qb3 = Query.table("orders").select("status", "country").group_by(["status", "country"])
    sql3, _ = qb3.to_sql()
    assert 'GROUP BY "status", "country"' in sql3


def test_group_by_multiple_calls_accumulates_columns() -> None:
    """Verify sequential group_by() calls accumulate columns rather than replacing them."""
    qb = Query.table("orders").select("status", "country").group_by("status").group_by("country")
    sql, _ = qb.to_sql()
    assert 'GROUP BY "status", "country"' in sql


def test_group_by_deduplicates_columns() -> None:
    """Verify duplicate group columns are deduplicated while preserving order."""
    qb = Query.table("orders").select("status", "country").group_by("status", "country", "status")
    sql, _ = qb.to_sql()
    assert 'GROUP BY "status", "country"' in sql
    assert sql.count('"status"') == 2  # Once in SELECT, once in GROUP BY


def test_group_by_empty_calls_and_validation() -> None:
    """Verify calling group_by() with no arguments is a no-op, while invalid columns raise."""
    qb = Query.table("orders").select("status").group_by()
    sql, _ = qb.to_sql()
    assert "GROUP BY" not in sql

    with pytest.raises(ValueError):
        Query.table("orders").group_by("")

    with pytest.raises(TypeError):
        Query.table("orders").group_by(123)  # type: ignore[arg-type]


def test_group_by_qualified_identifiers() -> None:
    """Verify qualified column identifiers like orders.status are safely quoted."""
    qb = Query.table("orders").select("orders.status").group_by("orders.status")
    sql, _ = qb.to_sql()
    assert 'GROUP BY "orders"."status"' in sql


def test_group_by_identifier_injection_rejected() -> None:
    """Verify malicious SQL injection in group_by column raises ValueError."""
    bad_columns = [
        "status; DROP TABLE orders; --",
        "status' OR '1'='1",
        "status\n",
        "col/*comment*/",
    ]
    for bad in bad_columns:
        with pytest.raises(ValueError):
            Query.table("orders").group_by(bad)


def test_aggregate_without_group_by() -> None:
    """Verify aggregate selection works normally without a GROUP BY clause."""
    qb = Query.table("orders").select_sum("amount", alias="total_revenue")
    sql, bindings = qb.to_sql()
    assert 'SELECT SUM("amount") AS "total_revenue" FROM "orders"' in sql
    assert "GROUP BY" not in sql
    assert bindings == []


def test_aggregate_selection_helpers() -> None:
    """Verify select_count, select_sum, select_avg, select_min, select_max, select_raw."""
    qb = (
        Query.table("orders")
        .select("status")
        .select_count("id", alias="total_orders")
        .select_sum("amount", alias="total_amount")
        .select_avg("amount", alias="avg_amount")
        .select_min("amount", alias="min_amount")
        .select_max("amount", alias="max_amount")
        .group_by("status")
    )
    sql, bindings = qb.to_sql()

    assert 'SELECT "status", COUNT("id") AS "total_orders", SUM("amount") AS "total_amount", AVG("amount") AS "avg_amount", MIN("amount") AS "min_amount", MAX("amount") AS "max_amount"' in sql
    assert 'FROM "orders"' in sql
    assert 'GROUP BY "status"' in sql
    assert bindings == []


def test_select_raw_and_bindings() -> None:
    """Verify select_raw appends custom expressions and binds parameters properly."""
    qb = (
        Query.table("orders")
        .select_raw("DATE_TRUNC('month', created_at) AS month")
        .select_raw("COUNT(*) FILTER (WHERE amount > ?) AS high_val", [1000])
        .group_by("month")
    )
    sql, bindings = qb.to_sql()
    assert "DATE_TRUNC('month', created_at) AS month" in sql
    assert "COUNT(*) FILTER (WHERE amount > $1) AS high_val" in sql
    assert bindings == [1000]


def test_having_resolves_select_alias_and_parameterizes_values() -> None:
    """Verify HAVING clause resolves aliases created via select_count to aggregate expressions."""
    qb = (
        Query.table("orders")
        .select("status")
        .select_count("id", alias="total")
        .group_by("status")
        .having("total", ">", 10)
    )
    sql, bindings = qb.to_sql()

    assert 'HAVING COUNT("id") > $1' in sql
    assert bindings == [10]


def test_multiple_having_conditions_and_or() -> None:
    """Verify multiple HAVING conditions with AND/OR operators and binding alignment."""
    qb = (
        Query.table("orders")
        .select("status")
        .select_count("id", alias="total")
        .group_by("status")
        .having("total", ">", 5)
        .or_having("total", "<", 2)
    )
    sql, bindings = qb.to_sql()
    assert 'HAVING COUNT("id") > $1 OR COUNT("id") < $2' in sql
    assert bindings == [5, 2]


def test_having_raw_clause() -> None:
    """Verify having_raw parameterizes ? placeholders."""
    qb = (
        Query.table("orders")
        .select("status")
        .group_by("status")
        .having_raw("COUNT(id) > ? AND SUM(amount) < ?", [5, 50000])
    )
    sql, bindings = qb.to_sql()
    assert "HAVING COUNT(id) > $1 AND SUM(amount) < $2" in sql
    assert bindings == [5, 50000]


def test_having_raw_malicious_input_is_parameterized() -> None:
    """Verify having_raw binds values rather than interpolating them."""
    malicious = "5; DROP TABLE users; --"
    qb = Query.table("orders").group_by("status").having_raw("COUNT(id) > ?", [malicious])
    sql, bindings = qb.to_sql()
    assert "HAVING COUNT(id) > $1" in sql
    assert bindings == [malicious]
    assert ";" not in sql


def test_having_unknown_identifier_is_quoted_or_rejected() -> None:
    """Verify HAVING column that is not an alias is quoted as an identifier, and invalid identifier syntax raises."""
    qb = Query.table("orders").group_by("status").having("custom_column", ">", 10)
    sql, _ = qb.to_sql()
    assert 'HAVING "custom_column" > $1' in sql

    with pytest.raises(ValueError):
        Query.table("orders").group_by("status").having("bad; col", ">", 10)


def test_clause_order_in_sql_compilation() -> None:
    """Verify clauses compile in strict standard order: SELECT -> FROM -> JOIN -> WHERE -> GROUP BY -> HAVING -> ORDER BY -> LIMIT -> OFFSET."""
    qb = (
        Query.table("orders")
        .select("orders.status")
        .select_count("orders.id", alias="total")
        .join("users", "orders.user_id", "=", "users.id")
        .where("users.active", True)
        .group_by("orders.status")
        .having("total", ">", 5)
        .order_by("orders.status", "DESC")
        .limit(10)
        .offset(20)
    )
    sql, bindings = qb.to_sql()

    pos_select = sql.index("SELECT")
    pos_from = sql.index("FROM")
    pos_join = sql.index("JOIN")
    pos_where = sql.index("WHERE")
    pos_group = sql.index("GROUP BY")
    pos_having = sql.index("HAVING")
    pos_order = sql.index("ORDER BY")
    pos_limit = sql.index("LIMIT")
    pos_offset = sql.index("OFFSET")

    assert pos_select < pos_from < pos_join < pos_where < pos_group < pos_having < pos_order < pos_limit < pos_offset
    assert bindings == [True, 5, 10, 20]


# ============================================================================
# 4. Deep Query Cloning and State Isolation Tests
# ============================================================================


def test_deep_query_cloning_nested_state_isolation() -> None:
    """
    Verify that cloning a complex query (SELECT, raw SQL, WHERE, GROUP BY, HAVING,
    ORDER BY, LIMIT, OFFSET) creates an isolated copy with zero shared mutable state.
    """
    base = (
        Query.table("orders")
        .select("status")
        .select_count("id", alias="total")
        .where("country", "NG")
        .group_by("status")
        .having("total", ">", 5)
        .order_by("status")
        .limit(20)
        .offset(10)
    )
    sql_base_orig, bindings_base_orig = base.to_sql()

    # Clone base query
    cloned = base.clone()

    # Apply extensive mutations to the clone
    cloned.where("amount", ">", 100)
    cloned.group_by("country")
    cloned.having("total", "<", 50)
    cloned.limit(5).offset(0)
    cloned.select_raw("SUM(amount) AS sum_amt", [99])
    cloned.having_raw("MAX(amount) > ?", [500])

    sql_cloned, bindings_cloned = cloned.to_sql()

    # Assert base query remains 100% untouched
    assert base.to_sql() == (sql_base_orig, bindings_base_orig)
    assert "amount" not in sql_base_orig
    assert "country" not in sql_base_orig.split("GROUP BY")[1]
    assert bindings_base_orig == ["NG", 5, 20, 10]

    # Assert clone contains all new mutations and independent bindings
    assert "amount" in sql_cloned
    assert "country" in sql_cloned.split("GROUP BY")[1]
    assert bindings_cloned != bindings_base_orig


# ============================================================================
# 5. Database Pagination & Simple Pagination Execution Tests (Mock DB Pool)
# ============================================================================


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


@pytest.mark.asyncio
async def test_paginate_execution_count_and_data_queries(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    """Verify paginate() executes a count query without limit/offset and a data query with limit/offset."""
    mock_conn, _ = mock_db_pool

    executed_queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_fetch(sql: str, *bindings: Any) -> list[dict[str, Any]]:
        executed_queries.append((sql, bindings))
        if "agg_result" in sql:
            # Ungrouped count query response
            return [{"agg_result": 42}]
        # Data query response
        return [{"id": 1, "status": "paid"}, {"id": 2, "status": "paid"}]

    mock_conn.fetch = AsyncMock(side_effect=fake_fetch)

    paginator = (
        await Query.table("orders")
        .where("status", "paid")
        .order_by("created_at", "DESC")
        .paginate(per_page=10, page=2)
    )

    assert len(executed_queries) == 2

    # Query 1: Count query
    count_sql, count_bindings = executed_queries[0]
    assert "COUNT(*)" in count_sql
    assert "LIMIT" not in count_sql
    assert "OFFSET" not in count_sql
    assert "ORDER BY" not in count_sql
    assert 'WHERE "status" = $1' in count_sql
    assert count_bindings == ("paid",)

    # Query 2: Data query
    data_sql, data_bindings = executed_queries[1]
    assert "ORDER BY" in data_sql
    assert "LIMIT $2 OFFSET $3" in data_sql
    assert data_bindings == ("paid", 10, 10)  # offset = (2 - 1) * 10 = 10

    # Paginator metadata
    assert paginator.total == 42
    assert paginator.current_page == 2
    assert paginator.per_page == 10
    assert paginator.last_page == 5
    assert paginator.has_more_pages is True
    assert paginator.has_previous_page is True
    assert len(paginator.items) == 2


@pytest.mark.asyncio
async def test_simple_paginate_fetches_extra_record_and_no_count_query(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    """Verify simple_paginate fetches per_page + 1 records, pops extra record, and runs NO count query."""
    mock_conn, _ = mock_db_pool

    executed_queries: list[tuple[str, tuple[Any, ...]]] = []

    # Scenario A: 11 records returned for per_page = 10 -> has_more_pages = True
    async def fake_fetch_11(sql: str, *bindings: Any) -> list[dict[str, Any]]:
        executed_queries.append((sql, bindings))
        return [{"id": i, "status": "paid"} for i in range(1, 12)]  # 11 records

    mock_conn.fetch = AsyncMock(side_effect=fake_fetch_11)

    paginator_has_more = (
        await Query.table("orders")
        .where("status", "paid")
        .simple_paginate(per_page=10, page=1)
    )

    # Assert exactly 1 query was executed (NO count query)
    assert len(executed_queries) == 1
    data_sql, data_bindings = executed_queries[0]
    assert "LIMIT $2 OFFSET $3" in data_sql
    assert data_bindings == ("paid", 11, 0)  # per_page + 1 = 11, offset = 0

    assert len(paginator_has_more.items) == 10  # 11th record popped
    assert paginator_has_more.has_more_pages is True
    assert paginator_has_more.has_previous_page is False

    # Scenario B: 8 records returned for per_page = 10 -> has_more_pages = False
    executed_queries.clear()

    async def fake_fetch_8(sql: str, *bindings: Any) -> list[dict[str, Any]]:
        executed_queries.append((sql, bindings))
        return [{"id": i, "status": "paid"} for i in range(1, 9)]  # 8 records

    mock_conn.fetch = AsyncMock(side_effect=fake_fetch_8)

    paginator_no_more = (
        await Query.table("orders")
        .where("status", "paid")
        .simple_paginate(per_page=10, page=2)
    )

    assert len(executed_queries) == 1
    assert len(paginator_no_more.items) == 8
    assert paginator_no_more.has_more_pages is False
    assert paginator_no_more.has_previous_page is True


@pytest.mark.asyncio
async def test_grouped_pagination_subquery_count_execution(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    """
    Verify grouped query pagination uses a subquery for counting groups rather
    than incorrectly counting underlying table rows.
    """
    mock_conn, _ = mock_db_pool

    executed_fetchvals: list[tuple[str, tuple[Any, ...]]] = []
    executed_fetches: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_fetchval(sql: str, *bindings: Any) -> int:
        executed_fetchvals.append((sql, bindings))
        return 3  # 3 distinct groups surviving HAVING

    async def fake_fetch(sql: str, *bindings: Any) -> list[dict[str, Any]]:
        executed_fetches.append((sql, bindings))
        return [
            {"country": "NG", "total": 12},
            {"country": "US", "total": 8},
        ]

    mock_conn.fetchval = AsyncMock(side_effect=fake_fetchval)
    mock_conn.fetch = AsyncMock(side_effect=fake_fetch)

    paginator = (
        await Query.table("orders")
        .select("country")
        .select_count("id", alias="total")
        .group_by("country")
        .having("total", ">", 5)
        .order_by("total", "DESC")
        .paginate(per_page=2, page=1)
    )

    # 1. Count query verified as subquery
    assert len(executed_fetchvals) == 1
    count_sql, count_bindings = executed_fetchvals[0]

    assert "SELECT COUNT(*) AS total FROM (" in count_sql
    assert 'GROUP BY "country"' in count_sql
    assert 'HAVING COUNT("id") > $1' in count_sql
    assert ") AS grouped_results" in count_sql
    assert "ORDER BY" not in count_sql
    assert "LIMIT" not in count_sql
    assert count_bindings == (5,)

    # 2. Data query verified
    assert len(executed_fetches) == 1
    data_sql, data_bindings = executed_fetches[0]
    assert "LIMIT $2 OFFSET $3" in data_sql
    assert data_bindings == (5, 2, 0)

    # 3. Paginator result verified
    assert paginator.total == 3
    assert paginator.last_page == 2
    assert paginator.has_more_pages is True
    assert len(paginator.items) == 2


@pytest.mark.asyncio
async def test_model_paginate_and_simple_paginate_execution(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    """Verify Model.paginate() and Model.simple_paginate() hydrate model instances."""
    mock_conn, _ = mock_db_pool

    async def fake_fetch(sql: str, *bindings: Any) -> list[dict[str, Any]]:
        if "agg_result" in sql:
            return [{"agg_result": 2}]
        return [
            {"id": 1, "status": "paid", "country": "NG", "amount": 100.0},
            {"id": 2, "status": "paid", "country": "US", "amount": 200.0},
        ]

    mock_conn.fetch = AsyncMock(side_effect=fake_fetch)

    # Model paginate
    paginator = await SampleOrder.where("status", "paid").paginate(per_page=10, page=1)
    assert isinstance(paginator.items[0], SampleOrder)
    assert paginator.items[0].id == 1
    assert paginator.items[0].status == "paid"
    assert paginator.total == 2

    # Model simple_paginate
    simple_pag = await SampleOrder.where("status", "paid").simple_paginate(per_page=10, page=1)
    assert isinstance(simple_pag.items[0], SampleOrder)
    assert simple_pag.items[0].country == "NG"
