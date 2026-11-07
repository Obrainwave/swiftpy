from __future__ import annotations

import uuid
from typing import Any

import pytest

from swiftpy.database.orm.model import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query
from swiftpy.database.transaction import db


class UuidDocument(Model):
    __table__ = "test_uuid_documents"
    __primary_key__ = "uuid"
    __fillable__ = ("uuid", "title")

    uuid: uuid.UUID
    title: str


class CountryCode(Model):
    __table__ = "test_country_codes"
    __primary_key__ = "code"
    __fillable__ = ("code", "name")

    code: str
    name: str


@pytest.fixture(autouse=True)
async def setup_pk_tables(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS "test_uuid_documents" CASCADE;
            CREATE TABLE "test_uuid_documents" (
                uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                title TEXT NOT NULL
            );
            DROP TABLE IF EXISTS "test_country_codes" CASCADE;
            CREATE TABLE "test_country_codes" (
                code VARCHAR(16) PRIMARY KEY,
                name TEXT NOT NULL
            );
            """
        )


@pytest.mark.asyncio
async def test_uuid_primary_key_create_save_refresh_delete_round_trip() -> None:
    # 1. Database-generated UUID via create()
    doc = await UuidDocument.create(title="Architecture Spec")
    assert isinstance(doc.uuid, uuid.UUID)
    assert doc.title == "Architecture Spec"

    # Update via save()
    doc.title = "Updated Architecture Spec"
    await doc.save()

    fetched = await UuidDocument.find(doc.uuid)
    assert fetched is not None
    assert fetched.uuid == doc.uuid
    assert fetched.title == "Updated Architecture Spec"

    # Refresh and delete
    await doc.refresh()
    assert doc.title == "Updated Architecture Spec"
    await doc.delete()
    assert await UuidDocument.find(doc.uuid) is None

    # 2. Client-supplied UUID via save() on a new model
    custom_uuid = uuid.uuid4()
    doc2 = UuidDocument(uuid=custom_uuid, title="Client UUID Doc")
    await doc2.save()
    assert doc2.uuid == custom_uuid
    fetched2 = await UuidDocument.find(custom_uuid)
    assert fetched2 is not None
    assert fetched2.title == "Client UUID Doc"


@pytest.mark.asyncio
async def test_custom_string_pk_code_round_trip_and_insert_returning() -> None:
    # 1. QueryBuilder.insert(..., returning=["code"]) returns native string pk
    returned_code = await Query.table("test_country_codes").insert(
        {"code": "NG", "name": "Nigeria"},
        returning=["code"],
    )
    assert returned_code == "NG"

    # 2. Model.create -> save -> refresh -> delete with custom string pk 'code'
    c = await CountryCode.create(code="CA", name="Canada")
    assert c.code == "CA"
    assert c.name == "Canada"

    c.name = "Canada (Updated)"
    await c.save()

    found = await CountryCode.find("CA")
    assert found is not None
    assert found.name == "Canada (Updated)"

    await c.refresh()
    assert c.name == "Canada (Updated)"

    await c.delete()
    assert await CountryCode.find("CA") is None


@pytest.mark.asyncio
async def test_create_executes_exactly_one_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_count = 0

    async with db.transaction() as tx_conn:

        class _CountingConnProxy:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> Any:
                nonlocal query_count
                query_count += 1
                return await self._inner.fetchrow(query, *args, **kwargs)

            async def fetch(self, query: str, *args: Any, **kwargs: Any) -> Any:
                nonlocal query_count
                query_count += 1
                return await self._inner.fetch(query, *args, **kwargs)

            async def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
                nonlocal query_count
                query_count += 1
                return await self._inner.execute(query, *args, **kwargs)

        monkeypatch.setattr(
            "swiftpy.database.query_builder.get_active_connection",
            lambda: _CountingConnProxy(tx_conn),
        )
        doc = await UuidDocument.create(title="Single Query Create")
        assert isinstance(doc.uuid, uuid.UUID)
        assert doc.title == "Single Query Create"
        assert query_count == 1


@pytest.mark.asyncio
async def test_save_raises_when_pk_unset_on_existing_model() -> None:
    doc = await UuidDocument.create(title="Has PK")
    doc.uuid = None  # type: ignore[assignment]
    with pytest.raises((ValueError, RuntimeError), match=r"(?i)primary key"):
        await doc.save()


def test_composite_primary_key_explicitly_unsupported() -> None:
    with pytest.raises(ValueError, match=r"(?i)composite|primary key"):

        class CompositeTupleModel(Model):
            __table__ = "composite_items"
            __primary_key__ = ("org_id", "user_id")  # type: ignore[assignment]

    with pytest.raises(ValueError, match=r"(?i)composite|primary key"):

        class CompositeCommaModel(Model):
            __table__ = "composite_items"
            __primary_key__ = "org_id, user_id"
