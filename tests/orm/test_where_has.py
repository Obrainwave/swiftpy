from __future__ import annotations

from collections.abc import Generator
from typing import Any, ClassVar

import pytest

from swiftpy.database.orm.model import (
    Model,
    restore_model_registry,
    snapshot_model_registry,
)
from swiftpy.database.orm.relationships import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
)
from swiftpy.database.pool import DatabasePool


class WhereHasAccount(Model):
    __table__ = "test_wh_users"
    __guarded__: ClassVar[list[str]] = []

    id: int
    name: str

    posts = HasMany("WhereHasPost", foreign_key="account_id")
    activeSubscription = HasOne("WhereHasSubscription", foreign_key="account_id")
    roles = BelongsToMany(
        "WhereHasRole",
        pivot_table="test_wh_account_roles",
        foreign_pivot_key="account_id",
        related_pivot_key="role_id",
    )


class WhereHasSubscription(Model):
    __table__ = "test_wh_subscriptions"
    __guarded__: ClassVar[list[str]] = []

    id: int
    account_id: int
    status: str


class WhereHasPost(Model):
    __table__ = "test_wh_posts"
    __guarded__: ClassVar[list[str]] = []

    id: int
    account_id: int | None
    title: str
    published: bool

    author = BelongsTo("WhereHasAccount", foreign_key="account_id")
    comments = HasMany("WhereHasComment", foreign_key="post_id")


class WhereHasComment(Model):
    __table__ = "test_wh_comments"
    __guarded__: ClassVar[list[str]] = []

    id: int
    post_id: int
    approved: bool
    body: str

    post = BelongsTo("WhereHasPost", foreign_key="post_id")


class WhereHasRole(Model):
    __table__ = "test_wh_roles"
    __guarded__: ClassVar[list[str]] = []

    id: int
    name: str


@pytest.fixture(scope="module", autouse=True)
def _preserve_model_registry() -> Generator[None, None, None]:
    snap = snapshot_model_registry()
    yield
    restore_model_registry(snap)


@pytest.fixture
async def setup_where_has_schema(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS
                test_wh_account_roles,
                test_wh_roles,
                test_wh_comments,
                test_wh_posts,
                test_wh_subscriptions,
                test_wh_users
            CASCADE;

            CREATE TABLE test_wh_users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE test_wh_subscriptions (
                id SERIAL PRIMARY KEY,
                account_id INTEGER REFERENCES test_wh_users(id) ON DELETE CASCADE,
                status TEXT NOT NULL
            );
            CREATE TABLE test_wh_posts (
                id SERIAL PRIMARY KEY,
                account_id INTEGER REFERENCES test_wh_users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                published BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE TABLE test_wh_comments (
                id SERIAL PRIMARY KEY,
                post_id INTEGER REFERENCES test_wh_posts(id) ON DELETE CASCADE,
                approved BOOLEAN NOT NULL DEFAULT FALSE,
                body TEXT NOT NULL
            );
            CREATE TABLE test_wh_roles (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE test_wh_account_roles (
                account_id INTEGER REFERENCES test_wh_users(id) ON DELETE CASCADE,
                role_id INTEGER REFERENCES test_wh_roles(id) ON DELETE CASCADE
            );
            """
        )

    alice = await WhereHasAccount.create({"name": "Alice"})
    bob = await WhereHasAccount.create({"name": "Bob"})
    await WhereHasAccount.create({"name": "Carol"})

    await WhereHasSubscription.create({"account_id": alice.id, "status": "active"})
    await WhereHasSubscription.create({"account_id": bob.id, "status": "canceled"})

    alice_post_1 = await WhereHasPost.create(
        {"account_id": alice.id, "title": "A1", "published": True}
    )
    alice_post_2 = await WhereHasPost.create(
        {"account_id": alice.id, "title": "A2", "published": False}
    )
    bob_post = await WhereHasPost.create(
        {"account_id": bob.id, "title": "B1", "published": True}
    )
    # Orphan post with NULL FK to verify NULL-safe NOT IN behavior
    await WhereHasPost.create(
        {"account_id": None, "title": "Orphan", "published": True}
    )

    await WhereHasComment.create(
        {"post_id": alice_post_1.id, "approved": True, "body": "Great"}
    )
    await WhereHasComment.create(
        {"post_id": alice_post_2.id, "approved": False, "body": "Draft note"}
    )
    await WhereHasComment.create(
        {"post_id": bob_post.id, "approved": False, "body": "Pending"}
    )

    admin_role = await WhereHasRole.create({"name": "admin"})
    editor_role = await WhereHasRole.create({"name": "editor"})
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO test_wh_account_roles (account_id, role_id) VALUES ($1, $2), ($3, $4)",
            alice.id,
            admin_role.id,
            bob.id,
            editor_role.id,
        )


def test_has_and_where_has_validation_errors() -> None:
    with pytest.raises(ValueError, match="not defined"):
        WhereHasAccount.has("nonexistent")

    with pytest.raises(ValueError, match="operator"):
        WhereHasAccount.has("posts", "LIKE", 1)

    with pytest.raises(ValueError, match="non-negative integer"):
        WhereHasAccount.has("posts", ">=", -1)

    async def _async_cb(q: Any) -> None:
        return None

    with pytest.raises(TypeError, match="synchronous"):
        WhereHasAccount.where_has("posts", _async_cb)


def test_has_and_doesnt_have_sql_generation() -> None:
    sql_has, bindings_has = WhereHasAccount.has("posts", ">=", 2).builder.to_sql()
    assert 'IN (SELECT "test_wh_posts"."account_id" FROM "test_wh_posts"' in sql_has
    assert '"test_wh_posts"."account_id" IS NOT NULL' in sql_has
    assert "HAVING COUNT(*) >= $1" in sql_has
    assert bindings_has == [2]

    sql_none, bindings_none = WhereHasAccount.doesnt_have("posts").builder.to_sql()
    assert '"test_wh_users"."id" IS NULL OR "test_wh_users"."id" NOT IN' in sql_none
    assert '"test_wh_posts"."account_id" IS NOT NULL' in sql_none
    assert bindings_none == []


@pytest.mark.asyncio
async def test_has_one_many_belongs_to_many_queries(setup_where_has_schema: None) -> None:
    has_sub = await WhereHasAccount.has("activeSubscription").order_by("id").get()
    assert [u.name for u in has_sub] == ["Alice", "Bob"]

    active_sub = (
        await WhereHasAccount.where_has(
            "activeSubscription", lambda q: q.where("status", "active")
        )
        .order_by("id")
        .get()
    )
    assert [u.name for u in active_sub] == ["Alice"]

    # Counts & operators
    multi_posts = await WhereHasAccount.has("posts", ">=", 2).order_by("id").get()
    assert [u.name for u in multi_posts] == ["Alice"]

    lt_two_posts = await WhereHasAccount.has("posts", "<", 2).order_by("id").get()
    assert [u.name for u in lt_two_posts] == ["Bob", "Carol"]

    exact_zero_posts = await WhereHasAccount.has("posts", "=", 0).order_by("id").get()
    assert [u.name for u in exact_zero_posts] == ["Carol"]

    # Negative existence (despite NULL account_id on Orphan post)
    no_posts = await WhereHasAccount.doesnt_have("posts").order_by("id").get()
    assert [u.name for u in no_posts] == ["Carol"]

    # where_doesnt_have with callback (Bob only has canceled subscription & published post)
    no_published = (
        await WhereHasAccount.where_doesnt_have(
            "posts", lambda q: q.where("published", True)
        )
        .order_by("id")
        .get()
    )
    assert [u.name for u in no_published] == ["Carol"]

    # BelongsTo including NULL FK row
    with_author = await WhereHasPost.has("author").order_by("id").get()
    assert [p.title for p in with_author] == ["A1", "A2", "B1"]

    without_author = await WhereHasPost.doesnt_have("author").order_by("id").get()
    assert [p.title for p in without_author] == ["Orphan"]

    # BelongsToMany
    admins = (
        await WhereHasAccount.where_has("roles", lambda q: q.where("name", "admin"))
        .order_by("id")
        .get()
    )
    assert [u.name for u in admins] == ["Alice"]

    no_roles = await WhereHasAccount.doesnt_have("roles").order_by("id").get()
    assert [u.name for u in no_roles] == ["Carol"]

    # OR combinators
    or_result = (
        await WhereHasAccount.where("name", "Carol")
        .or_where_has("roles", lambda q: q.where("name", "admin"))
        .order_by("id")
        .get()
    )
    assert [u.name for u in or_result] == ["Alice", "Carol"]

    or_no_posts = (
        await WhereHasAccount.where("name", "Bob")
        .or_doesnt_have("posts")
        .order_by("id")
        .get()
    )
    assert [u.name for u in or_no_posts] == ["Bob", "Carol"]

    # Dot-nested relations
    approved_commenters = (
        await WhereHasAccount.where_has(
            "posts.comments", lambda q: q.where("approved", True)
        )
        .order_by("id")
        .get()
    )
    assert [u.name for u in approved_commenters] == ["Alice"]

    no_comments_through_posts = (
        await WhereHasAccount.doesnt_have("posts.comments").order_by("id").get()
    )
    assert [u.name for u in no_comments_through_posts] == ["Carol"]

    # Explicit or_has and or_where_doesnt_have
    or_has_res = (
        await WhereHasAccount.where("name", "Carol")
        .or_has("posts", ">=", 2)
        .order_by("id")
        .get()
    )
    assert [u.name for u in or_has_res] == ["Alice", "Carol"]

    or_where_doesnt_res = (
        await WhereHasAccount.where("name", "Alice")
        .or_where_doesnt_have("posts", lambda q: q.where("published", True))
        .order_by("id")
        .get()
    )
    assert [u.name for u in or_where_doesnt_res] == ["Alice", "Carol"]

    # Remaining count operators: >, <=, !=, exact = 1 and = 2
    gt_one = await WhereHasAccount.has("posts", ">", 1).order_by("id").get()
    assert [u.name for u in gt_one] == ["Alice"]

    lte_one = await WhereHasAccount.has("posts", "<=", 1).order_by("id").get()
    assert [u.name for u in lte_one] == ["Bob", "Carol"]

    neq_one = await WhereHasAccount.has("posts", "!=", 1).order_by("id").get()
    assert [u.name for u in neq_one] == ["Alice", "Carol"]

    eq_one = await WhereHasAccount.has("posts", "=", 1).order_by("id").get()
    assert [u.name for u in eq_one] == ["Bob"]

    eq_two = await WhereHasAccount.has("posts", "=", 2).order_by("id").get()
    assert [u.name for u in eq_two] == ["Alice"]

    # Nested where_doesnt_have callback + multi-condition callback
    no_approved_comments = (
        await WhereHasAccount.where_doesnt_have(
            "posts.comments", lambda q: q.where("approved", True)
        )
        .order_by("id")
        .get()
    )
    assert [u.name for u in no_approved_comments] == ["Bob", "Carol"]

    # Chained BelongsTo nested relation (Comment -> Post -> Author)
    alice_comments = (
        await WhereHasComment.where_has(
            "post.author", lambda q: q.where("name", "Alice")
        )
        .order_by("id")
        .get()
    )
    assert [c.body for c in alice_comments] == ["Great", "Draft note"]

    # Parameterization safety & logical grouping in callbacks
    injection_probe = "' OR '1'='1"
    probe_qb = WhereHasAccount.where_has(
        "posts",
        lambda q: q.where("published", True).where("title", injection_probe),
    )
    probe_sql, probe_bindings = probe_qb.builder.to_sql()
    assert injection_probe not in probe_sql
    assert injection_probe in probe_bindings
    assert await probe_qb.get() == []

    # Nested OR inside where_has must remain scoped within subquery (must not match Alice)
    scoped_or = (
        await WhereHasAccount.where("name", "Bob")
        .where_has(
            "posts", lambda q: q.where("published", False).or_where("title", "B1")
        )
        .get()
    )
    assert [u.name for u in scoped_or] == ["Bob"]
