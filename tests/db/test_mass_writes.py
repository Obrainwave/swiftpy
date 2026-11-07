from __future__ import annotations

import pytest

from swiftpy.database.orm.model import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query, QueryError, escape_like


class MassWriteItem(Model):
    __table__ = "test_mass_write_items"
    __fillable__ = ("title", "tag", "views")

    id: int
    title: str
    tag: str | None
    views: int


@pytest.fixture(autouse=True)
async def setup_mass_writes_table(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS "test_mass_write_items" CASCADE;
            CREATE TABLE "test_mass_write_items" (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                tag TEXT DEFAULT NULL,
                views INT NOT NULL DEFAULT 0
            );
            INSERT INTO "test_mass_write_items" (title, tag, views) VALUES
                ('100% Pure_Python', 'lang', 10),
                ('100000 PureXPython', 'other', 20),
                ('Back\\slash_Item', 'path', 30);
            """
        )


@pytest.mark.asyncio
async def test_unguarded_update_and_delete_raise_unless_allow_all() -> None:
    # 1. QueryBuilder unguarded update and delete raise QueryError
    with pytest.raises(QueryError, match=r"(?i)where|allow_all"):
        await Query.table("test_mass_write_items").update({"views": 99})

    with pytest.raises(QueryError, match=r"(?i)where|allow_all"):
        await Query.table("test_mass_write_items").delete()

    # 2. ModelQueryBuilder unguarded update and delete raise QueryError
    with pytest.raises(QueryError, match=r"(?i)where|allow_all"):
        await MassWriteItem.query().update({"views": 99})

    with pytest.raises(QueryError, match=r"(?i)where|allow_all"):
        await MassWriteItem.query().delete()

    # Verify rows were not modified or deleted
    assert await MassWriteItem.query().count() == 3

    # 3. With .allow_all(), mass update and mass delete succeed
    await MassWriteItem.query().allow_all().update({"views": 77})
    rows = await MassWriteItem.all()
    assert all(r.views == 77 for r in rows)

    await Query.table("test_mass_write_items").allow_all().delete()
    assert await MassWriteItem.query().count() == 0


@pytest.mark.asyncio
async def test_empty_insert_and_update_raise_query_error() -> None:
    with pytest.raises(QueryError, match=r"(?i)empty"):
        await Query.table("test_mass_write_items").insert({})

    with pytest.raises(QueryError, match=r"(?i)empty"):
        await Query.table("test_mass_write_items").insert_returning({})

    with pytest.raises(QueryError, match=r"(?i)empty"):
        await Query.table("test_mass_write_items").where("id", 1).update({})


@pytest.mark.asyncio
async def test_in_and_not_in_empty_and_non_list_validation() -> None:
    # 1. Empty IN [] compiles to FALSE and returns no rows
    q_in_empty = Query.table("test_mass_write_items").where("id", "IN", [])
    sql_in, bindings_in = q_in_empty.to_sql()
    assert "WHERE FALSE" in sql_in
    assert bindings_in == []
    assert await q_in_empty.get() == []

    # 2. Empty NOT IN [] compiles to TRUE and returns all rows
    q_not_in_empty = Query.table("test_mass_write_items").where("id", "NOT IN", [])
    sql_not_in, bindings_not_in = q_not_in_empty.to_sql()
    assert "WHERE TRUE" in sql_not_in
    assert bindings_not_in == []
    assert len(await q_not_in_empty.get()) == 3

    # 3. Non-empty NOT IN works with bindings
    q_not_in = Query.table("test_mass_write_items").where("views", "NOT IN", [10, 30])
    sql_ni, bindings_ni = q_not_in.to_sql()
    assert '"views" NOT IN ($1, $2)' in sql_ni
    assert bindings_ni == [10, 30]
    rows = await q_not_in.get()
    assert len(rows) == 1
    assert rows[0]["views"] == 20

    # 4. Non-list raises QueryError
    for bad_value in ("not-a-list", 123, {"a": 1}, (1, 2), {1, 2}):
        with pytest.raises(QueryError, match=r"(?i)list"):
            Query.table("test_mass_write_items").where("id", "IN", bad_value)
        with pytest.raises(QueryError, match=r"(?i)list"):
            Query.table("test_mass_write_items").where("id", "NOT IN", bad_value)


def test_where_null_comparison_operators_raise() -> None:
    # '=' and '!=' / '<>' with None compile to IS NULL / IS NOT NULL
    q_eq = Query.table("test_mass_write_items").where("tag", None)
    sql_eq, bindings_eq = q_eq.to_sql()
    assert '"tag" IS NULL' in sql_eq
    assert bindings_eq == []

    q_ne = Query.table("test_mass_write_items").where("tag", "!=", None)
    sql_ne, bindings_ne = q_ne.to_sql()
    assert '"tag" IS NOT NULL' in sql_ne
    assert bindings_ne == []

    # Comparison operators with None must raise QueryError
    for op in ("<", ">", "<=", ">=", "LIKE", "ILIKE", "IN", "NOT IN"):
        with pytest.raises(QueryError):
            Query.table("test_mass_write_items").where("views", op, None)


@pytest.mark.asyncio
async def test_like_helper_escapes_wildcards_and_binds_parameters() -> None:
    assert escape_like(r"100% Pure_Python\test") == r"100\% Pure\_Python\\test"

    # Literal search for '100% Pure_Python' must match ONLY row 1, not '100000 PureXPython'
    q_like = Query.table("test_mass_write_items").where_like(
        "title", "100% Pure_Python"
    )
    sql, bindings = q_like.to_sql()
    assert r"ESCAPE '\'" in sql
    assert "100" not in sql  # Value never interpolated in SQL text
    assert bindings == [r"%100\% Pure\_Python%"]

    matched = await q_like.get()
    assert len(matched) == 1
    assert matched[0]["title"] == "100% Pure_Python"

    # Literal search for backslash + underscore via where_ilike
    q_ilike = MassWriteItem.query().where_ilike(
        "title", r"back\slash_item", match="exact"
    )
    sql_i, bindings_i = q_ilike.builder.to_sql()
    assert r"ILIKE $1 ESCAPE '\'" in sql_i
    assert "back" not in sql_i
    assert bindings_i == [r"back\\slash\_item"]

    matched_models = await q_ilike.get()
    assert len(matched_models) == 1
    assert matched_models[0].title == r"Back\slash_Item"
