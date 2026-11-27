from __future__ import annotations

import asyncio
import importlib
import os
import sys
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from swiftpy.database.factories import (
    Factory,
    FactoryBatch,
    Fake,
    Sequence,
    clear_factory_registry,
    restore_factory_registry,
    snapshot_factory_registry,
)
from swiftpy.database.migrations import Schema
from swiftpy.database.orm import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
    Model,
)
from swiftpy.database.orm import model as orm_model_mod
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query
from swiftpy.database.seeder import Seeder
from swiftpy.database.transaction import db
from swiftpy.schemas import BaseSchema

_WORKER_TAG = os.getenv("PYTEST_XDIST_WORKER", "")
_TBL_SUFFIX = f"_{_WORKER_TAG}" if _WORKER_TAG else ""
_TBL_USERS = f"test_fac_users{_TBL_SUFFIX}"
_TBL_POSTS = f"test_fac_posts{_TBL_SUFFIX}"
_TBL_PROFILES = f"test_fac_profiles{_TBL_SUFFIX}"
_TBL_ROLES = f"test_fac_roles{_TBL_SUFFIX}"
_TBL_ROLE_USER = f"test_fac_role_user{_TBL_SUFFIX}"

# ---------------------------------------------------------------------------
# Test Models & Factories
# ---------------------------------------------------------------------------


class FacRole(Model):
    __table__ = _TBL_ROLES
    __fillable__ = ["name"]

    id: int
    name: str


class FacProfile(Model):
    __table__ = _TBL_PROFILES
    __fillable__ = ["user_id", "bio"]

    id: int
    user_id: int
    bio: str
    user = BelongsTo("FacUser", foreign_key="user_id")


class FacPost(Model):
    __table__ = _TBL_POSTS
    __fillable__ = ["user_id", "title", "body", "published"]

    id: int
    user_id: int
    title: str
    body: str
    published: bool = True

    user = BelongsTo("FacUser", foreign_key="user_id")


class FacUser(Model):
    __table__ = _TBL_USERS
    __fillable__ = ["name", "email", "password"]
    __guarded__ = ["is_admin"]

    id: int
    name: str
    email: str
    password: str
    is_admin: bool = False

    posts = HasMany(FacPost, foreign_key="user_id")
    profile = HasOne(FacProfile, foreign_key="user_id")
    roles = BelongsToMany(
        FacRole,
        pivot_table=_TBL_ROLE_USER,
        foreign_pivot_key="user_id",
        related_pivot_key="role_id",
    )


class FacUserFactory(Factory[FacUser]):
    model = FacUser

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.fake.name(),
            "email": self.fake.unique_email(),
            "password": "hashed-password",
            "is_admin": False,
        }

    def admin(self) -> FacUserFactory:
        return self.state({"is_admin": True})


class FacPostFactory(Factory[FacPost]):
    model = FacPost

    def definition(self) -> dict[str, Any]:
        return {
            "user_id": FacUserFactory,
            "title": self.fake.sentence(4),
            "body": self.fake.paragraph(2),
            "published": True,
        }


class FacProfileFactory(Factory[FacProfile]):
    model = FacProfile

    def definition(self) -> dict[str, Any]:
        return {
            "bio": self.fake.sentence(5),
        }


class FacRoleFactory(Factory[FacRole]):
    model = FacRole

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.fake.slug(2),
        }


# ---------------------------------------------------------------------------
# Unit Tests (No Database Required)
# ---------------------------------------------------------------------------


def test_fake_generator_determinism_and_methods() -> None:
    """Fake produces deterministic output when seeded and supports core providers."""
    f1 = Fake(seed=12345)
    f2 = Fake(seed=12345)

    assert f1.name() == f2.name()
    assert f1.sentence() == f2.sentence()
    assert f1.uuid4() == f2.uuid4()
    assert f1.binary(16) == f2.binary(16)

    emails = {f1.unique_email() for _ in range(25)}
    assert len(emails) == 25
    assert all("@example.com" in em for em in emails)
    assert f1.safe_email().endswith("@example.org")
    assert f1.url().startswith("https://")
    assert len(f1.words(4)) == 4
    assert len(f1.paragraphs(2)) == 2
    assert len(f1.text(80)) <= 80
    assert 10 <= f1.random_int(10, 20) <= 20
    assert 0.0 <= f1.random_float(0.0, 5.0) <= 5.0
    assert isinstance(f1.boolean(), bool)
    assert f1.random_element(["a", "b", "c"]) in {"a", "b", "c"}
    assert len(f1.random_elements(["a", "b", "c"], length=2, unique=True)) == 2
    assert isinstance(f1.uuid_object(), uuid.UUID)
    assert len(f1.sha256()) == 64
    assert len(f1.token_hex(8)) == 16
    assert f1.phone_number().startswith("+1-")
    assert isinstance(f1.company(), str)
    assert isinstance(f1.city(), str)
    assert isinstance(f1.country(), str)
    assert len(f1.date()) == 10
    assert "T" in f1.iso8601()


def test_factory_make_raw_states_sequences_and_immutability() -> None:
    """Verify in-memory make/raw, states, sequences, hooks, and builder immutability."""
    base_factory = FacUser.factory()
    assert isinstance(base_factory, FacUserFactory)

    # Builder immutability: derived chains never mutate base_factory
    admin_factory = base_factory.state({"is_admin": True})
    regular_user = base_factory.make()
    admin_user = admin_factory.make()
    assert regular_user.is_admin is False
    assert admin_user.is_admin is True

    # Schema state + keyword override + callable state + after_making hook
    class UserStateSchema(BaseSchema):
        name: str

    def email_from_name(attrs: dict[str, Any]) -> dict[str, Any]:
        return {"email": f"{attrs['name'].lower()}@example.com"}

    def seq_email(idx: int) -> dict[str, Any]:
        return {"email": f"seq{idx}@example.com"}

    hooked: list[str] = []
    built = (
        FacUser.factory()
        .state(UserStateSchema(name="Ada"))
        .state(email_from_name)
        .after_making(lambda u: hooked.append(u.email))
        .make(is_admin=True)
    )
    assert built.name == "Ada"
    assert built.email == "ada@example.com"
    assert built.is_admin is True
    assert hooked == ["ada@example.com"]

    # Sequence cycling & index callbacks on FactoryBatch
    batch = (
        FacUser.factory()
        .count(4)
        .sequence({"name": "First"}, {"name": "Second"})
        .state(Sequence(seq_email))
    )
    assert isinstance(batch, FactoryBatch)
    made_users = batch.make()
    assert [u.name for u in made_users] == ["First", "Second", "First", "Second"]
    assert [u.email for u in made_users] == [
        "seq0@example.com",
        "seq1@example.com",
        "seq2@example.com",
        "seq3@example.com",
    ]

    # raw() returns dictionaries without instantiating models
    raw_single = FacUser.factory().seed(99).raw(name="Raw User")
    assert isinstance(raw_single, dict)
    assert raw_single["name"] == "Raw User"
    raw_batch = FacUser.factory().count(2).raw()
    assert len(raw_batch) == 2
    assert all(isinstance(item, dict) for item in raw_batch)

    # Custom subclass method chaining before and after .count()
    admins = FacUserFactory().admin().count(2).make()
    assert all(a.is_admin is True for a in admins)
    admins_after = FacUserFactory().count(2).admin().make()
    assert all(a.is_admin is True for a in admins_after)


def test_factory_make_with_relationships_and_validation() -> None:
    """Verify in-memory relationship population via has_posts, has_profile, and for_."""
    user = (
        FacUser.factory()
        .has_posts(2, {"published": False})
        .has_profile(1, bio="In-memory bio")
        .make(id=77)
    )
    assert len(user.posts) == 2
    assert all(p.user_id == 77 and p.published is False for p in user.posts)
    assert user.profile.bio == "In-memory bio"
    assert user.profile.user_id == 77

    # for_() attaches parent relation and sets foreign key in memory
    owner = FacUser(id=55, name="Owner", email="owner@example.com", password="pw")
    posts = FacPost.factory().count(2).for_(owner).make()
    assert len(posts) == 2
    assert all(p.user_id == 55 for p in posts)
    assert all(p._loaded_relations["user"] is owner for p in posts)

    # Validation guards
    with pytest.raises(ValueError, match="count must be >= 1"):
        FacUser.factory().count(0)
    with pytest.raises(TypeError, match="must be an integer"):
        FacUser.factory().count(True)
    with pytest.raises(ValueError, match="HasOne relationship"):
        FacUser.factory().has_profile(2).make()
    with pytest.raises(AttributeError, match="has_unknown"):
        _ = FacUser.factory().has_unknown(1)


def test_factory_convention_discovery_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Model.factory() discovers database/factories/<model>_factory.py automatically."""
    registry_snapshot = snapshot_factory_registry()
    clear_factory_registry()
    try:
        _run_factory_convention_discovery_checks(tmp_path, monkeypatch)
    finally:
        restore_factory_registry(registry_snapshot)


def _run_factory_convention_discovery_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    class EphemeralItem(Model):
        __table__ = "ephemeral_items"
        __fillable__ = ["label"]
        id: int
        label: str

    # 1. Fallback factory works even with no factory file
    fallback = EphemeralItem.factory().make(label="fallback-ok")
    assert fallback.label == "fallback-ok"

    # 2. Convention discovery from database/factories/<model>_factory.py
    pkg_dir = tmp_path / "app" / "models"
    pkg_dir.mkdir(parents=True)
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "widget_item.py").write_text(
        """from __future__ import annotations
from swiftpy.database.orm import Model

class WidgetItem(Model):
    __table__ = "widget_items"
    __fillable__ = ["label"]
    id: int
    label: str
""",
        encoding="utf-8",
    )

    factories_dir = tmp_path / "database" / "factories"
    factories_dir.mkdir(parents=True)
    (factories_dir / "widget_item_factory.py").write_text(
        """from __future__ import annotations
from typing import Any
from swiftpy.database.factories import Factory
from app.models.widget_item import WidgetItem

class WidgetItemFactory(Factory[WidgetItem]):
    model = WidgetItem

    def definition(self) -> dict[str, Any]:
        return {"label": "from-convention-discovery"}
""",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    saved_mods = {
        k: v
        for k, v in list(sys.modules.items())
        if k in {"app", "database"} or k.startswith(("app.", "database.", "_swiftpy_factories"))
    }
    for k in saved_mods:
        sys.modules.pop(k, None)

    try:
        widget_mod = importlib.import_module("app.models.widget_item")
        widget_cls = widget_mod.WidgetItem
        discovered = widget_cls.factory().make()
        assert discovered.label == "from-convention-discovery"
    finally:
        for k in list(sys.modules):
            if k in {"app", "database"} or k.startswith(
                ("app.", "database.", "_swiftpy_factories")
            ):
                sys.modules.pop(k, None)
        sys.modules.update(saved_mods)

    # 3. Explicit __factory__ attribute on model takes precedence
    class ExplicitCustomFactory(Factory[EphemeralItem]):
        model = EphemeralItem

        def definition(self) -> dict[str, Any]:
            return {"label": "from-explicit"}

    EphemeralItem.__factory__ = ExplicitCustomFactory
    built = EphemeralItem.factory().make()
    assert built.label == "from-explicit"


# ---------------------------------------------------------------------------
# PostgreSQL Integration Tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def factory_tables(
    ensure_db_pool: DatabasePool | None,
) -> AsyncGenerator[DatabasePool, None]:
    if ensure_db_pool is None:
        pytest.skip("PostgreSQL service is not available in local test environment")

    for tbl in (
        _TBL_ROLE_USER,
        _TBL_PROFILES,
        _TBL_POSTS,
        _TBL_ROLES,
        _TBL_USERS,
    ):
        await Schema.drop_if_exists(tbl)

    try:
        await Schema.create(
            _TBL_USERS,
            lambda t: (
                t.id(),
                t.string("name"),
                t.string("email").unique(),
                t.string("password"),
                t.boolean("is_admin").default(False),
                t.timestamps(),
            ),
        )
        await Schema.create(
            _TBL_POSTS,
            lambda t: (
                t.id(),
                t.big_integer("user_id"),
                t.string("title"),
                t.text("body"),
                t.boolean("published").default(True),
                t.foreign_key("user_id", references="id", on=_TBL_USERS, on_delete="CASCADE"),
            ),
        )
        await Schema.create(
            _TBL_PROFILES,
            lambda t: (
                t.id(),
                t.big_integer("user_id").unique(),
                t.string("bio"),
                t.foreign_key("user_id", references="id", on=_TBL_USERS, on_delete="CASCADE"),
            ),
        )
        await Schema.create(
            _TBL_ROLES,
            lambda t: (
                t.id(),
                t.string("name").unique(),
            ),
        )
        await Schema.create(
            _TBL_ROLE_USER,
            lambda t: (
                t.big_integer("user_id"),
                t.big_integer("role_id"),
                t.boolean("active").default(True),
                t.primary(["user_id", "role_id"]),
            ),
        )

        yield ensure_db_pool
    finally:
        for tbl in (
            _TBL_ROLE_USER,
            _TBL_PROFILES,
            _TBL_POSTS,
            _TBL_ROLES,
            _TBL_USERS,
        ):
            await Schema.drop_if_exists(tbl)


@pytest.mark.asyncio
async def test_pg_factory_create_count_has_posts_and_relationships(
    factory_tables: DatabasePool,
) -> None:
    """Verify User.factory().count(50).has_posts(1).create() and full relationship factories."""
    assert factory_tables is not None

    # 1. Exact insight workflow: 50 users each with 1 related post
    users = await FacUser.factory().count(50).has_posts(1).create()
    assert len(users) == 50
    assert all(u.id > 0 for u in users)
    assert all(len(u.posts) == 1 and u.posts[0].user_id == u.id for u in users)

    assert await FacUser.count() == 50
    assert await FacPost.count() == 50

    # 2. HasOne + multiple HasMany + state + async after_creating hook
    created_ids: list[int] = []

    async def record_created(user: FacUser) -> None:
        created_ids.append(user.id)

    def title_from_author(_attrs: dict[str, Any], parent: FacUser) -> dict[str, Any]:
        return {"title": f"Post by {parent.name}"}

    author = await (
        FacUser.factory()
        .state({"is_admin": True})
        .has_profile(1, bio="Core Maintainer")
        .has_posts(2, title_from_author)
        .after_creating(record_created)
        .create(name="Grace Hopper")
    )
    assert author.is_admin is True
    assert author.profile.user_id == author.id
    assert author.profile.bio == "Core Maintainer"
    assert len(author.posts) == 2
    assert all(p.title == "Post by Grace Hopper" for p in author.posts)
    assert created_ids == [author.id]

    # 3. BelongsTo via .for_(existing_user) and .for_(Factory)
    extra_posts = await FacPost.factory().count(3).for_(author).create()
    assert len(extra_posts) == 3
    assert all(p.user_id == author.id for p in extra_posts)

    # for_(FacUser.factory()) creates ONE shared parent user for the batch
    before_users = await FacUser.count()
    shared_posts = await FacPost.factory().count(2).for_(FacUser.factory()).create()
    assert await FacUser.count() == before_users + 1
    assert shared_posts[0].user_id == shared_posts[1].user_id

    # Standalone Post creation resolves nested "user_id": FacUserFactory automatically
    standalone_post = await FacPost.factory().create(title="Standalone")
    assert standalone_post.title == "Standalone"
    assert standalone_post.user_id > 0

    # 4. BelongsToMany via .has_roles() and .has_attached() with pivot columns
    editor_role = await FacRole.factory().create(name="editor")
    member_user = await (
        FacUser.factory()
        .has_roles(2)
        .has_attached(editor_role, pivot_attributes={"active": False}, relationship="roles")
        .create()
    )
    assert len(member_user.roles) == 3
    pivot_rows = (
        await Query.table(_TBL_ROLE_USER)
        .where("user_id", member_user.id)
        .get()
    )
    assert len(pivot_rows) == 3
    editor_pivot = next(r for r in pivot_rows if r["role_id"] == editor_role.id)
    assert editor_pivot["active"] is False

    # 5. Seeder integration inside db.transaction()
    class DemoSeeder(Seeder):
        async def run(self) -> None:
            await FacUser.factory().count(3).has_posts(2).create()

    before_seed_users = await FacUser.count()
    before_seed_posts = await FacPost.count()
    async with db.transaction():
        await DemoSeeder().run()
    assert await FacUser.count() == before_seed_users + 3
    assert await FacPost.count() == before_seed_posts + 6


# ---------------------------------------------------------------------------
# Additional Unit Edge Cases (Section 2A, 2B, 2C Hooks, 2D Discovery)
# ---------------------------------------------------------------------------


def test_factory_lifecycle_counts_seed_raw_precedence_sequences_and_immutability() -> None:
    """Cover count validation, seed reproducibility, raw/hook skip, state precedence, sequence cycling, count(1), and immutability."""
    # 1. count() validation: reject negative, 0, bool, float, str, None
    for invalid_int in (0, -1, -100):
        with pytest.raises(ValueError, match="count must be >= 1"):
            FacUser.factory().count(invalid_int)
    bad_counts: list[Any] = [True, False, 1.5, "2", None]
    for bad_val in bad_counts:
        with pytest.raises(TypeError, match="must be an integer"):
            FacUser.factory().count(bad_val)

    # 2. count(1) returns single-element list across make() and raw()
    single_batch_make = FacUser.factory().count(1).make(name="Solo")
    assert isinstance(single_batch_make, list)
    assert len(single_batch_make) == 1
    assert single_batch_make[0].name == "Solo"

    single_batch_raw = FacUser.factory().count(1).raw(name="SoloRaw")
    assert isinstance(single_batch_raw, list)
    assert len(single_batch_raw) == 1
    assert single_batch_raw[0]["name"] == "SoloRaw"

    # 3. seed() produces repeatable results across runs
    run_a = FacUser.factory().seed(2026).count(3).raw()
    run_b = FacUser.factory().seed(2026).count(3).raw()
    assert run_a == run_b

    # 4. raw() neither instantiates models nor triggers lifecycle hooks
    fired_hooks: list[str] = []
    raw_dict = (
        FacUser.factory()
        .after_making(lambda _u: fired_hooks.append("making"))
        .after_creating(lambda _u: fired_hooks.append("creating"))
        .raw(name="NoHooks")
    )
    assert isinstance(raw_dict, dict)
    assert not isinstance(raw_dict, Model)
    assert fired_hooks == []

    # 5. State override precedence + keyword override precedence
    s1 = FacUser.factory().state({"name": "First", "is_admin": False})
    s2 = s1.state({"name": "Second", "is_admin": True})
    s3 = s2.state({"name": "Third"})
    assert s1.make().name == "First"
    assert s2.make().is_admin is True
    assert s3.make().name == "Third"
    assert s3.make(name="KeywordWins", is_admin=False).name == "KeywordWins"
    assert s3.make(name="KeywordWins", is_admin=False).is_admin is False

    # 6. Multiple independent sequences + odd batch cycling
    seq_users = (
        FacUser.factory()
        .count(5)
        .sequence({"name": "Alpha"}, {"name": "Beta"})
        .sequence({"is_admin": False}, {"is_admin": True}, {"is_admin": False})
        .make()
    )
    assert [u.name for u in seq_users] == ["Alpha", "Beta", "Alpha", "Beta", "Alpha"]
    assert [u.is_admin for u in seq_users] == [False, True, False, False, True]

    # 7. Builder immutability across states, sequences, relations, hooks, and custom subclass methods
    base = FacUserFactory().seed(1)
    with_posts = base.has_posts(2)
    with_admin = base.admin()
    with_hook = base.after_making(lambda _u: fired_hooks.append("isolated"))
    counted_then_admin = base.count(2).admin()
    admin_then_counted = with_admin.count(2)

    base_user = base.make()
    assert base_user.is_admin is False
    assert "posts" not in base_user._loaded_relations
    assert len(with_posts.make(id=10).posts) == 2
    assert with_admin.make().is_admin is True
    assert fired_hooks == []
    _ = with_hook.make()
    assert fired_hooks == ["isolated"]
    assert all(u.is_admin is True for u in counted_then_admin.make())
    assert all(u.is_admin is True for u in admin_then_counted.make())


def test_factory_relationship_validation_and_recursion_guard() -> None:
    """Verify relationship validation failures and infinite recursion protection."""
    # Invalid relation name on has_<rel> / for_<rel>
    with pytest.raises(AttributeError, match="has_nonexistent"):
        _ = FacUser.factory().has_nonexistent(1)
    with pytest.raises(AttributeError, match="for_nonexistent"):
        _ = FacPost.factory().for_nonexistent(FacUserFactory)

    # Unsupported relation type (BelongsTo with has_<rel> or .has(..., relationship=...), HasMany with for_)
    with pytest.raises(TypeError, match="Must|must be HasMany, HasOne, or BelongsToMany"):
        _ = FacPost.factory().has_user(1)
    with pytest.raises(TypeError, match="must be HasMany, HasOne, or BelongsToMany"):
        FacPost.factory().has(FacUserFactory, relationship="user")
    with pytest.raises(TypeError, match="must be BelongsTo"):
        FacUser.factory().for_(FacPostFactory, relationship="posts")

    # Invalid counts on has_<rel>
    with pytest.raises(ValueError, match="count must be >= 1"):
        _ = FacUser.factory().has_posts(0)
    with pytest.raises(ValueError, match="count must be >= 1"):
        _ = FacUser.factory().has_posts(-3)
    bad_cnt: Any = "2"
    with pytest.raises(TypeError, match="must be an integer"):
        _ = FacUser.factory().has_posts(bad_cnt)
    with pytest.raises(ValueError, match="HasOne relationship"):
        _ = FacUser.factory().has_profile(2)

    # for_() with invalid parent object or wrong-model factory
    bad_parent: Any = "invalid_parent"
    with pytest.raises(TypeError, match="for_\\(\\) expects"):
        FacPost.factory().for_(bad_parent)
    with pytest.raises(ValueError, match="No BelongsTo relationship targeting 'FacRole'"):
        FacPost.factory().for_(FacRoleFactory).make()
    with pytest.raises(TypeError, match="targets 'FacUser', got 'FacRole'"):
        FacPost.factory().for_(FacRoleFactory, relationship="user")
    with pytest.raises(TypeError, match="targets 'FacUser', got 'FacRole'"):
        _ = FacPost.factory().for_user(FacRoleFactory)
    with pytest.raises(TypeError, match="targets 'FacPost', got 'FacRole'"):
        FacUser.factory().has(FacRoleFactory, relationship="posts").make()

    # Infinite recursion guard for mutual and self-referential factories
    class RecA(Model):
        __table__ = "rec_as"
        __fillable__ = ["b_id"]
        id: int
        b_id: int | None = None

    class RecB(Model):
        __table__ = "rec_bs"
        __fillable__ = ["a_id"]
        id: int
        a_id: int | None = None

    class RecAFactory(Factory[RecA]):
        model = RecA

        def definition(self) -> dict[str, Any]:
            return {"b_id": RecBFactory}

    class RecBFactory(Factory[RecB]):
        model = RecB

        def definition(self) -> dict[str, Any]:
            return {"a_id": RecAFactory}

    with pytest.raises(RecursionError, match="Maximum factory nesting depth"):
        RecAFactory().make()

    class SelfNode(Model):
        __table__ = "self_nodes"
        __fillable__ = ["parent_id"]
        id: int
        parent_id: int | None = None

    class SelfNodeFactory(Factory[SelfNode]):
        model = SelfNode

        def definition(self) -> dict[str, Any]:
            return {"parent_id": SelfNodeFactory}

    with pytest.raises(RecursionError, match="Maximum factory nesting depth"):
        SelfNodeFactory().make()


@pytest.mark.asyncio
async def test_factory_hooks_sync_async_order_and_make_async_rejection() -> None:
    """Verify sync/async hooks, batch execution counts, ordering, and async after_making guard."""
    async def async_hook(user: FacUser) -> None:
        await asyncio.sleep(0)
        _ = user

    with pytest.raises(TypeError, match="cannot be async when calling synchronous make"):
        FacUser.factory().after_making(async_hook).make()

    # Multiple sync hooks in order, executed once per batch item
    events: list[tuple[str, str]] = []
    users = (
        FacUser.factory()
        .count(3)
        .sequence({"name": "U1"}, {"name": "U2"}, {"name": "U3"})
        .after_making(lambda u: events.append(("m1", u.name)))
        .after_making(lambda u: events.append(("m2", u.name)))
        .make()
    )
    assert len(users) == 3
    assert events == [
        ("m1", "U1"),
        ("m2", "U1"),
        ("m1", "U2"),
        ("m2", "U2"),
        ("m1", "U3"),
        ("m2", "U3"),
    ]


def test_factory_discovery_edge_cases_errors_and_same_name_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify __factory__ validation, wrong-model detection, import error cleanup, repeated discovery, and multi-package collisions."""
    reg_snapshot = snapshot_factory_registry()
    clear_factory_registry()
    saved_mods = {
        k: v
        for k, v in list(sys.modules.items())
        if k in {"app", "database"} or k.startswith(("app.", "database.", "_swiftpy_factories"))
    }
    for k in saved_mods:
        sys.modules.pop(k, None)

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        # 1. Explicit __factory__ overrides convention discovery AND validates wrong classes
        class GoodItem(Model):
            __table__ = "good_items"
            __fillable__ = ["label"]
            id: int
            label: str

        class OtherItem(Model):
            __table__ = "other_items"
            __fillable__ = ["label"]
            id: int
            label: str

        class WrongModelFactory(Factory[OtherItem]):
            model = OtherItem

            def definition(self) -> dict[str, Any]:
                return {"label": "wrong"}

        # Invalid non-Factory __factory__
        good_cls_any: Any = GoodItem
        good_cls_any.__factory__ = "not_a_factory"
        with pytest.raises(TypeError, match="Invalid __factory__"):
            GoodItem.factory()

        # __factory__ bound to wrong model
        GoodItem.__factory__ = WrongModelFactory
        with pytest.raises(ValueError, match="targets 'OtherItem' instead"):
            GoodItem.factory()

        # Reset __factory__ and confirm registry remains clean
        GoodItem.__factory__ = None
        assert GoodItem not in snapshot_factory_registry()

        # Subclass without its own __factory__ does not inherit parent's __factory__
        class GoodItemFactory(Factory[GoodItem]):
            model = GoodItem

            def definition(self) -> dict[str, Any]:
                return {"label": "parent-factory"}

        class ChildGoodItem(GoodItem):
            __table__ = "child_good_items"

        GoodItem.__factory__ = GoodItemFactory
        child_built = ChildGoodItem.factory().make(label="child-ok")
        assert isinstance(child_built, ChildGoodItem)
        assert type(child_built) is ChildGoodItem
        assert child_built.label == "child-ok"
        GoodItem.__factory__ = None

        # Lazy _factory_resolver auto-initializes if None when Model.factory() is called
        prev_resolver = orm_model_mod._factory_resolver
        orm_model_mod._factory_resolver = None
        try:
            auto_resolved = GoodItem.factory().make(label="auto-init")
            assert auto_resolved.label == "auto-init"
            assert orm_model_mod._factory_resolver is not None
        finally:
            if orm_model_mod._factory_resolver is None and prev_resolver is not None:
                orm_model_mod._set_factory_resolver(prev_resolver)

        # 2. Convention discovery precedence vs explicit __factory__ + wrong-model in file + import errors
        models_dir = tmp_path / "app" / "models"
        pkg_a_dir = models_dir / "pkg_a"
        pkg_b_dir = models_dir / "pkg_b"
        pkg_a_dir.mkdir(parents=True)
        pkg_b_dir.mkdir(parents=True)
        (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
        (models_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_a_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_b_dir / "__init__.py").write_text("", encoding="utf-8")

        for pkg_name, pkg_dir in (("pkg_a", pkg_a_dir), ("pkg_b", pkg_b_dir)):
            (pkg_dir / "shared_item.py").write_text(
                f"""from __future__ import annotations
from swiftpy.database.orm import Model

class SharedItem(Model):
    __table__ = "{pkg_name}_shared_items"
    __fillable__ = ["label"]
    id: int
    label: str
""",
                encoding="utf-8",
            )

        fac_dir = tmp_path / "database" / "factories"
        (fac_dir / "pkg_a").mkdir(parents=True)
        (fac_dir / "pkg_b").mkdir(parents=True)
        (fac_dir / "pkg_a" / "shared_item_factory.py").write_text(
            """from __future__ import annotations
from typing import Any
from swiftpy.database.factories import Factory
from app.models.pkg_a.shared_item import SharedItem

class SharedItemFactory(Factory[SharedItem]):
    model = SharedItem

    def definition(self) -> dict[str, Any]:
        return {"label": "from-pkg-a"}
""",
            encoding="utf-8",
        )
        (fac_dir / "pkg_b" / "shared_item_factory.py").write_text(
            """from __future__ import annotations
from typing import Any
from swiftpy.database.factories import Factory
from app.models.pkg_b.shared_item import SharedItem

class SharedItemFactory(Factory[SharedItem]):
    model = SharedItem

    def definition(self) -> dict[str, Any]:
        return {"label": "from-pkg-b"}
""",
            encoding="utf-8",
        )

        mod_a = importlib.import_module("app.models.pkg_a.shared_item")
        mod_b = importlib.import_module("app.models.pkg_b.shared_item")
        assert mod_a.SharedItem.factory().make().label == "from-pkg-a"
        assert mod_b.SharedItem.factory().make().label == "from-pkg-b"

        # Repeated discovery reuses registration cleanly without duplication
        f1 = mod_a.SharedItem.factory()
        f2 = mod_a.SharedItem.factory()
        assert type(f1) is type(f2) and f1 is not f2

        # Explicit __factory__ overrides convention discovery on SharedItem
        class ExplicitAOverride(Factory[Any]):
            model = mod_a.SharedItem

            def definition(self) -> dict[str, Any]:
                return {"label": "explicit-wins-over-disk"}

        mod_a.SharedItem.__factory__ = ExplicitAOverride
        assert mod_a.SharedItem.factory().make().label == "explicit-wins-over-disk"
        mod_a.SharedItem.__factory__ = None

        # Broken factory file import error surfaces cleanly and does not pollute registry
        (models_dir / "broken_item.py").write_text(
            """from __future__ import annotations
from swiftpy.database.orm import Model

class BrokenItem(Model):
    __table__ = "broken_items"
    __fillable__ = ["label"]
    id: int
    label: str
""",
            encoding="utf-8",
        )
        (fac_dir / "broken_item_factory.py").write_text(
            "import nonexistent_module_for_factory_test\n",
            encoding="utf-8",
        )
        broken_mod = importlib.import_module("app.models.broken_item")
        with pytest.raises(ModuleNotFoundError, match="nonexistent_module_for_factory_test"):
            broken_mod.BrokenItem.factory()
        assert broken_mod.BrokenItem not in snapshot_factory_registry()

        # Convention file bound to wrong model fails cleanly and does not pollute registry
        (models_dir / "mismatched_item.py").write_text(
            """from __future__ import annotations
from swiftpy.database.orm import Model

class MismatchedItem(Model):
    __table__ = "mismatched_items"
    __fillable__ = ["label"]
    id: int
    label: str
""",
            encoding="utf-8",
        )
        (fac_dir / "mismatched_item_factory.py").write_text(
            """from __future__ import annotations
from typing import Any
from swiftpy.database.factories import Factory
from app.models.pkg_a.shared_item import SharedItem

class MismatchedItemFactory(Factory[SharedItem]):
    model = SharedItem

    def definition(self) -> dict[str, Any]:
        return {"label": "wrong"}
""",
            encoding="utf-8",
        )
        mismatched_mod = importlib.import_module("app.models.mismatched_item")
        with pytest.raises(ValueError, match="bound to model 'SharedItem'"):
            mismatched_mod.MismatchedItem.factory()
        assert mismatched_mod.MismatchedItem not in snapshot_factory_registry()
    finally:
        for k in list(sys.modules):
            if k in {"app", "database"} or k.startswith(
                ("app.", "database.", "_swiftpy_factories")
            ):
                sys.modules.pop(k, None)
        sys.modules.update(saved_mods)
        restore_factory_registry(reg_snapshot)


@pytest.mark.asyncio
async def test_pg_factory_transactions_hooks_custom_nullable_fks_and_failure_atomicity(
    factory_tables: DatabasePool,
) -> None:
    """Verify async/sync hook execution, non-persistence of make(), custom/nullable FKs, duplicate pivots, child failure atomicity, and transaction rollback."""
    assert factory_tables is not None

    # 1. make() never writes rows to PostgreSQL
    before_users = await FacUser.count()
    _ = FacUser.factory().count(4).has_posts(2).make()
    assert await FacUser.count() == before_users
    assert await FacPost.count() == 0

    # 2. count(1) returns list[T] on create()
    one_list = await FacUser.factory().count(1).create(name="CountOneCreate")
    assert isinstance(one_list, list) and len(one_list) == 1
    assert await FacUser.count() == before_users + 1

    # 3. Mixed async after_making + sync after_creating + batch once-per-model + exact order
    order_log: list[tuple[str, str]] = []

    async def async_making(user: FacUser) -> None:
        await asyncio.sleep(0)
        assert not getattr(user, "_exists", False)
        order_log.append(("making", user.name))

    def sync_creating(user: FacUser) -> None:
        assert user.id > 0
        order_log.append(("creating", user.name))

    hook_batch = await (
        FacUser.factory()
        .count(2)
        .sequence({"name": "H1"}, {"name": "H2"})
        .after_making(async_making)
        .after_creating(sync_creating)
        .create()
    )
    assert len(hook_batch) == 2
    assert order_log == [
        ("making", "H1"),
        ("creating", "H1"),
        ("making", "H2"),
        ("creating", "H2"),
    ]

    # 4. Callback in .has_posts() receives exact (attrs, parent) after parent save
    captured_parents: list[int] = []

    def inspect_parent_cb(attrs: dict[str, Any], parent: FacUser) -> dict[str, Any]:
        assert "title" in attrs and "body" in attrs
        assert parent.id > 0
        captured_parents.append(parent.id)
        return {"title": f"Child of {parent.id}"}

    cb_parent = await FacUser.factory().has_posts(2, inspect_parent_cb).create()
    assert captured_parents == [cb_parent.id, cb_parent.id]
    assert all(p.title == f"Child of {cb_parent.id}" for p in cb_parent.posts)

    # 5. Exception in after_creating rolls back parent & child records cleanly (zero orphans)
    users_before_hook_fail = await FacUser.count()
    posts_before_hook_fail = await FacPost.count()

    def failing_hook(_user: FacUser) -> None:
        raise RuntimeError("after_creating boom")

    with pytest.raises(RuntimeError, match="after_creating boom"):
        await FacUser.factory().has_posts(2).after_creating(failing_hook).create()

    assert await FacUser.count() == users_before_hook_fail
    assert await FacPost.count() == posts_before_hook_fail

    # 6. Child factory failure after parent insert rolls back parent cleanly (zero orphans)
    users_before_child_fail = await FacUser.count()
    posts_before_child_fail = await FacPost.count()

    def exploding_post(_attrs: dict[str, Any]) -> dict[str, Any]:
        return {"title": None}  # triggers NOT NULL violation on test_fac_posts.title

    with pytest.raises(asyncpg.NotNullViolationError):
        await (
            FacUser.factory()
            .has(FacPostFactory().state(exploding_post), count=1, relationship="posts")
            .create()
        )
    assert await FacUser.count() == users_before_child_fail
    assert await FacPost.count() == posts_before_child_fail

    # 7. Pivot callable with multiple attached models + duplicate attachment atomic rollback
    role_a = await FacRole.factory().create(name=f"role_a_{uuid.uuid4().hex[:6]}")
    role_b = await FacRole.factory().create(name=f"role_b_{uuid.uuid4().hex[:6]}")

    def pivot_active_for_role(_parent: FacUser, role: FacRole) -> dict[str, Any]:
        return {"active": role.id == role_a.id}

    pivot_user = await (
        FacUser.factory()
        .has_attached(
            [role_a, role_b],
            pivot_active_for_role,
            relationship="roles",
        )
        .create()
    )
    p_rows = (
        await Query.table(_TBL_ROLE_USER)
        .where("user_id", pivot_user.id)
        .order_by("role_id", "ASC")
        .get()
    )
    assert len(p_rows) == 2
    by_role = {r["role_id"]: r["active"] for r in p_rows}
    assert by_role[role_a.id] is True
    assert by_role[role_b.id] is False

    users_before_dup = await FacUser.count()
    with pytest.raises(asyncpg.UniqueViolationError):
        await (
            FacUser.factory()
            .has_attached([role_a, role_a], relationship="roles")
            .create()
        )
    assert await FacUser.count() == users_before_dup

    # 8. Explicit outer transaction rollback leaves zero users and zero posts
    users_before_tx = await FacUser.count()
    posts_before_tx = await FacPost.count()
    with pytest.raises(RuntimeError, match="Force rollback"):
        async with db.transaction():
            await FacUser.factory().count(3).has_posts(2).create()
            raise RuntimeError("Force rollback")
    assert await FacUser.count() == users_before_tx
    assert await FacPost.count() == posts_before_tx

    # 9. Custom foreign keys & nullable foreign keys (unique per-test tables with try/finally cleanup)
    sfx = uuid.uuid4().hex[:8]
    authors_tbl = f"tf_auth_{sfx}"
    notes_tbl = f"tf_note_{sfx}"

    class CustomNote(Model):
        __table__ = notes_tbl
        __fillable__ = ["author_ref", "body"]
        id: int
        author_ref: int | None = None
        body: str
        writer = BelongsTo("CustomAuthor", foreign_key="author_ref")

    class CustomAuthor(Model):
        __table__ = authors_tbl
        __fillable__ = ["pen_name"]
        id: int
        pen_name: str
        notes = HasMany(CustomNote, foreign_key="author_ref")

    class CustomNoteFactory(Factory[CustomNote]):
        model = CustomNote

        def definition(self) -> dict[str, Any]:
            return {"author_ref": None, "body": self.fake.sentence(4)}

    class CustomAuthorFactory(Factory[CustomAuthor]):
        model = CustomAuthor

        def definition(self) -> dict[str, Any]:
            return {"pen_name": self.fake.name()}

    try:
        await Schema.create(
            authors_tbl,
            lambda t: (t.id(), t.string("pen_name")),
        )
        await Schema.create(
            notes_tbl,
            lambda t: (
                t.id(),
                t.big_integer("author_ref").nullable(),
                t.string("body"),
                t.foreign_key(
                    "author_ref",
                    references="id",
                    on=authors_tbl,
                    on_delete="SET NULL",
                    name=f"fk_tf_note_{sfx}",
                ),
            ),
        )
        # Nullable FK defaults to NULL cleanly when no parent provided
        orphan_note = await CustomNoteFactory().create()
        assert orphan_note.author_ref is None

        # Custom FK via .has_notes() and .for_writer()
        writer = await CustomAuthorFactory().has_notes(2).create()
        assert len(writer.notes) == 2
        assert all(n.author_ref == writer.id for n in writer.notes)

        linked_note = await CustomNoteFactory().for_writer(writer).create()
        assert linked_note.author_ref == writer.id
    finally:
        await Schema.drop_if_exists(notes_tbl)
        await Schema.drop_if_exists(authors_tbl)
