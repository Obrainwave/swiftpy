from __future__ import annotations

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
    LazyLoadingDisabledError,
)
from swiftpy.database.pool import DatabasePool


class HardenedAuthor(Model):
    __table__ = "test_hardened_authors"
    id: int
    name: str
    books = HasMany("HardenedBook", foreign_key="author_id")
    __guarded__: ClassVar[list[str]] = []


class HardenedBook(Model):
    __table__ = "test_hardened_books"
    id: int
    author_id: int
    title: str
    author = BelongsTo("HardenedAuthor", foreign_key="author_id")
    reviews = HasMany("HardenedReview", foreign_key="book_id")
    tags = BelongsToMany(
        "HardenedTag",
        pivot_table="test_hardened_book_tags",
        foreign_pivot_key="book_id",
        related_pivot_key="tag_id",
    )
    __guarded__: ClassVar[list[str]] = []


class SubAuthor(HardenedAuthor):
    __table__ = "test_hardened_authors"


class HardenedReview(Model):
    __table__ = "test_hardened_reviews"
    id: int
    book_id: int
    body: str
    __guarded__: ClassVar[list[str]] = []


class HardenedTag(Model):
    __table__ = "test_hardened_tags"
    id: int
    name: str
    __guarded__: ClassVar[list[str]] = []


@pytest.fixture
async def setup_hardened_schema(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS test_hardened_book_tags, test_hardened_reviews, test_hardened_books, test_hardened_tags, test_hardened_authors CASCADE;
            CREATE TABLE test_hardened_authors (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE test_hardened_books (
                id SERIAL PRIMARY KEY,
                author_id INTEGER REFERENCES test_hardened_authors(id) ON DELETE CASCADE,
                title TEXT NOT NULL
            );
            CREATE TABLE test_hardened_reviews (
                id SERIAL PRIMARY KEY,
                book_id INTEGER REFERENCES test_hardened_books(id) ON DELETE CASCADE,
                body TEXT NOT NULL
            );
            CREATE TABLE test_hardened_tags (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE test_hardened_book_tags (
                book_id INTEGER REFERENCES test_hardened_books(id) ON DELETE CASCADE,
                tag_id INTEGER REFERENCES test_hardened_tags(id) ON DELETE CASCADE
            );
            """
        )


async def test_nested_eager_loading_two_and_three_levels(
    setup_hardened_schema: Any,
) -> None:
    author = await HardenedAuthor.create(name="Tolkien")
    book1 = await HardenedBook.create(author_id=author.id, title="Fellowship")
    book2 = await HardenedBook.create(author_id=author.id, title="Towers")

    await HardenedReview.create(book_id=book1.id, body="Masterpiece")
    await HardenedReview.create(book_id=book1.id, body="Epic")
    await HardenedReview.create(book_id=book2.id, body="Thrilling")

    # Nested eager loading with_("books.reviews")
    authors = await HardenedAuthor.with_("books.reviews").get()
    assert len(authors) == 1
    loaded_author = authors[0]

    # Both levels loaded into memory
    assert len(loaded_author.books) == 2
    b1 = next(b for b in loaded_author.books if b.title == "Fellowship")
    assert len(b1.reviews) == 2
    assert {r.body for r in b1.reviews} == {"Masterpiece", "Epic"}

    b2 = next(b for b in loaded_author.books if b.title == "Towers")
    assert len(b2.reviews) == 1
    assert b2.reviews[0].body == "Thrilling"


async def test_belongs_to_many_attach_detach_sync_and_create(
    setup_hardened_schema: Any,
) -> None:
    book = await HardenedBook.create(title="The Hobbit")
    tag1 = await HardenedTag.create(name="Fantasy")
    tag2 = await HardenedTag.create(name="Adventure")
    tag3 = await HardenedTag.create(name="Classic")

    # attach
    await book.tags.attach(tag1)
    await book.tags.attach(tag2.id)

    loaded = await book.tags.get()
    assert len(loaded) == 2
    assert {t.name for t in loaded} == {"Fantasy", "Adventure"}

    # detach single
    await book.tags.detach(tag1.id)
    loaded2 = await book.tags.get()
    assert len(loaded2) == 1
    assert loaded2[0].name == "Adventure"

    # sync
    await book.tags.sync([tag2, tag3])
    loaded3 = await book.tags.get()
    assert len(loaded3) == 2
    assert {t.name for t in loaded3} == {"Adventure", "Classic"}

    # create via BelongsToMany raises NotImplementedError to prevent unlinked rows
    with pytest.raises(NotImplementedError, match="attach"):
        await book.tags.create(name="Bestseller")


def test_relationship_query_builder_copy_safety() -> None:
    author = HardenedAuthor(id=1, name="George")
    builder = author.books

    # copy.copy inspects __copy__ via getattr; must raise AttributeError instead of RelationNotLoadedError
    with pytest.raises(AttributeError):
        _ = builder.__copy__

    with pytest.raises(AttributeError):
        _ = builder._private_foo


def test_strict_mode_prevents_lazy_loading() -> None:
    author = HardenedAuthor(id=1, name="Asimov")

    # When prevent_lazy_loading is True, accessing unloaded relation raises LazyLoadingDisabledError
    HardenedAuthor.prevent_lazy_loading = True
    try:
        with pytest.raises(LazyLoadingDisabledError, match="Lazy loading is disabled"):
            _ = author.books
    finally:
        HardenedAuthor.prevent_lazy_loading = False

    # When False, accessing returns RelationshipQueryBuilder
    builder = author.books
    assert builder is not None


def test_symmetric_equality_and_hash() -> None:
    author = HardenedAuthor(id=10, name="Arthur")
    author_same = HardenedAuthor(id=10, name="Arthur")
    sub_author = SubAuthor(id=10, name="Arthur")

    assert author == author_same
    assert hash(author) == hash(author_same)

    # Different class types must not be equal (symmetric check)
    assert author != sub_author
    assert sub_author != author


def test_string_relationship_target_requires_import_before_resolution() -> None:
    """String relationship targets resolve via _MODEL_REGISTRY once imported."""
    snap = snapshot_model_registry()
    try:

        class OrderOwner(Model):
            __table__ = "test_order_owners"
            __fillable__ = ["name"]  # noqa: RUF012
            id: int
            name: str
            items = HasMany("LateImportedItem", foreign_key="owner_id")

        owner = OrderOwner(id=1, name="Alice")
        with pytest.raises(RuntimeError, match="LateImportedItem.*has not been registered"):
            _ = owner.items

        class LateImportedItem(Model):
            __table__ = "test_late_imported_items"
            __fillable__ = ["owner_id", "label"]  # noqa: RUF012
            id: int
            owner_id: int
            label: str

        builder = owner.items
        assert builder is not None
    finally:
        restore_model_registry(snap)
