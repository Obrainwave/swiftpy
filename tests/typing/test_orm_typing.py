from __future__ import annotations

import tomllib
import zipfile
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, assert_type

import pytest

import swiftpy
from swiftpy.database import (
    Factory,
    FactoryBatch,
    Paginator,
    Query,
    QueryDumpError,
    QueryError,
    SimplePaginator,
)
from swiftpy.database.orm.fields import Field
from swiftpy.database.orm.model import MassAssignmentError, Model, ModelQueryBuilder
from swiftpy.database.orm.relationships import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
)
from swiftpy.http.response import json_response
from swiftpy.schemas import BaseSchema


class Role(Model):
    __table__ = "typing_roles"
    id: int
    name: str
    __guarded__ = []


class Profile(Model):
    __table__ = "typing_profiles"
    id: int
    user_id: int
    bio: str
    user = BelongsTo("User", foreign_key="user_id")
    __guarded__ = []


class Post(Model):
    __table__ = "typing_posts"
    id: int
    user_id: int
    title: str
    published: bool = True
    user = BelongsTo("User", foreign_key="user_id")
    __guarded__ = []


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
    __hidden__ = ("is_admin",)
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
    __fillable__ = ["name"]
    id: int
    name: str
    articles = HasMany("ArticleNoClassVar", foreign_key="author_id")


class ArticleNoClassVar(Model):
    __table__ = "typing_articles_nocv"
    __primary_key__ = "id"
    __fillable__ = ["author_id", "title"]
    __hidden__ = ["internal_note"]
    strict_events = True
    prevent_lazy_loading = False

    id: int
    author_id: int
    title: str
    internal_note: str
    author = BelongsTo("AuthorNoClassVar", foreign_key="author_id")


class OpenArticleNoClassVar(Model):
    __table__ = "typing_open_articles_nocv"
    __guarded__ = []
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
    __guarded__ = []


class _DocVersion(Model):
    __table__ = "typing_doc_versions"
    id: int
    version: str
    slug: str
    sort_order: int = 0
    is_active: bool = True
    groups = HasMany(_DocGroup, foreign_key="version_id")
    author = BelongsTo(User, foreign_key="author_id")
    __guarded__ = []


class _DocVersionCreateSchema(BaseSchema):
    version: str
    slug: str
    sort_order: int = 0


class _DocGroupCreateSchema(BaseSchema):
    name: str


class _DocVersionRepository:
    """Repository pattern verifying plain `model = _DocVersion` preserves concrete types."""

    model = _DocVersion

    async def get_doc_version(self, version_id: int) -> _DocVersion:
        version = await self.model.with_("groups").where("id", version_id).first()
        if not version:
            raise LookupError("Version not found")
        return version

    async def get_doc_versions(self) -> list[_DocVersion]:
        return await self.model.with_("groups", "author").order_by("sort_order").get()

    async def create_doc_version(self, data: _DocVersionCreateSchema) -> _DocVersion:
        return await self.model.create(data)

    async def update_doc_version(
        self, version_id: int, data: _DocVersionCreateSchema
    ) -> _DocVersion:
        version = await self.model.find_or_fail(version_id)
        return await version.update(data)


class _InstanceDocVersionRepository:
    """Repository assigning `self.model = _DocVersion` in `__init__`."""

    def __init__(self) -> None:
        self.model = _DocVersion

    async def get_all_ordered(self) -> list[_DocVersion]:
        return await self.model.order_by("sort_order").get()

    async def first_active(self) -> _DocVersion:
        return await self.model.where("is_active", True).first_or_fail()


async def _verify_direct_and_relationship_inference(v: _DocVersion) -> None:
    """Static type-checker contract for direct Model, chain, and relation inference."""
    versions: list[_DocVersion] = assert_type(
        await _DocVersion.with_("groups", "author").order_by("sort_order").get(),
        list[_DocVersion],
    )
    created: _DocVersion = assert_type(
        await _DocVersion.create({"version": "1.0", "slug": "v1"}),
        _DocVersion,
    )
    first: _DocVersion | None = assert_type(
        await _DocVersion.where("id", 1).first(),
        _DocVersion | None,
    )
    found: _DocVersion | None = assert_type(
        await _DocVersion.find(1),
        _DocVersion | None,
    )
    found_req: _DocVersion = assert_type(
        await _DocVersion.find_or_fail(1),
        _DocVersion,
    )
    first_req: _DocVersion = assert_type(
        await _DocVersion.where("is_active", True).first_or_fail(),
        _DocVersion,
    )
    updated: _DocVersion = assert_type(
        await created.update({"slug": "v1-1"}),
        _DocVersion,
    )
    refreshed: _DocVersion = assert_type(
        await updated.refresh(),
        _DocVersion,
    )
    paged: Paginator[_DocVersion] = assert_type(
        await _DocVersion.with_("groups").paginate(per_page=10, page=1),
        Paginator[_DocVersion],
    )
    simple_paged: SimplePaginator[_DocVersion] = assert_type(
        await _DocVersion.order_by("id").simple_paginate(per_page=10, page=1),
        SimplePaginator[_DocVersion],
    )

    typed_attrs: dict[str, str] = {"slug": "v1"}
    typed_vals: dict[str, str] = {"version": "1.0"}
    ensured: _DocVersion = assert_type(
        await _DocVersion.first_or_create(typed_attrs, typed_vals),
        _DocVersion,
    )
    upserted: _DocVersion = assert_type(
        await _DocVersion.update_or_create(typed_attrs, typed_vals),
        _DocVersion,
    )

    id_list: list[int] = [1, 2, 3]
    in_filtered: list[_DocVersion] = assert_type(
        await _DocVersion.where_in("id", id_list).get(),
        list[_DocVersion],
    )
    subquery_filtered: list[_DocVersion] = assert_type(
        await _DocVersion.where_in(
            "id", _DocGroup.select("id").where("name", "Core")
        ).get(),
        list[_DocVersion],
    )

    # Typed relationship awaiting and full query chaining without Any degradation
    awaited_groups: list[_DocGroup] = assert_type(await v.groups, list[_DocGroup])
    chained_groups: list[_DocGroup] = assert_type(
        await v.groups.where_in("id", id_list).latest("id").get(),
        list[_DocGroup],
    )
    first_group: _DocGroup | None = assert_type(
        await v.groups.where("name", "Core").first(),
        _DocGroup | None,
    )
    req_group: _DocGroup = assert_type(
        await v.groups.where("name", "Core").first_or_fail(),
        _DocGroup,
    )
    paged_groups: Paginator[_DocGroup] = assert_type(
        await v.groups.order_by("id").paginate(),
        Paginator[_DocGroup],
    )
    group_from_schema: _DocGroup = assert_type(
        await v.groups.create(_DocGroupCreateSchema(name="Guides")),
        _DocGroup,
    )
    lazy_group_builder: ModelQueryBuilder[_DocGroup] = assert_type(
        _DocVersion.groups.build_lazy_query(v),
        ModelQueryBuilder[_DocGroup],
    )

    awaited_author: User | None = assert_type(await v.author, User | None)
    filtered_author: User | None = assert_type(
        await v.author.where_not_null("email"),
        User | None,
    )
    req_author: User = assert_type(await v.author.first_or_fail(), User)

    # Model Factory static typing contracts (single -> T, count() -> list[T])
    user_factory: Factory[User] = assert_type(User.factory(), Factory[User])
    made_user: User = assert_type(user_factory.make(), User)
    created_user: User = assert_type(await User.factory().create(), User)
    user_batch: FactoryBatch[User] = assert_type(
        User.factory().count(50),
        FactoryBatch[User],
    )
    made_users: list[User] = assert_type(user_batch.make(), list[User])
    created_users_with_posts: list[User] = assert_type(
        await User.factory().count(50).has_posts(1).create(),
        list[User],
    )
    created_counted: list[User] = assert_type(
        await User.factory(10).create(),
        list[User],
    )
    post_for_user: Post = assert_type(
        await Post.factory().for_(created_user).create(),
        Post,
    )
    users_with_roles: list[User] = assert_type(
        await User.factory().count(3).has(Role, 2).create(),
        list[User],
    )

    repo = _DocVersionRepository()
    assert_type(await repo.get_doc_version(1), _DocVersion)
    assert_type(await repo.get_doc_versions(), list[_DocVersion])

    assert len(versions) >= 0
    assert first is None or isinstance(first, _DocVersion)
    assert found is None or isinstance(found, _DocVersion)
    assert found_req is refreshed and first_req is ensured and upserted is found_req
    assert in_filtered == subquery_filtered
    assert paged.total >= 0 and simple_paged.per_page == 10
    assert awaited_groups == chained_groups
    assert first_group is None or first_group == req_group
    assert paged_groups.total >= 0 and group_from_schema.name == "Guides"
    assert lazy_group_builder is not None
    assert awaited_author == filtered_author
    assert req_author.email != ""
    assert made_user.email != ""
    assert len(made_users) == 50
    assert len(created_users_with_posts) == 50
    assert len(created_counted) == 10
    assert post_for_user.user_id == created_user.id
    assert len(users_with_roles) == 3


def test_repository_query_chaining_type_preservation() -> None:
    """Verify repository methods preserve concrete model return types statically."""
    repo = _DocVersionRepository()
    instance_repo = _InstanceDocVersionRepository()
    assert repo.model is _DocVersion
    assert instance_repo.model is _DocVersion

    version = _DocVersion(id=1, version="0.3.7", slug="v0-3-7")
    lazy_groups = _DocVersion.groups.build_lazy_query(version)
    sql, bindings = (
        lazy_groups.where_in("id", [10, 20])
        .latest("id")
        .to_sql()
    )
    assert '"typing_doc_groups"' in sql
    assert bindings == [1, 10, 20]

    sub_sql, sub_bindings = (
        _DocVersion.where_in("id", _DocGroup.select("id").where("name", "Core"))
        .to_sql()
    )
    assert " IN (SELECT " in sub_sql
    assert sub_bindings == ["Core"]

    where_sub_sql, where_sub_bindings = (
        _DocVersion.where("id", "IN", _DocGroup.select("id").where("name", "Api"))
        .to_sql()
    )
    assert " IN (SELECT " in where_sub_sql
    assert where_sub_bindings == ["Api"]

    # Non-string Sequence accepted; str/bytes/memoryview rejected by where_in
    typed_seq: Sequence[int] = (1, 2)
    seq_sql, seq_bindings = Query.table("typing_doc_versions").where_in("id", typed_seq).to_sql()
    assert " IN ($1, $2)" in seq_sql
    assert seq_bindings == [1, 2]

    for bad_seq in ("12", b"12", memoryview(b"12")):
        with pytest.raises(QueryError, match="requires a list, tuple, or QueryBuilder subquery"):
            Query.table("typing_doc_versions").where("id", "IN", bad_seq)

    # Read-only Mapping accepted in _compile_update_sets
    sets, update_bindings = Query.table("typing_doc_versions")._compile_update_sets(
        MappingProxyType({"slug": "proxy-slug"})
    )
    assert sets == ['"slug" = $1']
    assert update_bindings == ["proxy-slug"]


def test_py_typed_marker_present_in_package_and_wheel_config() -> None:
    """Ensure PEP 561 py.typed marker exists in the swiftpy package and wheel packaging config."""
    pkg_dir = Path(swiftpy.__file__).resolve().parent
    py_typed = pkg_dir / "py.typed"
    assert py_typed.is_file(), "swiftpy/py.typed PEP 561 marker must exist in package"

    repo_root = pkg_dir.parent
    pyproject_path = repo_root / "pyproject.toml"
    if pyproject_path.is_file():
        cfg = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        wheel_pkgs = (
            cfg.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("targets", {})
            .get("wheel", {})
            .get("packages", [])
        )
        assert "swiftpy" in wheel_pkgs

    dist_dir = repo_root / "dist"
    if dist_dir.is_dir():
        for whl in dist_dir.glob("swiftpy_framework-*.whl"):
            with zipfile.ZipFile(whl) as zf:
                assert "swiftpy/py.typed" in zf.namelist()
