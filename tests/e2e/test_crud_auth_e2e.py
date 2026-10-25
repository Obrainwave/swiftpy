from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

import bcrypt
import pytest

from swiftpy.auth.jwt_issuer import JWTError, JWTIssuer
from swiftpy.core.application import Application
from swiftpy.core.exceptions import ForbiddenError, NotFoundError, UnauthorizedError
from swiftpy.database.orm import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient
from swiftpy.validation.form_request import FormRequest

_TABLE_SUFFIX = uuid.uuid4().hex[:8]
USERS_TABLE = f"e2e_users_{_TABLE_SUFFIX}"
ARTICLES_TABLE = f"e2e_articles_{_TABLE_SUFFIX}"


class E2EUser(Model):
    __table__ = USERS_TABLE
    id: int
    name: str
    email: str
    password_hash: str


class E2EArticle(Model):
    __table__ = ARTICLES_TABLE
    id: int
    title: str
    content: str
    author_id: int


class RegisterForm(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "name": "required|str|min:3",
        "email": f"required|email|unique:{USERS_TABLE},email",
        "password": "required|str|min:6",
    }


class LoginForm(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "email": "required|email",
        "password": "required|str",
    }


class CreateArticleForm(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "title": "required|str|min:3",
        "content": "required|str",
    }


class UpdateArticleForm(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "title": "required|str|min:3",
        "content": "required|str",
    }


@pytest.fixture
async def setup_e2e_tables(
    ensure_db_pool: DatabasePool,
) -> AsyncGenerator[None, None]:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS "{USERS_TABLE}" (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(150) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL
            );
            CREATE TABLE IF NOT EXISTS "{ARTICLES_TABLE}" (
                id SERIAL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                content TEXT NOT NULL,
                author_id INT NOT NULL REFERENCES "{USERS_TABLE}"(id) ON DELETE CASCADE
            );
            """
        )
    try:
        yield
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{ARTICLES_TABLE}";')
            await conn.execute(f'DROP TABLE IF EXISTS "{USERS_TABLE}";')


@pytest.mark.asyncio
async def test_complete_crud_and_auth_workflow(
    ensure_db_pool: DatabasePool,
    setup_e2e_tables: None,
) -> None:
    """
    End-to-end verification of an entire CRUD API built using only SwiftPY:
    - User registration with validation and unique DB checks
    - Password hashing and verification
    - JWT token issuing and authentication
    - Authenticated CRUD on ORM models
    - Automatic error handling and status code mapping
    """
    app = Application()
    app.container.instance(DatabasePool, ensure_db_pool)

    issuer = JWTIssuer(secret="e2e-jwt-super-secret-key-32-bytes!!")
    client = TestClient(app)

    # Helper to authenticate requests in routes
    def authenticate(request: Request) -> int:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise UnauthorizedError("Missing or invalid Authorization header")
        token = auth[7:]
        try:
            payload = issuer.decode_token(token)
        except JWTError:
            raise UnauthorizedError("Invalid token") from None
        return int(payload["sub"])

    # 1. Register Route
    @app.post("/auth/register")
    async def register(form: RegisterForm) -> Response:
        data = form.validated()
        pwd_hash = bcrypt.hashpw(
            data["password"].encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

        user = await E2EUser.create(
            name=data["name"],
            email=data["email"],
            password_hash=pwd_hash,
        )
        return json_response({"registered": True, "user_id": user.id}, status_code=201)

    # 2. Login Route
    @app.post("/auth/login")
    async def login(form: LoginForm) -> Response:
        data = form.validated()
        user = await E2EUser.where("email", data["email"]).first()
        if user is None:
            raise UnauthorizedError("Invalid email or password")

        if not bcrypt.checkpw(
            data["password"].encode("utf-8"), user.password_hash.encode("utf-8")
        ):
            raise UnauthorizedError("Invalid email or password")

        token_pair = issuer.issue_token_pair(
            subject=str(user.id),
            claims={"name": user.name, "email": user.email},
        )
        return json_response(
            {
                "access_token": token_pair["access_token"],
                "user_id": user.id,
            }
        )

    # 3. Authenticated Profile Route
    @app.get("/auth/me")
    async def me(request: Request) -> Response:
        user_id = authenticate(request)
        user = await E2EUser.find(user_id)
        if user is None:
            raise NotFoundError("User not found")
        return json_response({"id": user.id, "name": user.name, "email": user.email})

    # 4. Articles CRUD Routes
    @app.post("/articles")
    async def create_article(request: Request, form: CreateArticleForm) -> Response:
        user_id = authenticate(request)
        data = form.validated()
        article = await E2EArticle.create(
            title=data["title"],
            content=data["content"],
            author_id=user_id,
        )
        return json_response(
            {
                "id": article.id,
                "title": article.title,
                "content": article.content,
                "author_id": article.author_id,
            },
            status_code=201,
        )

    @app.get("/articles/{article_id}")
    async def get_article(request: Request, article_id: int) -> Response:
        authenticate(request)
        article = await E2EArticle.find(article_id)
        if article is None:
            raise NotFoundError("Article not found")
        return json_response(
            {
                "id": article.id,
                "title": article.title,
                "content": article.content,
                "author_id": article.author_id,
            }
        )

    @app.put("/articles/{article_id}")
    async def update_article(
        request: Request, article_id: int, form: UpdateArticleForm
    ) -> Response:
        user_id = authenticate(request)
        article = await E2EArticle.find(article_id)
        if article is None:
            raise NotFoundError("Article not found")
        if article.author_id != user_id:
            raise ForbiddenError("Not authorized to modify this article")

        data = form.validated()
        article.title = data["title"]
        article.content = data["content"]
        await article.save()

        return json_response(
            {
                "id": article.id,
                "title": article.title,
                "content": article.content,
                "author_id": article.author_id,
            }
        )

    @app.delete("/articles/{article_id}")
    async def delete_article(request: Request, article_id: int) -> Response:
        user_id = authenticate(request)
        article = await E2EArticle.find(article_id)
        if article is None:
            raise NotFoundError("Article not found")
        if article.author_id != user_id:
            raise ForbiddenError("Not authorized to delete this article")

        await article.delete()
        return json_response({"deleted": True, "id": article_id})

    # ==================== EXECUTION WORKFLOW ====================

    # Step A: Validation errors on registration
    res_invalid_reg = await client.post(
        "/auth/register",
        json={"name": "Al", "email": "bad-email", "password": "123"},
    )
    res_invalid_reg.assert_status(422)
    res_invalid_reg.assert_validation_error("name")
    res_invalid_reg.assert_validation_error("email")
    res_invalid_reg.assert_validation_error("password")

    # Step B: Register successfully
    res_reg = await client.post(
        "/auth/register",
        json={
            "name": "Alice Developer",
            "email": "alice.dev@example.com",
            "password": "strongpassword123",
        },
    )
    res_reg.assert_created()
    alice_id = res_reg.json()["user_id"]
    assert alice_id > 0

    # Step C: Unique email duplicate registration rejected
    res_dup = await client.post(
        "/auth/register",
        json={
            "name": "Alice Impostor",
            "email": "alice.dev@example.com",
            "password": "strongpassword123",
        },
    )
    res_dup.assert_validation_error("email")

    # Step D: Login with bad credentials rejected (401)
    res_bad_login = await client.post(
        "/auth/login",
        json={"email": "alice.dev@example.com", "password": "wrongpassword"},
    )
    res_bad_login.assert_unauthorized()

    # Step E: Login successfully and receive JWT
    res_login = await client.post(
        "/auth/login",
        json={
            "email": "alice.dev@example.com",
            "password": "strongpassword123",
        },
    )
    res_login.assert_ok()
    token = res_login.json()["access_token"]
    assert len(token) > 20

    # Step F: Access protected route without token rejected (401)
    res_unauth = await client.get("/auth/me")
    res_unauth.assert_unauthorized()

    # Access with tampered token rejected (401)
    res_tampered = await client.with_token(token + "tampered").get("/auth/me")
    res_tampered.assert_unauthorized()

    # Access with valid token succeeds (200)
    auth_client = client.with_token(token)
    res_me = await auth_client.get("/auth/me")
    res_me.assert_ok()
    res_me.assert_json_fragment({"id": alice_id, "email": "alice.dev@example.com"})

    # Step G: Create article via authenticated POST
    res_article = await auth_client.post(
        "/articles",
        json={
            "title": "Building ASGI with SwiftPY",
            "content": "A deep dive into SwiftPY framework internals.",
        },
    )
    res_article.assert_created()
    article_id = res_article.json()["id"]
    assert article_id > 0
    assert res_article.json()["author_id"] == alice_id

    # Step H: Retrieve article via GET
    res_get_art = await auth_client.get(f"/articles/{article_id}")
    res_get_art.assert_ok()
    res_get_art.assert_json_fragment(
        {"title": "Building ASGI with SwiftPY", "author_id": alice_id}
    )

    # Step I: Update article via PUT
    res_update_art = await auth_client.put(
        f"/articles/{article_id}",
        json={
            "title": "Building ASGI with SwiftPY (2nd Edition)",
            "content": "Updated content with advanced validation patterns.",
        },
    )
    res_update_art.assert_ok()
    assert res_update_art.json()["title"] == "Building ASGI with SwiftPY (2nd Edition)"

    # Step J: Cross-user authorization checks (User A vs User B)
    # Register User B (Bob)
    res_reg_bob = await client.post(
        "/auth/register",
        json={
            "name": "Bob Developer",
            "email": "bob.dev@example.com",
            "password": "bobpassword123",
        },
    )
    res_reg_bob.assert_created()
    bob_id = res_reg_bob.json()["user_id"]
    assert bob_id != alice_id

    # Login User B
    res_login_bob = await client.post(
        "/auth/login",
        json={
            "email": "bob.dev@example.com",
            "password": "bobpassword123",
        },
    )
    res_login_bob.assert_ok()
    bob_token = res_login_bob.json()["access_token"]
    bob_client = client.with_token(bob_token)

    # Bob attempts to update Alice's article -> 403 Forbidden
    res_bob_update = await bob_client.put(
        f"/articles/{article_id}",
        json={
            "title": "Bob Hijacked Title",
            "content": "Bob malicious overwrite.",
        },
    )
    res_bob_update.assert_forbidden()
    assert res_bob_update.json() == {"detail": "Not authorized to modify this article"}

    # Bob attempts to delete Alice's article -> 403 Forbidden
    res_bob_delete = await bob_client.delete(f"/articles/{article_id}")
    res_bob_delete.assert_forbidden()
    assert res_bob_delete.json() == {"detail": "Not authorized to delete this article"}

    # Verify Alice's article remains intact and uncompromised
    res_art_intact = await auth_client.get(f"/articles/{article_id}")
    res_art_intact.assert_ok()
    assert res_art_intact.json()["title"] == "Building ASGI with SwiftPY (2nd Edition)"

    # Step K: Token boundary conditions and failure cases
    # 1. Expired token -> 401
    expired_token = issuer.issue_access_token(
        subject=str(alice_id),
        claims={"email": "alice.dev@example.com"},
        expires_in=-3600,
    )
    res_expired = await client.with_token(expired_token).get("/auth/me")
    res_expired.assert_unauthorized()
    assert res_expired.json() == {"detail": "Invalid token"}

    # 2. Token signed with a different key / invalid signature -> 401
    foreign_issuer = JWTIssuer(secret="completely-different-signing-secret-32b!!")
    bad_sig_token = foreign_issuer.issue_access_token(
        subject=str(alice_id),
        claims={"email": "alice.dev@example.com"},
    )
    res_bad_sig = await client.with_token(bad_sig_token).get("/auth/me")
    res_bad_sig.assert_unauthorized()
    assert res_bad_sig.json() == {"detail": "Invalid token"}

    # 3. Malformed bearer token syntax -> 401
    res_malformed = await client.with_token("not-a-valid-jwt.three.parts").get(
        "/auth/me"
    )
    res_malformed.assert_unauthorized()

    # 4. Non-Bearer authorization header format -> 401
    res_bad_header = await client.with_headers(
        {"Authorization": "Basic dXNlcjpwYXNz"}
    ).get("/auth/me")
    res_bad_header.assert_unauthorized()

    # 5. Token whose subject does not correspond to an existing user -> 404
    non_existent_token = issuer.issue_access_token(
        subject="999999",
        claims={"email": "ghost@example.com"},
    )
    res_ghost = await client.with_token(non_existent_token).get("/auth/me")
    res_ghost.assert_not_found()
    assert res_ghost.json() == {"detail": "User not found"}

    # Step L: Alice deletes her own article via DELETE -> 200
    res_del = await auth_client.delete(f"/articles/{article_id}")
    res_del.assert_ok()
    assert res_del.json()["deleted"] is True

    # Step M: Retrieve deleted article returns 404 Not Found
    res_missing = await auth_client.get(f"/articles/{article_id}")
    res_missing.assert_not_found()
