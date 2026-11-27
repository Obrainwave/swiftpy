from __future__ import annotations

import uuid
from typing import Any, ClassVar

import pytest

from swiftpy.database.orm.fields import Field
from swiftpy.database.orm.model import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.events.dispatcher import EventDispatcher


def _counter() -> int:
    global _COUNT
    _COUNT += 1
    return _COUNT


_COUNT = 0


class DefaultDemoModel(Model):
    __table__ = "test_defaults_dirty"
    id: int
    name: str
    tags: list[str] = Field(default_factory=list)
    metadata_map: dict[str, Any] = Field(default_factory=dict)
    direct_list: list[int] = []  # Testing mutable class default isolation
    generated_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sequence_num: int = Field(default_factory=_counter)
    __guarded__: ClassVar[list[str]] = []


def test_shared_list_default_is_isolated_between_instances() -> None:
    m1 = DefaultDemoModel(name="One")
    m2 = DefaultDemoModel(name="Two")

    m1.direct_list.append(100)
    m1.tags.append("swift")

    assert m1.direct_list == [100]
    assert m1.tags == ["swift"]

    # Instance 2 is completely isolated and not mutated
    assert m2.direct_list == []
    assert m2.tags == []


def test_callable_default_and_default_factory_evaluated_per_instance() -> None:
    m1 = DefaultDemoModel(name="One")
    m2 = DefaultDemoModel(name="Two")

    assert m1.generated_id != m2.generated_id
    assert m1.sequence_num != m2.sequence_num


def test_in_place_mutation_detected_as_dirty() -> None:
    m = DefaultDemoModel(name="Initial")
    m.id = 1
    m._mark_persisted(m.get_attributes().keys())

    assert not m.is_dirty()
    assert m.get_dirty() == {}

    # Mutate dict in place
    m.metadata_map["role"] = "admin"
    assert m.is_dirty()
    assert m.is_dirty("metadata_map")
    assert not m.is_dirty("name")
    assert "metadata_map" in m.get_dirty()

    # Mutate list in place
    m.tags.append("new_tag")
    assert m.is_dirty("tags")


@pytest.fixture
async def setup_defaults_table(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS test_defaults_dirty CASCADE;
            CREATE TABLE test_defaults_dirty (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                tags JSONB DEFAULT '[]'::jsonb,
                metadata_map JSONB DEFAULT '{}'::jsonb,
                direct_list JSONB DEFAULT '[]'::jsonb,
                generated_id TEXT,
                sequence_num INTEGER
            );
            """
        )


async def test_noop_save_emits_no_event_and_issues_no_query(
    setup_defaults_table: Any,
) -> None:
    disp = EventDispatcher()
    EventDispatcher.set_instance(disp)
    updated_events: list[Any] = []
    disp.listen("default_demo_model.updated", lambda p: updated_events.append(p))

    m = await DefaultDemoModel.create(name="Original")
    assert m.id is not None
    assert len(updated_events) == 0

    # Save immediately without changes (no-op)
    await m.save()
    assert len(updated_events) == 0
    assert not m.is_dirty()

    # Now make actual change
    m.name = "Changed"
    assert m.is_dirty("name")
    await m.save()
    assert len(updated_events) == 1
    assert not m.is_dirty()


async def test_in_place_mutation_persisted_to_database(
    setup_defaults_table: Any,
) -> None:
    m = await DefaultDemoModel.create(name="JsonHolder")
    m.metadata_map["key"] = "saved_val"
    m.tags.append("persisted_tag")

    assert m.is_dirty()
    await m.save()
    assert not m.is_dirty()

    # Refresh from database
    refreshed = await DefaultDemoModel.find(m.id)
    assert refreshed is not None
    assert refreshed.metadata_map.get("key") == "saved_val"
    assert "persisted_tag" in refreshed.tags
