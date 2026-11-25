from __future__ import annotations

from typing import Any, ClassVar

import pytest

from swiftpy.database import QueryDumpError
from swiftpy.database.orm.fields import Field
from swiftpy.database.orm.model import MassAssignmentError, Model, ModelQueryBuilder
from swiftpy.database.orm.relationships import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
)
from swiftpy.http.response import json_response


class Role(Model):
    __table__ = "typing_roles"
    id: int
    name: str
    __guarded__: ClassVar[list[str]] = []


class Profile(Model):
    __table__ = "typing_profiles"
    id: int
    user_id: int
    bio: str
    user = BelongsTo("User", foreign_key="user_id")
    __guarded__: ClassVar[list[str]] = []


class Post(Model):
    __table__ = "typing_posts"
    id: int
    user_id: int
    title: str
    published: bool = True
    user = BelongsTo("User", foreign_key="user_id")
    __guarded__: ClassVar[list[str]] = []


class User(Model):
    __table__ = "typing_users"
    id: int
    name: str
    email: str
    tags: list[str] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)
    is_admin: bool = False
    posts = HasMany("Post", foreign_key="user_id")
    profile = HasOne("Profile", foreign_key="user_id")
    roles = BelongsToMany("Role", pivot_table="typing_user_roles")
    __hidden__: ClassVar[tuple[str, ...]] = ("is_admin",)
    __guarded__: ClassVar[list[str]] = []


async def test_static_typing_contracts() -> None:
    """Verify that SwiftPY ORM constructs type-check strictly without mypy errors."""
    user = User(name="Alice", email="alice@example.com")

    # 1. Attribute access
    name: str = user.name
    email: str = user.email
    assert name == "Alice"
    assert email == "alice@example.com"

    # 2. Hidden & serialization helpers
    user.make_visible("is_admin")
    user.make_hidden("email")
    data: dict[str, Any] = user.to_dict(include_hidden=True)
    assert isinstance(data, dict)

    # 3. Dirty tracking methods
    dirty: bool = user.is_dirty("name")
    dirty_dict: dict[str, Any] = user.get_dirty()
    assert isinstance(dirty, bool)
    assert isinstance(dirty_dict, dict)

    # 4. HasMany relationship operations on loaded relation
    p1 = Post(id=1, user_id=1, title="Hello World")
    user._loaded_relations["posts"] = [p1]

    posts_len: int = len(user.posts)
    assert posts_len == 1

    for p in user.posts:
        p_title: str = p.title
        assert p_title == "Hello World"

    first_post = user.posts[0]
    assert first_post.title == "Hello World"

    # Query builder operations
    builder = user.posts_query if hasattr(user, "posts_query") else User.posts.build_lazy_query(user)
    assert builder is not None
    extended_query: ModelQueryBuilder[User] = (
        User.query()
        .where_column("typing_users.id", "=", "typing_users.id")
        .or_where_column("typing_users.id", "!=", "typing_users.id")
        .where_any(["email", "name"], "alice")
        .or_where_any(["email", "name"], "like", "%alice%")
        .where_all(["email", "name"], "!=", None)
        .or_where_all(["email", "name"], "alice")
        .where_none(["email", "name"], "bob")
        .or_where_none(["email", "name"], "charlie")
        .where_past("created_at")
        .or_where_past("created_at")
        .where_future("created_at")
        .or_where_future("created_at")
        .where_json_contains("preferences->roles", "editor")
        .or_where_json_contains("preferences->roles", "admin")
        .where_json_doesnt_contain("preferences->roles", "banned")
        .or_where_json_doesnt_contain("preferences->roles", "guest")
        .where_json_contains_key("preferences->theme")
        .or_where_json_contains_key("preferences->lang")
        .where_json_doesnt_contain_key("preferences->retired")
        .or_where_json_doesnt_contain_key("preferences->legacy")
        .where_json_length("tags", ">=", 1)
        .or_where_json_length("tags", 0)
        .shared_lock()
        .lock_for_update()
        .dump()
        .dump_raw_sql()
    )
    assert isinstance(extended_query, ModelQueryBuilder)
    compiled_sql, compiled_bindings = extended_query.to_sql()
    raw_sql: str = extended_query.to_raw_sql()
    assert isinstance(compiled_sql, str)
    assert isinstance(compiled_bindings, list)
    assert isinstance(raw_sql, str)

    class_entry_query: ModelQueryBuilder[User] = (
        User.where_column("typing_users.id", "=", "typing_users.id")
        .where_not_exists(
            Post.query().where_column("typing_posts.user_id", "=", "typing_users.id")
        )
        .or_where_exists(
            Post.query().where_column("typing_posts.user_id", "=", "typing_users.id")
        )
        .or_where_not_exists(
            Post.query().where_column("typing_posts.user_id", "=", "typing_users.id")
        )
        .where_all(["email", "name"], "alice")
        .where_none(["email", "name"], "bob")
        .where_future("created_at")
        .where_json_contains("preferences->roles", "editor")
        .where_json_doesnt_contain("preferences->roles", "banned")
        .where_json_doesnt_contain_key("preferences->retired")
        .where_json_length("tags", 1)
        .shared_lock()
    )
    assert isinstance(class_entry_query, ModelQueryBuilder)

    exists_query: ModelQueryBuilder[User] = User.where_exists(
        Post.query().where_column("typing_posts.user_id", "=", "typing_users.id")
    )
    assert isinstance(exists_query, ModelQueryBuilder)

    class_shortcuts: list[ModelQueryBuilder[User]] = [
        User.or_where_column("typing_users.id", "=", "typing_users.id"),
        User.where_not_exists(Post.query()),
        User.or_where_exists(Post.query()),
        User.or_where_not_exists(Post.query()),
        User.where_any(["email", "name"], "alice"),
        User.or_where_any(["email", "name"], "alice"),
        User.where_all(["email", "name"], "alice"),
        User.or_where_all(["email", "name"], "alice"),
        User.where_none(["email", "name"], "bob"),
        User.or_where_none(["email", "name"], "bob"),
        User.where_past("created_at"),
        User.or_where_past("created_at"),
        User.where_future("created_at"),
        User.or_where_future("created_at"),
        User.where_json_contains("preferences->roles", "editor"),
        User.or_where_json_contains("preferences->roles", "editor"),
        User.where_json_doesnt_contain("preferences->roles", "banned"),
        User.or_where_json_doesnt_contain("preferences->roles", "banned"),
        User.where_json_contains_key("preferences->theme"),
        User.or_where_json_contains_key("preferences->theme"),
        User.where_json_doesnt_contain_key("preferences->retired"),
        User.or_where_json_doesnt_contain_key("preferences->retired"),
        User.where_json_length("tags", 1),
        User.or_where_json_length("tags", ">=", 1),
        User.shared_lock(),
        User.lock_for_update(),
    ]
    assert len(class_shortcuts) == 26
    assert issubclass(QueryDumpError, Exception)

    # 5. HasOne and BelongsTo relationship operations on loaded relation
    prof = Profile(id=1, user_id=1, bio="Pythonista")
    user._loaded_relations["profile"] = prof
    assert user.profile.bio == "Pythonista"

    # 6. BelongsToMany operations on loaded relation
    r1 = Role(id=1, name="Editor")
    user._loaded_relations["roles"] = [r1]

    roles_len: int = len(user.roles)
    assert roles_len == 1

    for r in user.roles:
        r_name: str = r.name
        assert r_name == "Editor"


class AuthorNoClassVar(Model):
    __table__ = "typing_authors_nocv"
    __fillable__ = ["name"]  # noqa: RUF012
    id: int
    name: str
    articles = HasMany("ArticleNoClassVar", foreign_key="author_id")


class ArticleNoClassVar(Model):
    __table__ = "typing_articles_nocv"
    __primary_key__ = "id"
    __fillable__ = ["author_id", "title"]  # noqa: RUF012
    __hidden__ = ["internal_note"]  # noqa: RUF012
    strict_events = True
    prevent_lazy_loading = False

    id: int
    author_id: int
    title: str
    internal_note: str
    author = BelongsTo("AuthorNoClassVar", foreign_key="author_id")


class OpenArticleNoClassVar(Model):
    __table__ = "typing_open_articles_nocv"
    __guarded__ = []  # noqa: RUF012
    id: int
    title: str


def test_models_without_classvar_hydrate_and_typecheck() -> None:
    """Models declaring __fillable__/__hidden__/__guarded__ without ClassVar behave identically."""
    assert set(ArticleNoClassVar.__fields__) == {
        "id",
        "author_id",
        "title",
        "internal_note",
    }
    assert ArticleNoClassVar.__fillable__ == ["author_id", "title"]
    assert ArticleNoClassVar.__hidden__ == ["internal_note"]
    assert ArticleNoClassVar.strict_events is True

    # Unlisted field 'internal_note' is ignored during mass assignment
    article = ArticleNoClassVar(
        id=7,
        author_id=1,
        title="No ClassVar Needed",
        internal_note="should-not-mass-assign",
    )
    title: str = article.title
    assert title == "No ClassVar Needed"
    assert article.internal_note is None

    # Undeclared fields still raise MassAssignmentError
    with pytest.raises(MassAssignmentError, match="Unknown attribute"):
        ArticleNoClassVar(unknown_col="nope")

    # __hidden__ excludes secret attributes during serialization and HTTP response encoding
    article.force_fill(internal_note="secret-draft")
    assert "internal_note" not in article.to_dict()
    assert article.to_dict(include_hidden=True)["internal_note"] == "secret-draft"
    assert b"secret-draft" not in json_response(article).content

    # __guarded__ = [] also works without ClassVar
    open_article = OpenArticleNoClassVar(id=1, title="Open")
    assert open_article.title == "Open"


class _DocGroup(Model):
    __table__ = "typing_doc_groups"
    id: int
    name: str
    __guarded__: ClassVar[list[str]] = []


class _DocVersion(Model):
    __table__ = "typing_doc_versions"
    id: int
    version: str
    slug: str
    sort_order: int = 0
    is_active: bool = True
    groups = HasMany(_DocGroup, foreign_key="version_id")
    author = BelongsTo(User, foreign_key="author_id")
    __guarded__: ClassVar[list[str]] = []


class _DocVersionRepository:
    """Repository pattern verifying `self.model: type[Model]` preserves concrete types."""

    model: type[_DocVersion] = _DocVersion

    async def get_doc_version(self, version_id: int) -> _DocVersion:
        version = await self.model.with_("groups").where("id", version_id).first()
        if not version:
            raise LookupError("Version not found")
        return version

    async def get_doc_versions(self) -> list[_DocVersion]:
        return await self.model.with_("groups", "author").order_by("sort_order").get()


class _GenericRepository[M: Model]:
    """Generic repository base preserving concrete Model type parameter across chains."""

    def __init__(self, model: type[M]) -> None:
        self.model: type[M] = model

    async def get_by_id(self, record_id: int) -> M:
        item = await self.model.where("id", record_id).first()
        if item is None:
            raise LookupError("Not found")
        return item

    async def list_ordered(self) -> list[M]:
        return await self.model.order_by("id").get()


def test_repository_query_chaining_type_preservation() -> None:
    """Verify repository methods preserve concrete model return types statically."""
    repo = _DocVersionRepository()
    generic_repo = _GenericRepository(_DocVersion)
    assert repo.model is _DocVersion
    assert generic_repo.model is _DocVersion
