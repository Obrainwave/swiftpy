from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

import pytest

from swiftpy import (
    CORS,
    Application,
    CurrentUser,
    JWTGuard,
    LazyLoadingDisabledError,
    Request,
    RequestId,
    Response,
    RouteGroup,
    SecurityHeaders,
    json_response,
)
from swiftpy.ai import (
    Agent,
    AgentMemory,
    EmbeddingsClient,
    InMemoryMemoryBackend,
    InMemoryVectorStore,
    Retriever,
    XMLBlocksFormatter,
    rag,
    tool,
)
from swiftpy.ai.client import AI
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.core.config import Config
from swiftpy.database.migrations import Blueprint, Migration, Schema, SchemaGrammar
from swiftpy.database.orm import HasMany, HasOne, Model
from swiftpy.database.pagination import Paginator, SimplePaginator
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query
from swiftpy.filesystem import Storage
from swiftpy.http.response import StreamingResponse
from swiftpy.mail.templates.renderer import MailTemplateRenderer
from swiftpy.queue import Job
from swiftpy.queue import job as queue_job
from swiftpy.queue.job import set_dispatch_handler
from swiftpy.testing import AIFake
from swiftpy.testing.http_client import TestClient
from swiftpy.validation import FormRequest
from swiftpy.validation.rules import email, min_, required, string, unique

# ==============================================================================
# 1. Minimal Application Entrypoint (README Example 1)
# ==============================================================================


@pytest.mark.asyncio
async def test_readme_minimal_application_entrypoint() -> None:
    test_secret = "a" * 32
    app_config = Config(
        {
            "app": {"name": "SwiftPY Store API", "env": "testing", "debug": True},
            "auth": {"jwt": {"secret": test_secret}},
            "redis": {
                "host": os.getenv("REDIS_HOST", "127.0.0.1"),
                "port": int(os.getenv("REDIS_PORT", "6379")),
                "password": os.getenv("REDIS_PASSWORD") or None,
            },
        }
    )

    app = Application(
        title="SwiftPY Store API",
        version="1.0.0",
        config=app_config,
        auto_discover_providers=False,
    )
    app.middleware([RequestId, SecurityHeaders, CORS])

    @app.get("/ping")
    async def ping(request: Request) -> dict[str, str]:
        return {"message": "pong"}

    @app.group("/api/v1")
    def v1_routes(group: RouteGroup) -> None:
        @group.get("/users/{user_id}")
        async def show_user(request: Request, user_id: int) -> Response:
            return json_response({"user_id": user_id, "ok": True})

    @app.group("/api/v1/account", middleware=[JWTGuard])
    def account_routes(group: RouteGroup) -> None:
        @group.get("/me")
        async def me(user: CurrentUser) -> Response:
            return json_response({"user": dict(user)})

    client = TestClient(app)

    # Ping endpoint check
    ping_resp = await client.get("/ping")
    assert ping_resp.status_code == 200
    assert ping_resp.json() == {"message": "pong"}
    assert "x-request-id" in ping_resp.headers
    assert ping_resp.headers.get("x-content-type-options") == "nosniff"

    # Route group parameter check
    user_resp = await client.get("/api/v1/users/42")
    assert user_resp.status_code == 200
    assert user_resp.json() == {"user_id": 42, "ok": True}

    # Route group JWT guarded endpoint (unauthorized)
    unauth_resp = await client.get("/api/v1/account/me")
    assert unauth_resp.status_code == 401

    # Route group JWT guarded endpoint (authorized with token)
    issuer = JWTIssuer(secret=test_secret)
    token = issuer.issue_access_token(subject="user_42", claims={"role": "admin"})
    auth_resp = await client.get(
        "/api/v1/account/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert auth_resp.status_code == 200
    res_data = auth_resp.json()
    assert res_data["user"]["sub"] == "user_42"
    assert res_data["user"]["role"] == "admin"


# ==============================================================================
# 2. Active Record Models & Relationships (README Example 2)
# ==============================================================================


class ReadmePost(Model):
    __table__ = "readme_example_posts"
    __fillable__: ClassVar[list[str]] = ["title", "user_id"]

    id: int
    title: str
    user_id: int


class ReadmeProfile(Model):
    __table__ = "readme_example_profiles"
    __fillable__: ClassVar[list[str]] = ["bio", "user_id"]

    id: int
    bio: str
    user_id: int


class ReadmeUser(Model):
    __table__ = "readme_example_users"
    __fillable__: ClassVar[list[str]] = ["name", "email"]

    id: int
    name: str
    email: str

    posts = HasMany(ReadmePost, foreign_key="user_id")
    profile = HasOne(ReadmeProfile, foreign_key="user_id")


@pytest.mark.asyncio
async def test_readme_active_record_models_and_relationships(
    ensure_db_pool: DatabasePool | None,
) -> None:
    if ensure_db_pool is None:
        pytest.skip("PostgreSQL service is not available in local test environment")
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS readme_example_posts CASCADE;")
        await conn.execute("DROP TABLE IF EXISTS readme_example_profiles CASCADE;")
        await conn.execute("DROP TABLE IF EXISTS readme_example_users CASCADE;")
        await conn.execute(
            """
            CREATE TABLE readme_example_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255) NOT NULL
            );
            CREATE TABLE readme_example_posts (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL REFERENCES readme_example_users(id) ON DELETE CASCADE,
                title VARCHAR(255) NOT NULL
            );
            CREATE TABLE readme_example_profiles (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL REFERENCES readme_example_users(id) ON DELETE CASCADE,
                bio TEXT NOT NULL
            );
            """
        )

    try:
        user = await ReadmeUser.create(name="Alice", email="alice@example.com")
        await ReadmePost.create(title="Post 1", user_id=user.id)
        await ReadmePost.create(title="Post 2", user_id=user.id)
        await ReadmeProfile.create(bio="Developer and writer", user_id=user.id)

        # 1. Querying and eager loading:
        users = (
            await ReadmeUser.with_("posts", "profile").where("name", "Alice").get()
        )
        assert len(users) == 1
        alice = users[0]
        assert alice.name == "Alice"
        assert len(alice.posts) == 2
        assert alice.profile is not None
        assert alice.profile.bio == "Developer and writer"

        # 2. Dirty-tracking save:
        loaded_user = await ReadmeUser.find(user.id)
        assert loaded_user is not None
        loaded_user.name = "Alice Cooper"
        await loaded_user.save()

        refreshed = await ReadmeUser.find(user.id)
        assert refreshed is not None
        assert refreshed.name == "Alice Cooper"

        # 3. Strict N+1 / lazy loading prevention check
        strict_user = await ReadmeUser.find(user.id)
        assert strict_user is not None
        ReadmeUser.prevent_lazy_loading = True
        try:
            with pytest.raises(LazyLoadingDisabledError):
                _ = strict_user.posts
        finally:
            ReadmeUser.prevent_lazy_loading = False

    finally:
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS readme_example_posts CASCADE;")
            await conn.execute("DROP TABLE IF EXISTS readme_example_profiles CASCADE;")
            await conn.execute("DROP TABLE IF EXISTS readme_example_users CASCADE;")


# ==============================================================================
# 3. Fluent Migrations & Schema Builder (README Example 3)
# ==============================================================================


class CreatePostsTable(Migration):
    async def up(self) -> None:
        def define(table: Blueprint) -> None:
            table.id()
            table.big_integer("user_id").unsigned()
            table.string("title", 200)
            table.text("body").nullable()
            table.boolean("is_published").default(False)
            table.timestamps()
            table.foreign_key(
                "user_id", references="id", on="users", on_delete="CASCADE"
            )
            table.index(["user_id", "is_published"], name="idx_posts_user_published")

        await Schema.create("posts", define)

    async def down(self) -> None:
        await Schema.drop_if_exists("posts")


@pytest.mark.asyncio
async def test_readme_fluent_migrations_and_schema_builder() -> None:
    table = Blueprint("posts", is_create=True)
    table.id()
    table.big_integer("user_id").unsigned()
    table.string("title", 200)
    table.text("body").nullable()
    table.boolean("is_published").default(False)
    table.timestamps()
    table.foreign_key("user_id", references="id", on="users", on_delete="CASCADE")
    table.index(["user_id", "is_published"], name="idx_posts_user_published")

    statements = SchemaGrammar.compile_create(table)
    assert len(statements) == 2
    create_sql, index_sql = statements[0], statements[1]

    assert 'CREATE TABLE "posts"' in create_sql
    assert '"id" BIGSERIAL PRIMARY KEY' in create_sql
    assert '"user_id" BIGINT NOT NULL CHECK ("user_id" >= 0)' in create_sql
    assert '"title" VARCHAR(200) NOT NULL' in create_sql
    assert '"body" TEXT NULL' in create_sql
    assert '"is_published" BOOLEAN NOT NULL DEFAULT FALSE' in create_sql
    assert '"created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP' in create_sql
    assert '"updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP' in create_sql
    assert (
        'CONSTRAINT "fk_posts_user_id_users" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE CASCADE'
        in create_sql
    )
    assert 'CREATE INDEX "idx_posts_user_published"' in index_sql

    drop_sql = SchemaGrammar.compile_drop_if_exists("posts")
    assert drop_sql == 'DROP TABLE IF EXISTS "posts";'


# ==============================================================================
# 4. Asynchronous Queues & Background Jobs (README Example 4)
# ==============================================================================


class SendWelcomeEmail(Job):
    tries = 3
    backoff = (1.0, 5.0, 15.0)

    def __init__(self, user_id: int, email: str) -> None:
        super().__init__(user_id=user_id, email=email)
        self.user_id = user_id
        self.email = email
        self.handled = False
        self.failed_called = False

    async def handle(self) -> None:
        self.handled = True

    async def failed(self, exception: BaseException) -> None:
        self.failed_called = True


@pytest.mark.asyncio
async def test_readme_asynchronous_queues_and_jobs() -> None:
    test_key = "k" * 32
    orig_app_key = Job.get_app_key()
    orig_handler = queue_job._dispatch_handler

    try:
        Job.set_app_key(test_key)
        Job.allow_job_class(SendWelcomeEmail)

        job = SendWelcomeEmail(user_id=42, email="alice@example.com")
        envelope = job.serialize()

        assert envelope["job_class"] == f"{SendWelcomeEmail.__module__}:{SendWelcomeEmail.__qualname__}"
        assert envelope["kwargs"]["user_id"] == 42
        assert envelope["kwargs"]["email"] == "alice@example.com"
        assert "signature" in envelope

        restored = Job.deserialize(envelope)
        assert isinstance(restored, SendWelcomeEmail)
        assert restored.user_id == 42
        assert restored.email == "alice@example.com"

        await restored.handle()
        assert restored.handled is True

        # Test dispatch hook
        dispatched_jobs: list[tuple[Job, str | None, float]] = []

        def fake_dispatcher(j: Job, q: str | None, delay: float) -> str:
            dispatched_jobs.append((j, q, delay))
            return j.job_id

        set_dispatch_handler(fake_dispatcher)
        job_id = await SendWelcomeEmail(user_id=42, email="alice@example.com").dispatch()
        assert job_id == job.job_id or len(dispatched_jobs) == 1
        assert len(dispatched_jobs) == 1
        assert dispatched_jobs[0][2] == 0.0

        await SendWelcomeEmail(user_id=42, email="alice@example.com").dispatch(delay=60.0)
        assert len(dispatched_jobs) == 2
        assert dispatched_jobs[1][2] == 60.0
    finally:
        Job.set_app_key(orig_app_key)
        set_dispatch_handler(orig_handler)


# ==============================================================================
# 5. Pagination, Query Builder, Storage, Validation, & Documented Keys
# ==============================================================================


def test_readme_pagination_and_query_builder_contracts() -> None:
    paginator = Paginator(items=[{"id": 1}, {"id": 2}], total=5, per_page=2, current_page=1)
    payload = paginator.to_dict()
    assert payload["current_page"] == 1
    assert payload["last_page"] == 3
    assert payload["has_more_pages"] is True
    assert payload["has_previous_page"] is False

    simple = SimplePaginator(items=[{"id": 1}], per_page=20, current_page=1, has_more=False)
    assert simple.has_more_pages is False

    sql, params = (
        Query.table("orders")
        .select("country")
        .select_count("id", alias="total_orders")
        .select_sum("amount", alias="total_revenue")
        .group_by("country")
        .having("total_orders", ">", 10)
        .order_by("total_revenue", "DESC")
        .to_sql()
    )
    assert 'GROUP BY "country"' in sql
    assert "HAVING COUNT" in sql
    assert params == [10]


@pytest.mark.asyncio
async def test_readme_storage_facade_example(tmp_path: Path) -> None:
    app_config = Config(
        {
            "app": {"name": "SwiftPY Docs", "env": "testing", "debug": True, "key": "k" * 32},
            "filesystems": {
                "default": "local",
                "signing_key": "k" * 32,
                "disks": {
                    "local": {
                        "driver": "local",
                        "root": str(tmp_path / "private"),
                        "visibility": "private",
                    }
                },
            },
        }
    )
    app = Application(config=app_config, auto_discover_providers=False)
    assert app is not None

    pdf_bytes = b"%PDF-1.4\nREADME storage smoke\n"
    await Storage.disk("local").put("reports/annual_2026.pdf", pdf_bytes)
    assert await Storage.exists("reports/annual_2026.pdf")
    assert await Storage.size("reports/annual_2026.pdf") == len(pdf_bytes)
    assert isinstance(await Storage.mime_type("reports/annual_2026.pdf"), str)

    @app.get("/reports/download")
    async def download_report(request: Request) -> Response:
        return await Storage.download(
            "reports/annual_2026.pdf", name="Annual_Report_2026.pdf"
        )

    client = TestClient(app)
    resp = await client.get("/reports/download")
    assert resp.status_code == 200
    assert resp.body == pdf_bytes
    assert "Annual_Report_2026.pdf" in resp.headers.get("content-disposition", "")


class ReadmeRegisterRequest(FormRequest):
    def rules(self) -> dict[str, Any]:
        return {
            "name": [required(), string(), min_(3)],
            "email": [required(), email()],
            "password": [required(), string(), min_(8)],
        }


@pytest.mark.asyncio
async def test_readme_form_request_validation_example() -> None:
    _unused_unique_ref = unique
    app_config = Config({"app": {"name": "SwiftPY Docs", "env": "testing", "debug": True}})
    app = Application(config=app_config, auto_discover_providers=False)

    @app.post("/register")
    async def register(form: ReadmeRegisterRequest) -> Response:
        data = form.validated()
        return json_response({"name": data["name"], "email": data["email"]}, status_code=201)

    client = TestClient(app)
    bad = await client.post(
        "/register",
        json={"name": "Al", "email": "not-an-email", "password": "short"},
    )
    assert bad.status_code == 422

    ok = await client.post(
        "/register",
        json={"name": "Alice", "email": "alice@example.com", "password": "secret-password"},
    )
    assert ok.status_code == 201
    assert ok.json() == {"name": "Alice", "email": "alice@example.com"}


def test_readme_documented_config_and_env_keys() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env_example = (repo_root / ".env.example").read_text(encoding="utf-8")
    for key in (
        "APP_ENV=local",
        "APP_DEBUG=false",
        "APP_KEY=",
        "DB_CONNECTION=postgresql",
        "DB_SSL=disable",
        "DB_SSL_CA_FILE=",
        "FILESYSTEM_DISK=local",
        "MAIL_MAILER=smtp",
        "JWT_SECRET=",
        "AUTH_KEY_PEPPER=",
        "CORS_ALLOWED_ORIGINS=",
        "CORS_ALLOW_CREDENTIALS=false",
    ):
        assert key in env_example

    cfg = Application(auto_discover_providers=False).config
    assert cfg.get("app.env") in {"local", "testing", "production"}
    assert cfg.get("filesystems.default") == "local"
    assert isinstance(cfg.get("cors.allow_credentials"), bool)

    assert (repo_root / "views" / "mail").is_dir()
    renderer = MailTemplateRenderer()
    assert renderer.template_dir.name == "views"


# ==============================================================================
# 6. Native AI Subsystem (README Example 8)
# ==============================================================================


@tool
async def readme_lookup_order_status(order_id: int) -> dict[str, object]:
    """Look up fulfillment status for a customer order ID."""
    return {"order_id": order_id, "status": "shipped"}


@pytest.mark.asyncio
async def test_readme_ai_client_rag_and_agent_example() -> None:
    app = Application(
        config={"app": {"name": "SwiftPY AI Docs", "env": "testing", "debug": True}},
        auto_discover_providers=False,
    )

    @app.get("/ai/stream")
    async def stream_answer(request: Request) -> StreamingResponse:
        question = request.query_params.get("question", "Explain asyncpg pooling")

        async def chunks() -> Any:
            client = AI.chat(model="claude-3-5-sonnet-20241022").user(question)
            async for token in client.stream():
                yield token.encode("utf-8")

        return StreamingResponse(
            chunks(), headers={"content-type": "text/plain; charset=utf-8"}
        )

    # 1. Fluent chat + SSE streaming under AIFake
    with AIFake("DatabasePool manages asyncpg connections.") as fake:
        response = await (
            AI.chat(model="gpt-4o-mini")
            .system("Answer concisely in one paragraph.")
            .user("Summarize SwiftPY's transaction model.")
            .temperature(0.2)
            .max_tokens(300)
            .send()
        )
        assert response.content == "DatabasePool manages asyncpg connections."
        fake.assert_called_once()

        client = TestClient(app)
        stream_resp = await client.get("/ai/stream", query={"question": "pool?"})
        assert stream_resp.status_code == 200
        assert "DatabasePool manages asyncpg connections." in stream_resp.text

    # 2. Embeddings, InMemoryVectorStore & RAG pipeline
    store = InMemoryVectorStore(dimensions=1536, metric="cosine")
    await store.create_collection("handbook", dimensions=1536, metric="cosine")
    retriever = Retriever(
        vectorstore=store,
        embeddings=EmbeddingsClient(),
        provider="deterministic",
    )
    await retriever.add_documents(
        ["SwiftPY manages asyncpg connections via DatabasePool."]
    )

    with AIFake("SwiftPY manages PostgreSQL connections with DatabasePool."):
        rag_result = await (
            rag()
            .retriever(retriever)
            .top_k(4)
            .formatter(XMLBlocksFormatter())
            .generator(AI.chat(model="gpt-4o-mini"))
            .run("How does SwiftPY manage PostgreSQL connections?")
        )
        assert (
            rag_result.text
            == "SwiftPY manages PostgreSQL connections with DatabasePool."
        )
        assert len(rag_result.sources) == 1

    # 3. @tool + Stateful Agent with session memory
    with AIFake("Order #42 is shipped.") as fake:
        agent = Agent(
            name="SupportAssistant",
            system_prompt="Help customers track orders accurately.",
            tools=[readme_lookup_order_status],
            memory=AgentMemory(
                backend=InMemoryMemoryBackend(), session_id="user-7"
            ),
            model="gpt-4o-mini",
        )
        reply = await agent.run("Where is order #42?", session_id="user-7")
        assert reply.content == "Order #42 is shipped."
        fake.assert_called_once()
