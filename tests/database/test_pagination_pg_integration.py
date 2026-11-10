from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from swiftpy.database.orm.model import Model
from swiftpy.database.pagination import Paginator, SimplePaginator
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query


class OrderRecord(Model):
    __table__ = "test_orders"
    __fillable__ = ("id", "status", "country", "amount")

    id: int
    status: str
    country: str
    amount: float


@pytest.fixture
async def orders_table(
    ensure_db_pool: DatabasePool | None,
) -> AsyncGenerator[str, None]:
    """Create test_orders table populated with the standard test dataset."""
    if ensure_db_pool is None:
        pytest.skip("PostgreSQL service is not available in local test environment")

    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS "test_orders" CASCADE;
            CREATE TABLE "test_orders" (
                id INT PRIMARY KEY,
                status VARCHAR(50) NOT NULL,
                country VARCHAR(10) NOT NULL,
                amount NUMERIC(10, 2) NOT NULL
            );
            INSERT INTO "test_orders" (id, status, country, amount) VALUES
                (1, 'paid', 'NG', 100.00),
                (2, 'paid', 'NG', 200.00),
                (3, 'pending', 'NG', 150.00),
                (4, 'paid', 'US', 300.00),
                (5, 'pending', 'US', 250.00),
                (6, 'paid', 'NG', 400.00);
            """
        )

    try:
        yield "test_orders"
    finally:
        async with pool.acquire() as conn:
            await conn.execute('DROP TABLE IF EXISTS "test_orders" CASCADE;')


@pytest.mark.asyncio
async def test_pg_limit_and_offset(orders_table: str) -> None:
    """Verify limit and offset execution against live PostgreSQL."""
    rows = (
        await Query.table("test_orders")
        .select("id")
        .order_by("id", "ASC")
        .limit(2)
        .offset(1)
        .get()
    )
    assert [r["id"] for r in rows] == [2, 3]

    rows_alias = (
        await Query.table("test_orders")
        .select("id")
        .order_by("id", "ASC")
        .take(3)
        .skip(3)
        .get()
    )
    assert [r["id"] for r in rows_alias] == [4, 5, 6]


@pytest.mark.asyncio
async def test_pg_length_aware_pagination(orders_table: str) -> None:
    """Verify paginate() retrieves pages, calculates totals, and hydrates models."""
    paginator: Paginator[OrderRecord] = (
        await OrderRecord.where("status", "paid")
        .order_by("id", "ASC")
        .paginate(per_page=2, page=1)
    )

    assert paginator.total == 4
    assert paginator.per_page == 2
    assert paginator.current_page == 1
    assert paginator.last_page == 2
    assert paginator.has_more_pages is True
    assert paginator.has_previous_page is False
    assert paginator.first_item == 1
    assert paginator.last_item == 2
    assert [item.id for item in paginator.items] == [1, 2]

    # Page 2
    page2 = (
        await OrderRecord.where("status", "paid")
        .order_by("id", "ASC")
        .paginate(per_page=2, page=2)
    )
    assert page2.current_page == 2
    assert page2.has_more_pages is False
    assert page2.has_previous_page is True
    assert page2.first_item == 3
    assert page2.last_item == 4
    assert [item.id for item in page2.items] == [4, 6]


@pytest.mark.asyncio
async def test_pg_simple_pagination(orders_table: str) -> None:
    """Verify simple_paginate() handles extra-record pagination flags without count query."""
    paginator: SimplePaginator[OrderRecord] = (
        await OrderRecord.order_by("id", "ASC").simple_paginate(per_page=2, page=1)
    )

    assert paginator.per_page == 2
    assert paginator.current_page == 1
    assert paginator.has_more_pages is True
    assert paginator.has_previous_page is False
    assert [item.id for item in paginator.items] == [1, 2]

    # Page 3 (last page of 6 items)
    page3 = (
        await OrderRecord.order_by("id", "ASC").simple_paginate(per_page=2, page=3)
    )
    assert page3.current_page == 3
    assert page3.has_more_pages is False
    assert page3.has_previous_page is True
    assert [item.id for item in page3.items] == [5, 6]


@pytest.mark.asyncio
async def test_pg_grouped_counts(orders_table: str) -> None:
    """Verify GROUP BY aggregation executes accurately on PostgreSQL."""
    rows = (
        await Query.table("test_orders")
        .select("country")
        .select_count("id", alias="order_count")
        .group_by("country")
        .order_by("country", "ASC")
        .get()
    )
    assert len(rows) == 2
    assert rows[0] == {"country": "NG", "order_count": 4}
    assert rows[1] == {"country": "US", "order_count": 2}


@pytest.mark.asyncio
async def test_pg_multiple_group_columns(orders_table: str) -> None:
    """Verify GROUP BY with multiple columns partitions rows properly."""
    rows = (
        await Query.table("test_orders")
        .select("country", "status")
        .select_count("id", alias="total")
        .group_by("country", "status")
        .order_by("country", "ASC")
        .get()
    )
    # Expected: (NG, paid: 3), (NG, pending: 1), (US, paid: 1), (US, pending: 1)
    group_map = {(r["country"], r["status"]): r["total"] for r in rows}
    assert group_map[("NG", "paid")] == 3
    assert group_map[("NG", "pending")] == 1
    assert group_map[("US", "paid")] == 1
    assert group_map[("US", "pending")] == 1


@pytest.mark.asyncio
async def test_pg_having(orders_table: str) -> None:
    """Verify HAVING filters aggregated groups in PostgreSQL."""
    rows = (
        await Query.table("test_orders")
        .select("country")
        .select_count("id", alias="total")
        .group_by("country")
        .having("total", ">", 2)
        .get()
    )
    # Only NG has > 2 orders
    assert len(rows) == 1
    assert rows[0]["country"] == "NG"
    assert rows[0]["total"] == 4


@pytest.mark.asyncio
async def test_pg_grouped_pagination(orders_table: str) -> None:
    """
    Verify grouped query pagination counts the number of surviving groups via
    subquery rather than counting underlying table rows.
    """
    paginator: Paginator[dict[str, Any]] = (
        await Query.table("test_orders")
        .select("country")
        .select_count("id", alias="total")
        .group_by("country")
        .having("total", ">=", 2)
        .order_by("country", "ASC")
        .paginate(per_page=1, page=1)
    )

    # 2 groups total (NG and US), each has >= 2 orders
    assert paginator.total == 2
    assert paginator.last_page == 2
    assert paginator.has_more_pages is True
    assert paginator.items[0]["country"] == "NG"

    page2 = (
        await Query.table("test_orders")
        .select("country")
        .select_count("id", alias="total")
        .group_by("country")
        .having("total", ">=", 2)
        .order_by("country", "ASC")
        .paginate(per_page=1, page=2)
    )
    assert page2.total == 2
    assert page2.has_more_pages is False
    assert page2.items[0]["country"] == "US"


@pytest.mark.asyncio
async def test_pg_empty_results(orders_table: str) -> None:
    """Verify pagination against empty results produces valid metadata."""
    paginator = (
        await OrderRecord.where("status", "non_existent").paginate(per_page=5, page=1)
    )
    assert paginator.total == 0
    assert paginator.last_page == 1
    assert paginator.items == []
    assert paginator.first_item is None
    assert paginator.last_item is None
    assert paginator.has_more_pages is False
    assert paginator.has_previous_page is False


@pytest.mark.asyncio
async def test_pg_page_beyond_last_page(orders_table: str) -> None:
    """Verify requesting a page beyond last page preserves page number with empty items."""
    paginator = (
        await OrderRecord.order_by("id", "ASC").paginate(per_page=2, page=10)
    )
    assert paginator.total == 6
    assert paginator.last_page == 3
    assert paginator.current_page == 10
    assert paginator.items == []
    assert paginator.first_item is None
    assert paginator.last_item is None
    assert paginator.has_more_pages is False
    assert paginator.has_previous_page is True


@pytest.mark.asyncio
async def test_pg_pagination_does_not_mutate_query(orders_table: str) -> None:
    """Verify calling paginate() on a base query leaves it unmutated and reusable."""
    base_query = OrderRecord.where("status", "paid").order_by("id", "ASC")

    # Run pagination on page 1
    page1 = await base_query.paginate(per_page=2, page=1)
    assert len(page1.items) == 2

    # Execute original query without pagination; all 4 paid rows returned
    all_paid = await base_query.get()
    assert len(all_paid) == 4
    assert [o.id for o in all_paid] == [1, 2, 4, 6]

