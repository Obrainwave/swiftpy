from __future__ import annotations

from typing import Any, ClassVar

import pytest

from swiftpy.database.orm.model import MassAssignmentError, Model
from swiftpy.database.pool import DatabasePool


class DefaultDenyModel(Model):
    __table__ = "test_default_deny"
    id: int
    title: str
    status: str = "draft"


class GuardedOptOutModel(Model):
    __table__ = "test_guarded_opt_out"
    id: int
    name: str
    email: str
    __guarded__: ClassVar[list[str]] = []


class UserWithFillable(Model):
    __table__ = "test_user_fillable"
    id: int
    name: str
    email: str
    is_admin: bool = False
    __fillable__: ClassVar[list[str]] = ["name", "email"]


class UserWithGuarded(Model):
    __table__ = "test_user_guarded"
    id: int
    name: str
    email: str
    is_admin: bool = False
    __guarded__: ClassVar[list[str]] = ["is_admin"]


def test_private_attribute_mass_assignment_rejected() -> None:
    with pytest.raises(MassAssignmentError, match="Cannot mass-assign private attribute '_exists'"):
        GuardedOptOutModel(_exists=True)

    with pytest.raises(MassAssignmentError, match="Cannot mass-assign private attribute '_original'"):
        GuardedOptOutModel(_original={})


def test_unknown_attribute_mass_assignment_rejected() -> None:
    with pytest.raises(MassAssignmentError, match="Unknown attribute 'hacker_field'"):
        GuardedOptOutModel(hacker_field="evil")


def test_default_deny_all_when_neither_fillable_nor_guarded_set() -> None:
    # Model has no __fillable__ or __guarded__ declared
    with pytest.raises(MassAssignmentError, match="mass assignment is denied by default"):
        DefaultDenyModel(title="Hello")

    # Empty constructor without mass-assignment kwargs is permitted
    instance = DefaultDenyModel()
    assert instance.title is None
    assert instance.status == "draft"


def test_guarded_opt_out_allows_all_declared_fields() -> None:
    m = GuardedOptOutModel(name="Alice", email="alice@example.com")
    assert m.name == "Alice"
    assert m.email == "alice@example.com"


def test_fillable_allows_subset_and_ignores_unlisted() -> None:
    user = UserWithFillable(name="Bob", email="bob@example.com")
    assert user.name == "Bob"
    assert user.email == "bob@example.com"
    assert user.is_admin is False

    # is_admin is not in __fillable__, so it is ignored
    user2 = UserWithFillable(name="Bob", email="bob@example.com", is_admin=True)
    assert user2.name == "Bob"
    assert user2.is_admin is False


def test_guarded_rejects_guarded_field() -> None:
    user = UserWithGuarded(name="Charlie", email="charlie@example.com")
    assert user.name == "Charlie"
    assert user.is_admin is False

    with pytest.raises(MassAssignmentError, match="is guarded"):
        UserWithGuarded(name="Charlie", is_admin=True)


def test_fill_method_enforces_mass_assignment() -> None:
    user = UserWithFillable()
    user.fill(name="David", email="david@example.com", is_admin=True)
    assert user.name == "David"
    assert user.is_admin is False


def test_force_fill_bypasses_fillable_and_guarded() -> None:
    user = UserWithFillable()
    user.force_fill(name="Root", is_admin=True)
    assert user.name == "Root"
    assert user.is_admin is True

    # force_fill still rejects private and unknown attributes
    with pytest.raises(MassAssignmentError, match="Cannot mass-assign private attribute '_exists'"):
        user.force_fill(_exists=True)

    with pytest.raises(MassAssignmentError, match="Unknown attribute 'foo_bar'"):
        user.force_fill(foo_bar=123)


@pytest.fixture
async def setup_db_tables(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS test_user_fillable, test_user_guarded CASCADE;
            CREATE TABLE test_user_fillable (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                is_admin BOOLEAN DEFAULT FALSE
            );
            CREATE TABLE test_user_guarded (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                is_admin BOOLEAN DEFAULT FALSE
            );
            """
        )


async def test_create_and_force_create_db(setup_db_tables: Any) -> None:
    # create() validates fillable: is_admin is not fillable so it stays False
    user = await UserWithFillable.create(name="Eve", email="eve@example.com", is_admin=True)
    assert user.id is not None
    assert user.name == "Eve"
    assert user.is_admin is False

    # create() with guarded field on UserWithGuarded raises MassAssignmentError
    with pytest.raises(MassAssignmentError, match="is guarded"):
        await UserWithGuarded.create(name="Eve", email="eve@example.com", is_admin=True)

    # force_create() allows setting is_admin
    admin = await UserWithFillable.force_create(name="Admin", email="admin@example.com", is_admin=True)
    assert admin.id is not None
    assert admin.is_admin is True
