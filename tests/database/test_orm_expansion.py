from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from swiftpy.core.container import get_container
from swiftpy.core.exceptions import ExceptionHandler
from swiftpy.database.exceptions import ModelNotFoundError, RecordNotFoundError
from swiftpy.database.orm import Model
from swiftpy.database.orm.fields import Field
from swiftpy.database.pool import DatabasePool


class SampleUser(Model):
    __table__ = "users"
    __primary_key__ = "id"
    __fillable__ = ("id", "name", "email", "age", "points", "status", "role")

    id: int
    name: str
    email: str
    age: int
    points: int
    status: str
    role: str


class SamplePost(Model):
    __table__ = "posts"
    __primary_key__ = "id"
    __fillable__ = ("id", "user_id", "title", "views")

    id: int
    user_id: int
    title: str
    views: int


class CustomUuidItem(Model):
    __table__ = "custom_uuid_items"
    __primary_key__ = "uuid"
    __fillable__ = ("uuid", "title")

    uuid: str = Field(primary_key=True)
    title: str


class CustomCodeItem(Model):
    __table__ = "custom_code_items"
    __primary_key__ = "code"
    __fillable__ = ("code", "name")

    code: str = Field(primary_key=True)
    name: str


@pytest.fixture
def mock_db_pool() -> tuple[AsyncMock, MagicMock]:
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
async def test_model_first_and_find_or_fail(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool

    # 1. find_or_fail success
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 1,
                "name": "Alice",
                "email": "alice@example.com",
                "age": 30,
                "points": 100,
                "status": "active",
                "role": "admin",
            }
        ]
    )
    user = await SampleUser.find_or_fail(1)
    assert isinstance(user, SampleUser)
    assert user.id == 1
    assert user.name == "Alice"

    # 2. find_or_fail raises ModelNotFoundError
    mock_conn.fetch = AsyncMock(return_value=[])
    with pytest.raises(ModelNotFoundError, match="No query results for model \\[SampleUser\\] with primary key \\[99\\]"):
        await SampleUser.find_or_fail(99)

    # 3. first_or_fail success
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 2,
                "name": "Bob",
                "email": "bob@example.com",
                "age": 25,
                "points": 50,
                "status": "active",
                "role": "user",
            }
        ]
    )
    bob = await SampleUser.where("name", "Bob").first_or_fail()
    assert isinstance(bob, SampleUser)
    assert bob.name == "Bob"

    # 4. first_or_fail raises ModelNotFoundError
    mock_conn.fetch = AsyncMock(return_value=[])
    with pytest.raises(ModelNotFoundError, match="No query results for model \\[SampleUser\\]"):
        await SampleUser.where("name", "Ghost").first_or_fail()


@pytest.mark.asyncio
async def test_model_aggregates_and_exists(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool

    # count
    mock_conn.fetch = AsyncMock(return_value=[{"agg_result": 42}])
    cnt = await SampleUser.count()
    assert cnt == 42

    # exists
    mock_conn.fetch = AsyncMock(return_value=[{"?column?": 1}])
    has_users = await SampleUser.exists()
    assert has_users is True

    # sum, avg, min, max
    mock_conn.fetch = AsyncMock(return_value=[{"agg_result": 500}])
    assert await SampleUser.sum("points") == 500

    mock_conn.fetch = AsyncMock(return_value=[{"agg_result": 28.5}])
    assert await SampleUser.avg("age") == 28.5

    mock_conn.fetch = AsyncMock(return_value=[{"agg_result": 18}])
    assert await SampleUser.min("age") == 18

    mock_conn.fetch = AsyncMock(return_value=[{"agg_result": 75}])
    assert await SampleUser.max("age") == 75


def test_model_query_builder_proxies_to_sql() -> None:
    # Model classmethod chaining to SQL verification
    query = (
        SampleUser.select("id", "name")
        .distinct()
        .where("status", "active")
        .or_where("role", "admin")
        .where_in("age", [20, 25, 30])
        .where_between("points", [50, 200])
        .or_where_null("banned_at")
        .latest()
    )
    sql, bindings = query.builder.to_sql()
    assert 'SELECT DISTINCT "id", "name" FROM "users"' in sql
    assert (
        'WHERE "status" = $1 OR "role" = $2 AND "age" IN ($3, $4, $5) AND "points" BETWEEN $6 AND $7 OR "banned_at" IS NULL'
        in sql
    )
    assert 'ORDER BY "created_at" DESC' in sql
    assert bindings == ["active", "admin", 20, 25, 30, 50, 200]


def test_model_joins_and_conditionals() -> None:
    qb = (
        SampleUser.join("orders", "orders.user_id", "=", "users.id")
        .left_join("profiles", "profiles.user_id", "=", "users.id")
        .when(True, lambda q, _: q.where("status", "active"))
        .unless(True, lambda q, _: q.where("status", "inactive"))
    )
    sql, bindings = qb.builder.to_sql()
    assert 'INNER JOIN "orders" ON "orders"."user_id" = "users"."id"' in sql
    assert 'LEFT JOIN "profiles" ON "profiles"."user_id" = "users"."id"' in sql
    assert 'WHERE "status" = $1' in sql
    assert bindings == ["active"]


@pytest.mark.asyncio
async def test_model_value_and_pluck(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    mock_conn.fetch = AsyncMock(
        return_value=[
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ]
    )

    val = await SampleUser.where("id", 1).value("name")
    assert val == "Alice"

    names = await SampleUser.pluck("name")
    assert names == ["Alice", "Bob"]

    name_map = await SampleUser.pluck("name", key="id")
    assert name_map == {1: "Alice", 2: "Bob"}


@pytest.mark.asyncio
async def test_model_first_or_create_and_update_or_create(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool

    # Scenario 1: first_or_create finds existing
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 1,
                "name": "Alice",
                "email": "alice@example.com",
                "age": 30,
                "points": 100,
                "status": "active",
                "role": "admin",
            }
        ]
    )
    user1 = await SampleUser.first_or_create(
        {"email": "alice@example.com"},
        {"name": "Alice", "status": "active"},
    )
    assert user1.id == 1
    assert user1.name == "Alice"

    # Scenario 2: first_or_create creates new
    mock_conn.fetch = AsyncMock(return_value=[])  # Not found
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "id": 10,
            "name": "Charlie",
            "email": "charlie@example.com",
            "age": 22,
            "points": 0,
            "status": "pending",
            "role": "user",
        }
    )
    user2 = await SampleUser.first_or_create(
        {"email": "charlie@example.com"},
        {"name": "Charlie", "status": "pending"},
    )
    assert user2.id == 10
    assert user2.email == "charlie@example.com"

    # Scenario 3: update_or_create updates existing
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 1,
                "name": "Alice",
                "email": "alice@example.com",
                "age": 30,
                "points": 100,
                "status": "active",
                "role": "admin",
            }
        ]
    )
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    user3 = await SampleUser.update_or_create(
        {"email": "alice@example.com"},
        {"name": "Alice Updated", "points": 150},
    )
    assert user3.name == "Alice Updated"
    assert user3.points == 150


@pytest.mark.asyncio
async def test_model_instance_increment_and_decrement(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool

    post = SamplePost(id=1, user_id=1, title="Hello World", views=10)
    post._exists = True
    post._original = {"id": 1, "user_id": 1, "title": "Hello World", "views": 10}

    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    # For refresh() call
    mock_conn.fetch = AsyncMock(
        return_value=[{"id": 1, "user_id": 1, "title": "Hello World", "views": 15}]
    )

    await post.increment("views", 5)
    assert post.views == 15

    # Decrement
    mock_conn.fetch = AsyncMock(
        return_value=[{"id": 1, "user_id": 1, "title": "Hello World", "views": 12}]
    )
    await post.decrement("views", 3)
    assert post.views == 12


@pytest.mark.asyncio
async def test_exception_handler_maps_model_and_record_not_found_to_404() -> None:
    handler = ExceptionHandler(debug=False)

    model_err = ModelNotFoundError(model=SampleUser, primary_key=42)
    resp1 = await handler.handle(model_err)
    assert resp1.status_code == 404
    assert "No query results for model [SampleUser] with primary key [42]" in resp1.content.decode()

    rec_err = RecordNotFoundError("Custom record not found")
    resp2 = await handler.handle(rec_err)
    assert resp2.status_code == 404
    assert "Custom record not found" in resp2.content.decode()


def test_model_where_group_and_or_where_group() -> None:
    query = (
        SampleUser.where("status", "active")
        .where_group(
            lambda q: (
                q.where("role", "admin")
                .or_where("role", "moderator")
            )
        )
        .where("age", ">=", 18)
    )
    sql, bindings = query.builder.to_sql()
    assert 'WHERE "status" = $1 AND ("role" = $2 OR "role" = $3) AND "age" >= $4' in sql
    assert bindings == ["active", "admin", "moderator", 18]

    # ModelQueryBuilder.or_where_group
    query_or = (
        SampleUser.query()
        .where("status", "active")
        .or_where_group(
            lambda q: q.where("points", ">", 100).where("role", "vip")
        )
    )
    sql_or, bindings_or = query_or.builder.to_sql()
    assert 'WHERE "status" = $1 OR ("points" > $2 AND "role" = $3)' in sql_or
    assert bindings_or == ["active", 100, "vip"]


@pytest.mark.asyncio
async def test_model_custom_primary_key_and_uuid(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool

    # 1. UUID primary key find_or_fail
    mock_conn.fetch = AsyncMock(
        return_value=[{"uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "title": "UUID Test"}]
    )
    uuid_item = await CustomUuidItem.find_or_fail("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    assert isinstance(uuid_item, CustomUuidItem)
    assert uuid_item.uuid == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    exec_sql = mock_conn.fetch.call_args[0][0]
    assert 'WHERE "uuid" = $1' in exec_sql

    # 2. String code primary key find_or_fail
    mock_conn.fetch = AsyncMock(
        return_value=[{"code": "PROMO-2026", "name": "Discount 2026"}]
    )
    code_item = await CustomCodeItem.find_or_fail("PROMO-2026")
    assert isinstance(code_item, CustomCodeItem)
    assert code_item.code == "PROMO-2026"
    exec_sql2 = mock_conn.fetch.call_args[0][0]
    assert 'WHERE "code" = $1' in exec_sql2


@pytest.mark.asyncio
async def test_model_find_and_find_or_fail_none_guard(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool

    # find(None) returns None immediately without executing a database query
    result = await SampleUser.find(None)
    assert result is None
    assert mock_conn.fetch.call_count == 0

    # find_or_fail(None) raises ValueError
    with pytest.raises(ValueError, match="Primary key lookup value cannot be None"):
        await SampleUser.find_or_fail(None)


@pytest.mark.asyncio
async def test_database_failure_never_maps_to_404() -> None:
    handler = ExceptionHandler(debug=False)

    # 1. asyncpg DatabaseError / PostgresError
    pg_error = asyncpg.PostgresError("Fatal connection error or syntax error")
    resp_pg = await handler.handle(pg_error)
    assert resp_pg.status_code == 500
    assert resp_pg.status_code != 404

    # 2. InterfaceError (connection drop)
    interface_err = asyncpg.InterfaceError("Connection was closed")
    resp_interface = await handler.handle(interface_err)
    assert resp_interface.status_code == 500
    assert resp_interface.status_code != 404

    # 3. Generic DB RuntimeError
    db_runtime_err = RuntimeError("Database pool exhausted")
    resp_rt = await handler.handle(db_runtime_err)
    assert resp_rt.status_code == 500
    assert resp_rt.status_code != 404


@pytest.mark.asyncio
async def test_model_dirty_tracking_synchronized_after_increment_refresh(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    post = SamplePost(id=1, user_id=1, title="Hello", views=10)
    post._exists = True
    post._original = {"id": 1, "user_id": 1, "title": "Hello", "views": 10}

    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.fetch = AsyncMock(
        return_value=[{"id": 1, "user_id": 1, "title": "Hello", "views": 15}]
    )

    assert post.is_dirty() is False
    await post.increment("views", 5)

    assert post.views == 15
    assert post.is_dirty() is False
    assert post.get_dirty() == {}


@pytest.mark.asyncio
async def test_first_or_fail_with_multiple_records(
    mock_db_pool: tuple[AsyncMock, MagicMock],
) -> None:
    mock_conn, _ = mock_db_pool
    mock_conn.fetch = AsyncMock(
        return_value=[
            {"id": 1, "name": "First", "email": "1@e.com", "age": 20, "points": 10, "status": "active", "role": "user"},
            {"id": 2, "name": "Second", "email": "2@e.com", "age": 21, "points": 20, "status": "active", "role": "user"},
        ]
    )
    user = await SampleUser.where("status", "active").first_or_fail()
    assert user.id == 1
    assert user.name == "First"
