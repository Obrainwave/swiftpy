# tests/orm/test_eager_loading.py

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg

from app.providers.app_service_provider import AppServiceProvider
from app.providers.database_service_provider import DatabaseServiceProvider
from swiftpy.core.bootstrap import create_app
from swiftpy.database.orm.model import Model
from swiftpy.database.orm.relationships import (
    BelongsTo,
    HasMany,
    HasOne,
    RelationNotLoadedError,
)
from swiftpy.database.pool import DatabasePool


class QueryCounter:
    """Helper to track SQL queries executed on asyncpg connections."""

    def __init__(self) -> None:
        self.count = 0

    def increment(self) -> None:
        self.count += 1


# Models demonstrating multiple relationship types
class Profile(Model):
    __table__ = "orm_profiles"
    user_id: int
    bio: str
    user = BelongsTo("User", foreign_key="user_id")


class Post(Model):
    __table__ = "orm_posts"
    user_id: int
    title: str
    published: bool = True
    user = BelongsTo("User", foreign_key="user_id")


class User(Model):
    __table__ = "orm_users"
    name: str
    email: str = ""
    # Model with two distinct relationship types:
    posts = HasMany("Post", foreign_key="user_id")
    profile = HasOne("Profile", foreign_key="user_id")


async def setup_schema(db_pool: DatabasePool) -> None:
    pool = db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS orm_profiles, orm_posts, orm_users CASCADE;

            CREATE TABLE orm_users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE orm_posts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES orm_users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                published BOOLEAN DEFAULT TRUE
            );

            CREATE TABLE orm_profiles (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES orm_users(id) ON DELETE CASCADE,
                bio TEXT NOT NULL
            );
            """
        )


async def seed_data() -> None:
    for user_index in range(10):
        user = await User.create(
            name=f"user_{user_index}",
            email=f"user_{user_index}@example.com",
        )

        # Create 5 posts per user (1 unpublished, 4 published)
        for post_index in range(5):
            await Post.create(
                user_id=user.id,
                title=f"post_{user_index}_{post_index}",
                published=(post_index != 0),
            )

        # Create 1 profile per user
        await Profile.create(
            user_id=user.id,
            bio=f"Bio of user_{user_index}",
        )


import pytest


@pytest.fixture(autouse=True)
async def orm_test_setup(ensure_db_pool: DatabasePool) -> None:
    await setup_schema(ensure_db_pool)
    await seed_data()


async def test_has_many_eager_loading_avoids_n_plus_one() -> None:
    print("\n==== HAS MANY EAGER LOADING ====")
    counter = QueryCounter()
    original_fetch = asyncpg.Connection.fetch

    async def wrapped_fetch(self: Any, *args: Any, **kwargs: Any) -> Any:
        counter.increment()
        return await original_fetch(self, *args, **kwargs)

    asyncpg.Connection.fetch = wrapped_fetch
    try:
        users = await User.with_("posts").get()
        assert len(users) == 10

        for user in users:
            posts = user.posts
            assert len(posts) == 5

        # 1 query for users + 1 batched query for posts (WHERE user_id IN (...))
        assert counter.count <= 2, f"N+1 detected. Query count={counter.count}"
        print(
            f"PASS: Loaded {len(users)} users and 50 posts with query count={counter.count}"
        )
    finally:
        asyncpg.Connection.fetch = original_fetch


async def test_two_relationship_types_eager_loading_avoids_n_plus_one() -> None:
    print("\n==== TWO RELATIONSHIP TYPES EAGER LOADING ====")
    counter = QueryCounter()
    original_fetch = asyncpg.Connection.fetch

    async def wrapped_fetch(self: Any, *args: Any, **kwargs: Any) -> Any:
        counter.increment()
        return await original_fetch(self, *args, **kwargs)

    asyncpg.Connection.fetch = wrapped_fetch
    try:
        # Eager load both HasMany ('posts') and HasOne ('profile') on User
        users = await User.query().with_("posts", "profile").get()
        assert len(users) == 10

        for user in users:
            assert len(user.posts) == 5
            assert user.profile is not None
            assert user.profile.bio == f"Bio of {user.name}"

        # 1 query for users + 1 query for posts + 1 query for profiles = 3 queries (never 21)
        assert counter.count <= 3, (
            f"N+1 detected on dual relationships. Query count={counter.count}"
        )
        print(
            f"PASS: Dual relationship eager load (HasMany + HasOne) completed in {counter.count} queries"
        )
    finally:
        asyncpg.Connection.fetch = original_fetch


async def test_belongs_to_eager_loading_avoids_n_plus_one() -> None:
    print("\n==== BELONGS TO EAGER LOADING ====")
    counter = QueryCounter()
    original_fetch = asyncpg.Connection.fetch

    async def wrapped_fetch(self: Any, *args: Any, **kwargs: Any) -> Any:
        counter.increment()
        return await original_fetch(self, *args, **kwargs)

    asyncpg.Connection.fetch = wrapped_fetch
    try:
        posts = await Post.with_("user").get()
        assert len(posts) == 50

        for post in posts:
            assert post.user is not None
            assert post.user.id == post.user_id

        assert counter.count <= 2, f"N+1 detected. Query count={counter.count}"
        print(
            f"PASS: Loaded {len(posts)} posts with owners in query count={counter.count}"
        )
    finally:
        asyncpg.Connection.fetch = original_fetch


async def test_un_eager_loaded_relation_raises_error_to_prevent_n_plus_one() -> None:
    print("\n==== UN-EAGER-LOADED ERROR PREVENTION (N+1 GUARD) ====")
    users = await User.query().get()
    assert len(users) > 0
    first_user = users[0]

    # Attempting synchronous iteration must raise RelationNotLoadedError
    iter_raised = False
    try:
        for _ in first_user.posts:
            pass
    except RelationNotLoadedError:
        iter_raised = True
    assert iter_raised, (
        "Synchronous iteration on un-eager-loaded relation did not raise RelationNotLoadedError"
    )

    # Attempting len() must raise RelationNotLoadedError
    len_raised = False
    try:
        _ = len(first_user.posts)
    except RelationNotLoadedError:
        len_raised = True
    assert len_raised, (
        "len() on un-eager-loaded relation did not raise RelationNotLoadedError"
    )

    # Attempting bool() check must raise RelationNotLoadedError
    bool_raised = False
    try:
        _ = bool(first_user.posts)
    except RelationNotLoadedError:
        bool_raised = True
    assert bool_raised, (
        "bool() on un-eager-loaded relation did not raise RelationNotLoadedError"
    )

    # Accessing attributes directly on un-eager-loaded HasOne must raise RelationNotLoadedError
    attr_raised = False
    try:
        _ = first_user.profile.bio
    except RelationNotLoadedError:
        attr_raised = True
    assert attr_raised, (
        "Attribute access on un-eager-loaded relation did not raise RelationNotLoadedError"
    )

    print("PASS: Un-eager-loaded access strictly raises RelationNotLoadedError")


async def test_lazily_awaitable_query_builder_scoped_to_relationship() -> None:
    print("\n==== LAZILY AWAITABLE RELATIONSHIP QUERY BUILDER ====")
    user = (await User.query().get())[0]

    # 1. Lazily awaiting the relationship builder directly: `await user.posts`
    posts = await user.posts
    assert len(posts) == 5

    # 2. Scoped query filtering: `await user.posts.where("published", True).get()`
    published_posts = await user.posts.where("published", True).get()
    assert len(published_posts) == 4
    for post in published_posts:
        assert post.published is True
        assert post.user_id == user.id

    unpublished_posts = await user.posts.where("published", False).get()
    assert len(unpublished_posts) == 1
    assert unpublished_posts[0].published is False

    print("PASS: Lazily awaitable and scoped relationship queries")


async def test_target_orm_api() -> None:
    print("\n==== TARGET ORM API VERIFICATION ====")
    # Target API specified in requirements:
    # user = await User.create(name="Akeem", email="a@x.com")
    # posts = await user.posts.where("published", True).get()
    # users = await User.query().with_("posts").get()

    user = await User.create(name="Akeem", email="a@x.com")
    assert user.id is not None
    assert user.name == "Akeem"
    assert user.email == "a@x.com"

    await Post.create(user_id=user.id, title="Target Post 1", published=True)
    await Post.create(user_id=user.id, title="Target Post 2", published=False)

    posts = await user.posts.where("published", True).get()
    assert len(posts) == 1
    assert posts[0].title == "Target Post 1"

    users = await User.query().with_("posts").get()
    found = next((u for u in users if u.id == user.id), None)
    assert found is not None
    assert len(found.posts) == 2

    print("PASS: Target ORM API verified successfully")


async def test_active_record_crud_lifecycle() -> None:
    print("\n==== ACTIVE RECORD CRUD LIFECYCLE ====")
    # Create
    item = await User.create(name="CRUD Test", email="crud@example.com")
    assert item.id is not None

    # Find
    loaded = await User.find(item.id)
    assert loaded is not None
    assert loaded.name == "CRUD Test"

    # Save (update)
    loaded.name = "CRUD Test Updated"
    await loaded.save()

    refreshed = await User.find(item.id)
    assert refreshed is not None
    assert refreshed.name == "CRUD Test Updated"

    # Delete
    await refreshed.delete()
    deleted = await User.find(item.id)
    assert deleted is None

    print("PASS: Model CRUD lifecycle (create, find, save, delete)")


async def main() -> None:
    app = create_app(providers=[AppServiceProvider, DatabaseServiceProvider])
    db_pool = app.container.resolve(DatabasePool)
    await db_pool.init()

    try:
        await setup_schema(db_pool)
        await seed_data()

        await test_has_many_eager_loading_avoids_n_plus_one()
        await test_two_relationship_types_eager_loading_avoids_n_plus_one()
        await test_belongs_to_eager_loading_avoids_n_plus_one()
        await test_un_eager_loaded_relation_raises_error_to_prevent_n_plus_one()
        await test_lazily_awaitable_query_builder_scoped_to_relationship()
        await test_target_orm_api()
        await test_active_record_crud_lifecycle()

        print("\n==== ALL WEEK 8 ACTIVE RECORD ORM TESTS PASSED ====\n")
    finally:
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
