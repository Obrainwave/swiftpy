from __future__ import annotations

from typing import Any, assert_type

from swiftpy.database.factories import Factory, FactoryBatch, Fake, Sequence
from swiftpy.database.orm import BelongsTo, BelongsToMany, HasMany, HasOne, Model


class TypingRole(Model):
    __table__ = "typing_fac_roles"
    __fillable__ = ["name"]

    id: int
    name: str


class TypingProfile(Model):
    __table__ = "typing_fac_profiles"
    __fillable__ = ["user_id", "bio"]

    id: int
    user_id: int
    bio: str


class TypingPost(Model):
    __table__ = "typing_fac_posts"
    __fillable__ = ["user_id", "title"]

    id: int
    user_id: int
    title: str

    user = BelongsTo("TypingFacUser", foreign_key="user_id")


class TypingFacUser(Model):
    __table__ = "typing_fac_users"
    __fillable__ = ["name", "email", "is_admin"]

    id: int
    name: str
    email: str
    is_admin: bool = False

    posts = HasMany(TypingPost, foreign_key="user_id")
    profile = HasOne(TypingProfile, foreign_key="user_id")
    roles = BelongsToMany(
        TypingRole,
        pivot_table="typing_fac_role_user",
        foreign_pivot_key="user_id",
        related_pivot_key="role_id",
    )


class TypingFacUserFactory(Factory[TypingFacUser]):
    model = TypingFacUser

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.fake.name(),
            "email": self.fake.unique_email(),
            "is_admin": False,
        }

    def admin(self) -> TypingFacUserFactory:
        return self.state({"is_admin": True})


class TypingFacPostFactory(Factory[TypingPost]):
    model = TypingPost

    def definition(self) -> dict[str, Any]:
        return {
            "title": self.fake.sentence(3),
        }

    def untitled(self) -> TypingFacPostFactory:
        return self.state({"title": "Untitled"})


def test_factory_static_typing_in_memory() -> None:
    """Verify Model.factory(), Factory[T], FactoryBatch[T], subclass chains, Sequence, and relation helpers preserve types."""
    # 1. Model.factory() -> Factory[T] vs Model.factory(n) / .count(n) -> FactoryBatch[T]
    model_factory = assert_type(TypingFacUser.factory(), Factory[TypingFacUser])
    made_one = assert_type(model_factory.make(), TypingFacUser)
    assert isinstance(made_one, TypingFacUser)

    batch_direct = assert_type(TypingFacUser.factory(5), FactoryBatch[TypingFacUser])
    batch_counted = assert_type(TypingFacUser.factory().count(5), FactoryBatch[TypingFacUser])
    made_five = assert_type(batch_counted.make(), list[TypingFacUser])
    made_five_direct = assert_type(batch_direct.make(), list[TypingFacUser])
    assert len(made_five) == 5 and all(isinstance(u, TypingFacUser) for u in made_five)
    assert len(made_five_direct) == 5

    # 2. Custom subclass method chaining + relationship builders preserve concrete factory type
    def make_seq_email(index: int) -> dict[str, Any]:
        return {"email": f"typed{index}@example.com"}

    seq_obj: Sequence = assert_type(
        Sequence(lambda idx: {"email": f"lambda{idx}@example.com"}),
        Sequence,
    )
    seq_val = seq_obj(0)
    assert seq_val == {"email": "lambda0@example.com"}

    custom_chain = assert_type(
        TypingFacUserFactory()
        .seed(7)
        .admin()
        .state({"name": "Ada"})
        .sequence(make_seq_email)
        .has(TypingFacPostFactory, 2)
        .has_attached(TypingRole(id=1, name="admin"), {"active": True})
        .has_posts(1)
        .has_profile(1, bio="Typed bio"),
        TypingFacUserFactory,
    )
    assert_type(custom_chain.fake, Fake)
    built_custom = assert_type(custom_chain.make(id=1), TypingFacUser)
    assert built_custom.is_admin is True
    assert built_custom.name == "Ada"
    assert built_custom.email == "typed0@example.com"
    assert len(built_custom.posts) == 3

    # 3. Dynamic .has_<relation>() and .for_() on base Factory[T] preserve Factory[T] (never degrade to Any)
    dynamic_chain = assert_type(
        TypingFacUser.factory()
        .has_posts(2)
        .has_profile(1, bio="Dynamic")
        .state({"name": "Grace"}),
        Factory[TypingFacUser],
    )
    built_dynamic = assert_type(dynamic_chain.make(id=2), TypingFacUser)
    assert built_dynamic.name == "Grace"
    assert len(built_dynamic.posts) == 2

    # 4. BelongsTo .for_() / .for_<rel>() preserve concrete factory & batch return types
    owner = TypingFacUser(id=10, name="Owner", email="owner@example.com")
    post_factory = assert_type(
        TypingFacPostFactory().untitled().for_(owner),
        TypingFacPostFactory,
    )
    single_post = assert_type(post_factory.make(), TypingPost)
    assert single_post.user_id == 10
    assert single_post.title == "Untitled"

    posts_batch = assert_type(
        TypingPost.factory().count(3).for_user(owner),
        FactoryBatch[TypingPost],
    )
    made_posts = assert_type(posts_batch.make(), list[TypingPost])
    assert len(made_posts) == 3
    assert all(p.user_id == 10 for p in made_posts)


async def _verify_factory_async_create_types() -> None:
    """Compile-time static contract check for await .create() on Factory[T] and FactoryBatch[T]."""
    created_single = assert_type(
        await TypingFacUser.factory().has_posts(1).create(),
        TypingFacUser,
    )
    created_batch = assert_type(
        await TypingFacUser.factory().count(4).has(TypingFacPostFactory, 2).create(),
        list[TypingFacUser],
    )
    created_subclass = assert_type(
        await TypingFacUserFactory().admin().create(),
        TypingFacUser,
    )
    created_for = assert_type(
        await TypingPost.factory().count(2).for_(created_single).create(),
        list[TypingPost],
    )
    assert created_single.id >= 0
    assert len(created_batch) == 4
    assert created_subclass.is_admin is True
    assert len(created_for) == 2
