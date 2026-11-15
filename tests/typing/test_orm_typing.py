from __future__ import annotations

from typing import Any, ClassVar

from swiftpy.database.orm.fields import Field
from swiftpy.database.orm.model import Model
from swiftpy.database.orm.relationships import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
)


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
