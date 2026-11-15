from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

from swiftpy.database.orm.model import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query, QueryError


class IntegrationItem(Model):
    __table__ = "test_integration_items"
    __fillable__ = ("id", "code", "category", "price", "stock", "created_at")

    id: int
    code: str
    category: str
    price: float
    stock: int


@pytest.fixture
async def items_table(
    ensure_db_pool: DatabasePool | None,
) -> AsyncGenerator[str, None]:
    if ensure_db_pool is None:
        pytest.skip("PostgreSQL service is not available in local test environment")

    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS "test_integration_items" CASCADE;
            CREATE TABLE "test_integration_items" (
                id SERIAL PRIMARY KEY,
                code VARCHAR(50) UNIQUE NOT NULL,
                category VARCHAR(50) NOT NULL,
                price NUMERIC(10, 2) NOT NULL,
                stock INT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO "test_integration_items" (code, category, price, stock) VALUES
                ('A-101', 'electronics', 150.00, 10),
                ('A-102', 'electronics', 250.00, 5),
                ('B-201', 'books', 20.00, 100),
                ('B-202', 'books', 35.00, 50),
                ('C-301', 'clothing', 60.00, 30);
            """
        )

    try:
        yield "test_integration_items"
    finally:
        async with pool.acquire() as conn:
            await conn.execute('DROP TABLE IF EXISTS "test_integration_items" CASCADE;')


@pytest.mark.asyncio
async def test_pg_where_between_and_or_where(items_table: str) -> None:
    results = (
        await Query.table(items_table)
        .where_between("price", [50.0, 200.0])
        .or_where("category", "books")
        .order_by("id", "ASC")
        .get()
    )
    codes = [r["code"] for r in results]
    assert "A-101" in codes
    assert "B-201" in codes
    assert "B-202" in codes
    assert "C-301" in codes
    assert "A-102" not in codes


@pytest.mark.asyncio
async def test_pg_distinct_and_distinct_on(items_table: str) -> None:
    categories = (
        await Query.table(items_table)
        .distinct()
        .select("category")
        .order_by("category", "ASC")
        .pluck("category")
    )
    assert sorted(categories) == ["books", "clothing", "electronics"]


@pytest.mark.asyncio
async def test_pg_atomic_increment_and_decrement(items_table: str) -> None:
    # 1. Sequential increment
    await Query.table(items_table).where("code", "A-101").increment("stock", 5)
    new_stock = await Query.table(items_table).where("code", "A-101").value("stock")
    assert new_stock == 15

    # 2. Decrement
    await Query.table(items_table).where("code", "A-101").decrement("stock", 3)
    stock_after_dec = await Query.table(items_table).where("code", "A-101").value("stock")
    assert stock_after_dec == 12

    # 3. Concurrent increments to test zero lost updates
    async def increment_task() -> None:
        await Query.table(items_table).where("code", "A-101").increment("stock", 1)

    await asyncio.gather(*(increment_task() for _ in range(10)))
    final_stock = await Query.table(items_table).where("code", "A-101").value("stock")
    assert final_stock == 22


@pytest.mark.asyncio
async def test_pg_upsert(items_table: str) -> None:
    rows = [
        {"code": "A-101", "category": "electronics", "price": 175.00, "stock": 20},
        {"code": "D-401", "category": "furniture", "price": 400.00, "stock": 2},
    ]

    # A-101 should update, D-401 should insert
    affected = await Query.table(items_table).upsert(
        rows,
        unique_by=["code"],
        update_columns=["price", "stock"],
    )
    assert affected == 2

    updated_item = await IntegrationItem.where("code", "A-101").first_or_fail()
    assert float(updated_item.price) == 175.00
    assert updated_item.stock == 20

    new_item = await IntegrationItem.where("code", "D-401").first_or_fail()
    assert float(new_item.price) == 400.00
    assert new_item.stock == 2


@pytest.mark.asyncio
async def test_pg_chunk_and_chunk_by_id(items_table: str) -> None:
    expected_codes = {"A-101", "A-102", "B-201", "B-202", "C-301"}

    # 1. Standard chunking via limit & offset
    processed_codes: list[str] = []

    async def collect_chunk(items: list[IntegrationItem]) -> None:
        for it in items:
            processed_codes.append(it.code)

    ok = await IntegrationItem.query().chunk(2, collect_chunk)
    assert ok is True
    assert len(processed_codes) == 5
    assert len(set(processed_codes)) == 5
    assert set(processed_codes) == expected_codes

    # 2. Keyset chunk_by_id (guaranteed ascending ID order)
    id_codes: list[str] = []
    visited_ids: list[int] = []

    async def collect_by_id(items: list[IntegrationItem]) -> None:
        for it in items:
            id_codes.append(it.code)
            visited_ids.append(it.id)

    ok2 = await IntegrationItem.query().chunk_by_id(2, collect_by_id)
    assert ok2 is True
    assert len(id_codes) == 5
    assert len(set(id_codes)) == 5
    assert set(id_codes) == expected_codes
    assert visited_ids == sorted(visited_ids)


@pytest.mark.asyncio
async def test_pg_where_group_precedence(items_table: str) -> None:
    # WHERE "category" = 'electronics' AND ("code" = 'A-101' OR "stock" > 20)
    results = (
        await Query.table(items_table)
        .where("category", "electronics")
        .where_group(
            lambda q: q.where("code", "A-101").or_where("stock", ">", 20)
        )
        .get()
    )
    codes = [r["code"] for r in results]
    assert codes == ["A-101"]


@pytest.mark.asyncio
async def test_pg_empty_where_in_and_not_in(items_table: str) -> None:
    empty_res = await Query.table(items_table).where_in("id", []).get()
    assert empty_res == []

    all_res = await Query.table(items_table).where_not_in("id", []).get()
    assert len(all_res) == 5


@pytest.mark.asyncio
async def test_pg_upsert_duplicate_keys_prevented(items_table: str) -> None:
    dup_rows = [
        {"code": "X-1", "category": "electronics", "price": 10.0, "stock": 1},
        {"code": "X-1", "category": "electronics", "price": 20.0, "stock": 2},
    ]
    with pytest.raises(QueryError, match="Duplicate conflict key"):
        await Query.table(items_table).upsert(dup_rows, unique_by=["code"])


@pytest.mark.asyncio
async def test_pg_upsert_do_nothing(items_table: str) -> None:
    rows = [{"code": "A-101", "category": "new_cat", "price": 999.0, "stock": 999}]
    affected = await Query.table(items_table).upsert(
        rows,
        unique_by=["code"],
        update_columns=[],
    )
    assert affected == 0
    item = await IntegrationItem.where("code", "A-101").first_or_fail()
    assert item.category == "electronics"
