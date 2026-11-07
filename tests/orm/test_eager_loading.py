# tests/orm/test_eager_loading.py

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, ClassVar

import asyncpg
import pytest

from app.providers.app_service_provider import AppServiceProvider
from app.providers.database_service_provider import DatabaseServiceProvider
from swiftpy.core.bootstrap import create_app
from swiftpy.database.orm.model import Model
from swiftpy.database.orm.relationships import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
    RelationNotLoadedError,
)
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query
from swiftpy.helpers.helper import pluralize


class QueryCounter:
    """Helper to track SQL queries executed on asyncpg connections."""

    def __init__(self) -> None:
        self.count = 0

    def increment(self) -> None:
        self.count += 1


# Models demonstrating multiple relationship types and inheritance
class Role(Model):
    __table__ = "orm_roles"
    id: int
    name: str
    users = BelongsToMany("User", pivot_table="orm_user_roles")
    __guarded__: ClassVar[list[str]] = []


class Profile(Model):
    __table__ = "orm_profiles"
    id: int
    user_id: int
    bio: str
    user = BelongsTo("User", foreign_key="user_id")
    __guarded__: ClassVar[list[str]] = []


class Post(Model):
    __table__ = "orm_posts"
    id: int
    user_id: int
    title: str
    published: bool = True
    deleted_at: Any = None
    user = BelongsTo("User", foreign_key="user_id")
    __guarded__: ClassVar[list[str]] = []


class User(Model):
    __table__ = "orm_users"
    id: int
    name: str
    email: str = ""
    is_admin: bool = False
    posts = HasMany("Post", foreign_key="user_id")
    profile = HasOne("Profile", foreign_key="user_id")
    roles = BelongsToMany("Role", pivot_table="orm_user_roles")
    __guarded__: ClassVar[list[str]] = []


class AdminUser(User):
    __table__ = "orm_users"


class SecureUser(Model):
    __table__ = "orm_users"
    id: int
    name: str
    email: str = ""
    is_admin: bool = False
    __fillable__: ClassVar[list[str]] = ["name", "email"]


async def setup_schema(db_pool: DatabasePool) -> None:
    pool = db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS orm_user_roles, orm_roles, orm_profiles, orm_posts, orm_users CASCADE;

            CREATE TABLE orm_users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                is_admin BOOLEAN DEFAULT FALSE
            );

            CREATE TABLE orm_posts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES orm_users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                published BOOLEAN DEFAULT TRUE,
                deleted_at TIMESTAMPTZ DEFAULT NULL
            );

            CREATE TABLE orm_profiles (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES orm_users(id) ON DELETE CASCADE,
                bio TEXT NOT NULL
            );

            CREATE TABLE orm_roles (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL
            );

            CREATE TABLE orm_user_roles (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES orm_users(id) ON DELETE CASCADE,
                role_id INTEGER NOT NULL REFERENCES orm_roles(id) ON DELETE CASCADE
            );
            """
        )


async def seed_data() -> None:
    # Seed roles
    role_admin = await Role.create(name="Administrator")
    role_editor = await Role.create(name="Editor")

    for user_index in range(10):
        user = await User.create(
            name=f"user_{user_index}",
            email=f"user_{user_index}@example.com",
        )

        # Create 5 posts per user (1 unpublished, 4 published; post 0 has deleted_at set)
        for post_index in range(5):
            deleted = (
                datetime(2026, 10, 18, 10, 0, 0, tzinfo=UTC)
                if post_index == 0
                else None
            )
            await Post.create(
                user_id=user.id,
                title=f"post_{user_index}_{post_index}",
                published=(post_index != 0),
                deleted_at=deleted,
            )

        # Create 1 profile per user
        await Profile.create(
            user_id=user.id,
            bio=f"Bio of user_{user_index}",
        )

        # Attach roles to first two users
        if user_index == 0:
            await Query.table("orm_user_roles").insert(
                {"user_id": user.id, "role_id": role_admin.id}
            )
            await Query.table("orm_user_roles").insert(
                {"user_id": user.id, "role_id": role_editor.id}
            )
        elif user_index == 1:
            await Query.table("orm_user_roles").insert(
                {"user_id": user.id, "role_id": role_editor.id}
            )


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
    assert len(found.posts) >= 2

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


async def test_partial_select_save_does_not_clobber_other_columns() -> None:
    print("\n==== PARTIAL SELECT DIRTY TRACKING ====")
    user = await User.create(name="keep", email="keep@example.com")
    partial = await User.query().select("id", "name").where("id", user.id).first()
    assert partial is not None
    partial.name = "renamed"
    await partial.save()
    reloaded = await User.find(user.id)
    assert reloaded is not None
    assert reloaded.email == "keep@example.com"
    print("PASS: Partial select save does not clobber unselected columns")


async def test_loaded_list_is_awaitable_in_both_states() -> None:
    print("\n==== LOADED LIST AWAITABILITY ====")
    # 1. Lazy state: awaiting user.posts executes query
    lazy_user = (await User.query().get())[0]
    posts_from_lazy = await lazy_user.posts
    assert len(posts_from_lazy) == 5

    # 2. Eager-loaded state: user.posts is LoadedList and can also be awaited
    eager_user = (await User.query().with_("posts").get())[0]
    posts_from_eager = await eager_user.posts
    assert len(posts_from_eager) == 5
    # Also functions as regular list
    assert len(eager_user.posts) == 5
    assert eager_user.posts[0].title.startswith("post_")
    print("PASS: user.posts is awaitable in both lazy and eager-loaded states")


async def test_where_null_and_where_not_null() -> None:
    print("\n==== WHERE NULL AND WHERE NOT NULL ====")
    # Total posts: 50. In seed_data, post_index 0 has deleted_at set (10 posts), others None (40 posts)
    null_posts = await Post.query().where_null("deleted_at").get()
    not_null_posts = await Post.query().where_not_null("deleted_at").get()
    total_posts = await Post.query().count()

    assert len(not_null_posts) == 10, (
        f"Expected 10 not null posts, got {len(not_null_posts)}"
    )
    assert len(null_posts) == total_posts - 10
    assert len(null_posts) + len(not_null_posts) == total_posts
    print("PASS: where_null and where_not_null execute proper SQL IS (NOT) NULL")


async def test_belongs_to_many_eager_and_lazy() -> None:
    print("\n==== BELONGS TO MANY RELATIONSHIP ====")
    # 1. Eager load
    users = await User.query().with_("roles").get()
    user0 = users[0]
    user1 = users[1]
    user2 = users[2]

    assert len(user0.roles) == 2
    role_names = {r.name for r in user0.roles}
    assert "Administrator" in role_names
    assert "Editor" in role_names
    # Verify target attributes like id and name are correct (not pivot columns)
    for r in user0.roles:
        assert isinstance(r.id, int)
        assert isinstance(r.name, str)

    assert len(user1.roles) == 1
    assert len(user2.roles) == 0

    # 2. Lazy query
    lazy_user0 = await User.find(user0.id)
    assert lazy_user0 is not None
    lazy_roles = await lazy_user0.roles.get()
    assert len(lazy_roles) == 2

    # 3. BelongsToMany.create() raises NotImplementedError to prevent unlinked orphan rows
    with pytest.raises(NotImplementedError, match="attach"):
        await lazy_user0.roles.create(name="OrphanRole")

    print("PASS: BelongsToMany eager loading, lazy loading, and attach() enforcement")


async def test_relation_inheritance() -> None:
    print("\n==== RELATION INHERITANCE ====")
    # AdminUser inherits posts, profile, and roles from User
    admin = (
        await AdminUser.query().with_("posts", "roles").where("name", "user_0").first()
    )
    assert admin is not None
    assert len(admin.posts) == 5
    assert len(admin.roles) == 2
    print("PASS: Subclasses inherit parent model relations cleanly")


async def test_fillable_mass_assignment_protection() -> None:
    print("\n==== FILLABLE MASS ASSIGNMENT PROTECTION ====")
    # SecureUser has __fillable__ = ["name", "email"], is_admin should be ignored
    user = await SecureUser.create(
        name="SecurityTest",
        email="sec@example.com",
        is_admin=True,
    )
    loaded = await SecureUser.find(user.id)
    assert loaded is not None
    assert loaded.name == "SecurityTest"
    assert loaded.email == "sec@example.com"
    # is_admin was not passed to insert, so it defaults to False in database
    assert loaded.is_admin is False
    print("PASS: Mass assignment protection honors __fillable__")


async def test_pluralize_and_table_name_resolution() -> None:
    print("\n==== PLURALIZATION & TABLE NAME RESOLUTION ====")

    test_words = [
        ("user", "users"),
        ("company", "companies"),
        ("bus", "buses"),
        ("class", "classes"),
        ("box", "boxes"),
        ("analysis", "analyses"),
        ("axis", "axes"),
        ("database", "databases"),
        ("quiz", "quizzes"),
        ("mouse", "mice"),
        ("louse", "lice"),
        ("matrix", "matrices"),
        ("status", "statuses"),
        ("index", "indices"),
        ("sheep", "sheep"),
        ("Person", "People"),
        ("Category", "Categories"),
    ]

    for singular, expected in test_words:
        assert pluralize(singular) == expected, (
            f"Failed on {singular}: expected {expected}, got {pluralize(singular)}"
        )

    # Default model table name generation
    class Company(Model):
        pass

    class Bus(Model):
        pass

    class CustomPerson(Model):
        __table__ = "special_people"

    assert Company.__table__ == "companies"
    assert Bus.__table__ == "buses"
    assert CustomPerson.__table__ == "special_people"
    print("PASS: Pluralization rules and explicit __table__ overrides verified")


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
        await test_partial_select_save_does_not_clobber_other_columns()
        await test_loaded_list_is_awaitable_in_both_states()
        await test_where_null_and_where_not_null()
        await test_belongs_to_many_eager_and_lazy()
        await test_relation_inheritance()
        await test_fillable_mass_assignment_protection()
        await test_pluralize_and_table_name_resolution()

        print("\n==== ALL WEEK 8 ACTIVE RECORD ORM TESTS PASSED ====\n")
    finally:
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
