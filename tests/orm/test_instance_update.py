from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from swiftpy.database.orm.fields import Field
from swiftpy.database.orm.model import MassAssignmentError, Model, PrimaryKeyError
from swiftpy.database.pool import DatabasePool
from swiftpy.events.dispatcher import EventDispatcher


class FillableUpdateUser(Model):
    __table__ = "test_instance_update_users"
    id: int
    name: str
    email: str = "default@example.com"
    role: str = "member"
    updated_at: datetime = Field(default=None)
    __fillable__: ClassVar[list[str]] = ["name", "email"]


class GuardedUpdateUser(Model):
    __table__ = "test_instance_update_users"
    id: int
    name: str
    email: str = "default@example.com"
    role: str = "member"
    updated_at: datetime = Field(default=None)
    __guarded__: ClassVar[list[str]] = ["role"]


class DefaultDenyUpdateUser(Model):
    __table__ = "test_instance_update_users"
    id: int
    name: str
    email: str = "default@example.com"


@pytest.fixture
async def setup_instance_update_table(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS test_instance_update_users CASCADE;
            CREATE TABLE test_instance_update_users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT 'default@example.com',
                role TEXT NOT NULL DEFAULT 'member',
                updated_at TIMESTAMPTZ
            );
            """
        )


async def test_model_update_with_dict_and_kwargs(
    setup_instance_update_table: Any,
) -> None:
    user = await FillableUpdateUser.create(name="Alice", email="alice@example.com")
    assert user.id is not None

    returned = await user.update({"name": "Alice Cooper"})
    assert returned is user
    assert user.name == "Alice Cooper"
    assert not user.is_dirty()

    fetched = await FillableUpdateUser.find(user.id)
    assert fetched is not None
    assert fetched.name == "Alice Cooper"

    await user.update(email="cooper@example.com")
    refetched = await FillableUpdateUser.find(user.id)
    assert refetched is not None
    assert refetched.email == "cooper@example.com"

    await user.update({"name": "Alice Combined"}, email="combined@example.com")
    final_row = await FillableUpdateUser.find(user.id)
    assert final_row is not None
    assert final_row.name == "Alice Combined"
    assert final_row.email == "combined@example.com"


async def test_model_update_respects_fillable_and_guarded(
    setup_instance_update_table: Any,
) -> None:
    fillable_user = await FillableUpdateUser.create(
        name="Member",
        email="member@example.com",
    )
    # Unlisted key 'role' is ignored when __fillable__ is declared
    await fillable_user.update({"name": "Member Two", "role": "admin"})
    refetched = await FillableUpdateUser.find(fillable_user.id)
    assert refetched is not None
    assert refetched.name == "Member Two"
    assert refetched.role == "member"

    guarded_user = await GuardedUpdateUser.create(name="Guarded", email="g@example.com")
    with pytest.raises(MassAssignmentError, match="guarded"):
        await guarded_user.update({"role": "superadmin"})
    with pytest.raises(MassAssignmentError, match="private"):
        await guarded_user.update({"_exists": False})
    with pytest.raises(MassAssignmentError, match="Unknown attribute"):
        await guarded_user.update({"nonexistent_col": "x"})

    deny_user = await DefaultDenyUpdateUser.force_create(
        name="Locked",
        email="locked@example.com",
    )
    with pytest.raises(MassAssignmentError, match="denied by default"):
        await deny_user.update({"name": "Unlocked"})


async def test_model_force_update_bypasses_mass_assignment_and_saves(
    setup_instance_update_table: Any,
) -> None:
    user = await FillableUpdateUser.create(name="Standard", email="std@example.com")

    returned = await user.force_update({"name": "Elevated", "role": "admin"})
    assert returned is user
    refetched = await FillableUpdateUser.find(user.id)
    assert refetched is not None
    assert refetched.name == "Elevated"
    assert refetched.role == "admin"

    # force_update still rejects private and unknown attributes
    with pytest.raises(MassAssignmentError, match="private"):
        await user.force_update({"_exists": False})
    with pytest.raises(MassAssignmentError, match="Unknown attribute"):
        await user.force_update(unknown_col="nope")


async def test_model_update_skips_noop_and_emits_lifecycle_events_and_timestamps(
    setup_instance_update_table: Any,
) -> None:
    disp = EventDispatcher()
    EventDispatcher.set_instance(disp)

    events: list[str] = []
    disp.listen(
        "fillable_update_user.updating",
        lambda u: events.append(f"updating:{u.name}"),
    )
    disp.listen(
        "fillable_update_user.updated",
        lambda u: events.append(f"updated:{u.name}"),
    )

    user = await FillableUpdateUser.create(name="Same", email="same@example.com")
    assert user.updated_at is None

    # No-op update: identical value must not touch DB or emit update events
    await user.update({"name": "Same"})
    assert events == []
    assert user.updated_at is None
    assert not user.is_dirty()

    # Empty update is also a no-op
    await user.update()
    assert events == []

    # Actual change emits updating/updated and updates updated_at
    before = datetime.now(UTC)
    await user.update({"name": "UpdatedName"})
    assert events == ["updating:UpdatedName", "updated:UpdatedName"]
    assert user.updated_at is not None
    assert user.updated_at >= before
    assert not user.is_dirty()


async def test_model_update_error_cases_and_query_builder_compatibility(
    setup_instance_update_table: Any,
) -> None:
    unsaved = FillableUpdateUser(name="Transient")
    with pytest.raises(PrimaryKeyError, match="unsaved"):
        await unsaved.update({"name": "ShouldFail"})

    persisted = await FillableUpdateUser.create(name="WillDelete")
    await persisted.update({"name": "BeforeDelete"})
    await persisted.delete()
    with pytest.raises(PrimaryKeyError, match="unsaved"):
        await persisted.update({"name": "AfterDelete"})

    valid = await FillableUpdateUser.create(name="ArgCheck")
    update_fn: Callable[..., Awaitable[FillableUpdateUser]] = valid.update
    with pytest.raises(TypeError, match="mapping"):
        await update_fn("not-a-dict")
    with pytest.raises(TypeError, match="mapping"):
        await update_fn(None)
    with pytest.raises(TypeError):
        await update_fn({"name": "A"}, {"email": "B"})
    with pytest.raises(TypeError, match="must be str"):
        await update_fn({1: "invalid"})

    # Query-builder bulk .update(dict) continues working alongside instance .update()
    await FillableUpdateUser.where("id", valid.id).update({"name": "BulkUpdated"})
    row = await FillableUpdateUser.find(valid.id)
    assert row is not None
    assert row.name == "BulkUpdated"
