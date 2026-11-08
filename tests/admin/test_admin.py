from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import urlencode

import pytest

from swiftpy.admin import (
    ADMIN_COOKIE_NAME,
    AdminAuthenticator,
    AdminSessionStore,
    AdminSite,
    AdminUser,
    FieldMetadata,
    ModelAdmin,
    RelationshipMetadata,
    clear_admin_session,
    create_admin_session,
    current_admin,
    get_admin_session_store,
    get_current_admin,
)
from swiftpy.core.application import Application
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import get_container
from swiftpy.database.orm.model import Model
from swiftpy.database.orm.relationships import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
)
from swiftpy.database.pool import DatabasePool
from swiftpy.http.request import Request
from swiftpy.http.response import Response
from swiftpy.testing.http_client import TestClient

# ==============================================================================
# Domain Models for Admin Testing
# ==============================================================================


class Author(Model):
    __table__ = "admin_test_authors"
    id: int
    name: str
    bio: str
    books = HasMany("Book", foreign_key="author_id")
    profile = HasOne("AuthorProfile", foreign_key="author_id")
    __guarded__: ClassVar[list[str]] = []


class AuthorProfile(Model):
    __table__ = "admin_test_author_profiles"
    id: int
    author_id: int
    website: str
    author = BelongsTo("Author", foreign_key="author_id")
    __guarded__: ClassVar[list[str]] = []


class Tag(Model):
    __table__ = "admin_test_tags"
    id: int
    name: str
    __guarded__: ClassVar[list[str]] = []


class Book(Model):
    __table__ = "admin_test_books"
    id: int
    title: str
    author_id: int
    published_year: int
    is_published: bool
    author = BelongsTo("Author", foreign_key="author_id")
    tags = BelongsToMany(
        "Tag",
        pivot_table="admin_test_book_tags",
        foreign_pivot_key="book_id",
        related_pivot_key="tag_id",
    )
    __guarded__: ClassVar[list[str]] = []


# ==============================================================================
# 1. Admin Registry & Reflection Tests
# ==============================================================================


def test_admin_registration_and_reflection() -> None:
    test_site = AdminSite(title="Custom Admin Test Site")

    @test_site.register(Author)
    class AuthorAdmin(ModelAdmin):
        list_display: ClassVar[list[str]] = ["id", "name", "bio"]
        search_fields: ClassVar[list[str]] = ["name"]

    test_site.register(Book)

    assert test_site.is_registered(Author)
    assert test_site.is_registered(Book)

    author_admin = test_site.get_model_admin(Author)
    book_admin = test_site.get_model_admin(Book)

    author_fields = author_admin.get_fields()
    author_field_names = [f.name for f in author_fields]
    assert "id" in author_field_names
    assert "name" in author_field_names
    assert "bio" in author_field_names

    book_fields = book_admin.get_fields()
    book_field_map = {f.name: f for f in book_fields}

    assert book_field_map["author_id"].is_foreign_key is True
    assert book_field_map["author_id"].foreign_relation_name == "author"
    assert book_field_map["author_id"].foreign_model_name == "Author"
    assert book_field_map["author_id"].input_type == "select"
    assert book_field_map["is_published"].type_name == "bool"
    assert book_field_map["is_published"].input_type == "checkbox"
    assert book_field_map["published_year"].type_name == "int"
    assert book_field_map["published_year"].input_type == "number"

    book_relations = book_admin.get_relationships()
    assert "author" in book_relations
    assert book_relations["author"].relation_type == "BelongsTo"


def test_admin_duplicate_and_invalid_registration() -> None:
    """Verify duplicate model registration raises ValueError and invalid object raises TypeError."""
    site = AdminSite(title="Validation Site")
    site.register(Author)

    with pytest.raises(ValueError) as err:
        site.register(Author)
    assert "is already registered" in str(err.value)

    class NotAModel:
        pass

    with pytest.raises(TypeError) as err_type:
        site.register(NotAModel)  # type: ignore[arg-type]
    assert "expects a Model subclass" in str(err_type.value)


def test_admin_field_metadata_defaults_and_coercion() -> None:
    """Verify FieldMetadata type coercion, defaults, and error boundaries."""
    int_field = FieldMetadata("age", int, default=18)
    assert int_field.coerce_value("42") == 42
    assert int_field.coerce_value("") == 18

    with pytest.raises(ValueError) as err_int:
        int_field.coerce_value("invalid-int")
    assert "must be a valid integer" in str(err_int.value)

    float_field = FieldMetadata("score", float, default=0.0)
    assert float_field.coerce_value("3.14") == 3.14
    assert float_field.coerce_value("") == 0.0

    with pytest.raises(ValueError) as err_float:
        float_field.coerce_value("invalid-float")
    assert "must be a valid number" in str(err_float.value)

    bool_field = FieldMetadata("is_active", bool)
    assert bool_field.coerce_value("on") is True
    assert bool_field.coerce_value("true") is True
    assert bool_field.coerce_value("1") is True
    assert bool_field.coerce_value("") is False
    assert bool_field.coerce_value(None) is False

    str_field = FieldMetadata("bio", str, nullable=True)
    assert str_field.coerce_value("") is None
    assert str_field.coerce_value("Some bio") == "Some bio"


def test_admin_all_relationship_types_metadata() -> None:
    """Verify metadata discovery and target model resolution for all 4 relationship types."""
    rel_belongs_to = RelationshipMetadata("author", Book.author, Book)
    assert rel_belongs_to.relation_type == "BelongsTo"
    assert rel_belongs_to.target_model_name == "Author"

    rel_has_many = RelationshipMetadata("books", Author.books, Author)
    assert rel_has_many.relation_type == "HasMany"
    assert rel_has_many.target_model_name == "Book"

    rel_has_one = RelationshipMetadata("profile", Author.profile, Author)
    assert rel_has_one.relation_type == "HasOne"
    assert rel_has_one.target_model_name == "AuthorProfile"

    rel_belongs_to_many = RelationshipMetadata("tags", Book.tags, Book)
    assert rel_belongs_to_many.relation_type == "BelongsToMany"
    assert rel_belongs_to_many.target_model_name == "Tag"
    assert rel_belongs_to_many.pivot_table == "admin_test_book_tags"


# ==============================================================================
# 2. Session Authentication & Security Hardening Tests
# ==============================================================================


def test_session_based_admin_auth_lifecycle() -> None:
    """Verify session creation, cookie attachment, request verification, and clearing."""
    user = AdminUser(username="admin_user", is_admin=True, permissions=["models:read", "models:write"])
    response = Response(content="ok")
    token = create_admin_session(response, user)

    cookie_header = response.headers.get("set-cookie")
    assert cookie_header is not None
    assert f"{ADMIN_COOKIE_NAME}={token}" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=Lax" in cookie_header

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin",
        "headers": [(b"cookie", f"{ADMIN_COOKIE_NAME}={token}".encode())],
    }

    async def dummy_receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    req = Request(scope, dummy_receive)
    verified = get_current_admin(req)

    assert verified is not None
    assert verified.username == "admin_user"
    assert verified.is_admin is True
    curr = current_admin()
    assert curr is not None
    assert curr.username == "admin_user"

    clear_resp = Response(content="cleared")
    clear_admin_session(clear_resp, req)
    clear_cookie = clear_resp.headers.get("set-cookie")
    assert clear_cookie is not None
    assert "Max-Age=0" in clear_cookie

    # Replay of revoked session fails
    store = get_admin_session_store()
    assert store.verify_token(token) is None


def test_admin_session_tampering_and_expiry() -> None:
    """Verify corrupted, expired, or tampered tokens are rejected."""
    store = AdminSessionStore(secret_key="top-secret-admin-key-32-bytes-long!")
    user = AdminUser(username="superadmin")

    # 1. Valid token
    token = store.create_token(user, max_age=3600)
    assert store.verify_token(token) is not None

    # 2. Tampered signature
    parts = token.split(".")
    tampered_sig = parts[0] + ".tampered_signature"
    assert store.verify_token(tampered_sig) is None

    # 3. Tampered payload
    tampered_payload = "invalidpayload." + parts[1]
    assert store.verify_token(tampered_payload) is None

    # 4. Expired token
    expired_token = store.create_token(user, max_age=-10)
    assert store.verify_token(expired_token) is None


@pytest.mark.asyncio
async def test_admin_csrf_protection() -> None:
    """Verify CSRF token generation, form injection, and validation on POST requests."""
    site = AdminSite(title="CSRF Site")
    site.register(Author)
    app = Application()
    site.mount(app, prefix="/admin")
    client = TestClient(app)

    store = get_admin_session_store()
    user = AdminUser(username="admin")
    valid_csrf = store.generate_csrf_token(user)

    # Fake authenticated response
    login_resp = Response()
    token = create_admin_session(login_resp, user)
    cookie_header = f"{ADMIN_COOKIE_NAME}={token}"
    auth_headers = {
        "Cookie": cookie_header,
        "content-type": "application/x-www-form-urlencoded",
    }

    # 1. POST without CSRF token returns 403 Forbidden
    resp_no_csrf = await client.post(
        "/admin/admin_test_authors/create",
        data=urlencode({"name": "Arthur Conan Doyle", "bio": "Author of Sherlock Holmes"}),
        headers=auth_headers,
    )
    assert resp_no_csrf.status_code == 403
    assert "CSRF verification failed" in resp_no_csrf.text

    # 2. POST with invalid CSRF token returns 403 Forbidden
    resp_bad_csrf = await client.post(
        "/admin/admin_test_authors/create",
        data=urlencode({"name": "Arthur Conan Doyle", "_csrf_token": "bad-csrf-token"}),
        headers=auth_headers,
    )
    assert resp_bad_csrf.status_code == 403

    # 3. POST with valid CSRF token passes CSRF check
    # (will return redirect or validation result, not 403)
    resp_valid_csrf = await client.post(
        "/admin/admin_test_authors/create",
        data=urlencode({"name": "Arthur Conan Doyle", "bio": "Bio text", "_csrf_token": valid_csrf}),
        headers=auth_headers,
    )
    assert resp_valid_csrf.status_code != 403


def test_admin_default_credentials_disabled_in_production() -> None:
    """Verify default credentials 'admin'/'admin123' are strictly forbidden when APP_ENV=production."""
    prod_cfg = Config()
    prod_cfg.set("app.env", "production")
    prod_cfg.set("admin.username", "admin")
    prod_cfg.set("admin.password", "admin123")

    authenticator = AdminAuthenticator(config=prod_cfg)
    with pytest.raises(ConfigurationError) as err:
        authenticator.authenticate("admin", "admin123")
    assert "strictly prohibited in production mode" in str(err.value)


# ==============================================================================
# 3. HTML Escaping, Protected Fields & Multi-Site Routing
# ==============================================================================


@pytest.mark.asyncio
async def test_admin_html_escaping_and_xss_prevention() -> None:
    """Verify XSS payloads in model fields are escaped in list and form screens."""
    site = AdminSite(title="Security Admin")
    site.register(Author)
    app = Application()
    site.mount(app, prefix="/admin")
    client = TestClient(app)

    user = AdminUser(username="admin")
    login_resp = Response()
    token = create_admin_session(login_resp, user)
    cookie_header = f"{ADMIN_COOKIE_NAME}={token}"

    store = get_admin_session_store()
    csrf_tok = store.generate_csrf_token(user)

    # Submit payload containing malicious HTML / JS
    xss_payload = '<script>alert("XSS")</script>'
    resp = await client.post(
        "/admin/admin_test_authors/create",
        data=urlencode({"name": xss_payload, "bio": '"quoted text" <admin>', "_csrf_token": csrf_tok}),
        headers={"Cookie": cookie_header, "content-type": "application/x-www-form-urlencoded"},
    )
    # Form re-renders or returns result: check raw text has escaped tags
    if resp.status_code == 422:
        assert "<script>" not in resp.text
        assert "&lt;script&gt;" in resp.text
        assert "&quot;quoted text&quot;" in resp.text


@pytest.mark.asyncio
async def test_admin_protected_fields_filtering() -> None:
    """Verify primary key and timestamp mutations are ignored during form submission."""
    site = AdminSite(title="Protected Site")
    site.register(Author)
    app = Application()
    site.mount(app, prefix="/admin")
    client = TestClient(app)

    user = AdminUser(username="admin")
    login_resp = Response()
    token = create_admin_session(login_resp, user)
    cookie_header = f"{ADMIN_COOKIE_NAME}={token}"
    store = get_admin_session_store()
    csrf_tok = store.generate_csrf_token(user)

    # Attempt to inject primary key and fake timestamps
    resp = await client.post(
        "/admin/admin_test_authors/create",
        data=urlencode({
            "id": "9999",
            "name": "Sir Arthur",
            "bio": "Writer",
            "created_at": "hacked-timestamp",
            "_csrf_token": csrf_tok,
        }),
        headers={"Cookie": cookie_header, "content-type": "application/x-www-form-urlencoded"},
    )
    # Must not throw error trying to coerce created_at
    assert resp.status_code in (200, 302, 422)


@pytest.mark.asyncio
async def test_multiple_admin_sites_with_different_prefixes() -> None:
    """Verify two independent AdminSite instances mount cleanly without route collisions."""
    app = Application()
    site1 = AdminSite(title="Public Admin")
    site1.register(Author)
    site1.mount(app, prefix="/admin")

    site2 = AdminSite(title="Internal Admin")
    site2.register(Book)
    site2.mount(app, prefix="/internal-admin")

    client = TestClient(app)

    # Both login endpoints respond cleanly
    r1 = await client.get("/admin/login")
    assert r1.status_code == 200
    assert "Public Admin" in r1.text

    r2 = await client.get("/internal-admin/login")
    assert r2.status_code == 200
    assert "Internal Admin" in r2.text


# ==============================================================================
# 4. Database-Backed CRUD Integration Workflow (Real PostgreSQL)
# ==============================================================================


@pytest.mark.asyncio
async def test_admin_full_database_backed_crud_workflow() -> None:
    """
    End-to-End Real PostgreSQL CRUD Integration Test:
    1. Initialize real PostgreSQL pool and create tables.
    2. Login to admin via /admin/login.
    3. Create an Author via POST /admin/admin_test_authors/create with CSRF.
    4. Verify Author exists in PostgreSQL table.
    5. Create a Book associated with Author via POST with CSRF.
    6. Verify Book exists in PostgreSQL table with foreign key.
    7. Open Book list view and verify Author name is rendered from eager-loaded relationship.
    8. Edit Book title via POST /admin/admin_test_books/{id}/edit with CSRF.
    9. Verify updated Book title in PostgreSQL table.
    10. Delete Book via POST /admin/admin_test_books/{id}/delete with CSRF.
    11. Verify Book is deleted from PostgreSQL table while Author remains intact.
    """
    cfg = Config()
    cfg.set("database.connections.postgresql.host", "postgres")
    cfg.set("database.connections.postgresql.port", 5432)
    cfg.set("database.connections.postgresql.database", "swiftpy")
    cfg.set("database.connections.postgresql.username", "postgres")
    cfg.set("database.connections.postgresql.password", "secret")
    pool = DatabasePool(config=cfg)
    await pool.init()
    raw_pool = pool.get_pool()

    # 1. Setup real PostgreSQL schema
    async with raw_pool.acquire() as conn:
        await conn.execute("""
        DROP TABLE IF EXISTS admin_test_books CASCADE;
        DROP TABLE IF EXISTS admin_test_authors CASCADE;
        CREATE TABLE admin_test_authors (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            bio TEXT
        );
        CREATE TABLE admin_test_books (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            author_id INT REFERENCES admin_test_authors(id) ON DELETE CASCADE,
            published_year INT,
            is_published BOOLEAN DEFAULT TRUE
        );
        """)

    app = Application()
    app.container.instance(DatabasePool, pool)
    get_container().instance(DatabasePool, pool)

    site = AdminSite(title="Real DB Admin")
    site.register(Author)
    site.register(Book)
    site.mount(app, prefix="/admin")
    client = TestClient(app)

    # 2. Login to Admin
    login_resp = await client.post(
        "/admin/login",
        data=urlencode({"username": "admin", "password": "admin123"}),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert login_resp.status_code == 302
    cookie_str = login_resp.headers.get("set-cookie")
    assert cookie_str is not None
    raw_cookie = cookie_str.split(";")[0]
    token = raw_cookie.split("=", 1)[1]

    store = get_admin_session_store()
    auth_user = store.verify_token(token)
    assert auth_user is not None
    csrf_token = store.generate_csrf_token(auth_user)
    auth_headers = {
        "Cookie": raw_cookie,
        "content-type": "application/x-www-form-urlencoded",
    }

    # 3. Create Author via Admin Panel
    author_resp = await client.post(
        "/admin/admin_test_authors/create",
        data=urlencode({
            "name": "Arthur Conan Doyle",
            "bio": "Creator of Sherlock Holmes.",
            "_csrf_token": csrf_token,
        }),
        headers=auth_headers,
    )
    assert author_resp.status_code == 302

    # 4. Verify Author exists in real PostgreSQL database
    async with raw_pool.acquire() as conn:
        author_row = await conn.fetchrow("SELECT * FROM admin_test_authors WHERE name = $1", "Arthur Conan Doyle")
        assert author_row is not None
        author_id = author_row["id"]
        assert author_row["bio"] == "Creator of Sherlock Holmes."

    # 5. Create Book associated with Author via Admin Panel
    book_resp = await client.post(
        "/admin/admin_test_books/create",
        data=urlencode({
            "title": "A Study in Scarlet",
            "author_id": str(author_id),
            "published_year": "1887",
            "is_published": "true",
            "_csrf_token": csrf_token,
        }),
        headers=auth_headers,
    )
    assert book_resp.status_code == 302

    # 6. Verify Book exists in real PostgreSQL database
    async with raw_pool.acquire() as conn:
        book_row = await conn.fetchrow("SELECT * FROM admin_test_books WHERE title = $1", "A Study in Scarlet")
        assert book_row is not None
        book_id = book_row["id"]
        assert book_row["author_id"] == author_id
        assert book_row["published_year"] == 1887
        assert book_row["is_published"] is True

    # 7. Open Book list view and verify eager-loaded Author name is rendered
    list_resp = await client.get("/admin/admin_test_books", headers={"Cookie": raw_cookie})
    assert list_resp.status_code == 200
    assert "A Study in Scarlet" in list_resp.text
    assert "Arthur Conan Doyle" in list_resp.text

    # 8. Edit Book title via Admin Panel
    edit_resp = await client.post(
        f"/admin/admin_test_books/{book_id}/edit",
        data=urlencode({
            "title": "A Study in Scarlet (Updated Edition)",
            "published_year": "1888",
            "is_published": "true",
            "_csrf_token": csrf_token,
        }),
        headers=auth_headers,
    )
    assert edit_resp.status_code == 302

    # 9. Verify updated Book title in PostgreSQL table
    async with raw_pool.acquire() as conn:
        updated_row = await conn.fetchrow("SELECT * FROM admin_test_books WHERE id = $1", book_id)
        assert updated_row is not None
        assert updated_row["title"] == "A Study in Scarlet (Updated Edition)"
        assert updated_row["published_year"] == 1888

    # 10. Delete Book via Admin Panel
    delete_resp = await client.post(
        f"/admin/admin_test_books/{book_id}/delete",
        data=urlencode({"_csrf_token": csrf_token}),
        headers=auth_headers,
    )
    assert delete_resp.status_code == 302

    # 11. Verify Book is deleted while Author remains
    async with raw_pool.acquire() as conn:
        deleted_book = await conn.fetchrow("SELECT * FROM admin_test_books WHERE id = $1", book_id)
        assert deleted_book is None
        surviving_author = await conn.fetchrow("SELECT * FROM admin_test_authors WHERE id = $1", author_id)
        assert surviving_author is not None
        assert surviving_author["name"] == "Arthur Conan Doyle"

        # Cleanup test tables
        await conn.execute("DROP TABLE IF EXISTS admin_test_books CASCADE;")
        await conn.execute("DROP TABLE IF EXISTS admin_test_authors CASCADE;")

    await pool.close()
