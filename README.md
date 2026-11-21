<p align="center">
  <img src="./red-black.png" alt="SwiftPY Logo" width="420" />
</p>

# SwiftPY

**An ASGI-native, Eloquent-style Python web framework combining the developer experience of FastAPI with the elegance and structure of Laravel.**

[![CI](https://github.com/obrainwave/swiftpy/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/obrainwave/swiftpy/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: Production-Hardened](https://img.shields.io/badge/status-production--hardened-green.svg)](./DEVLOG.md)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Types: Mypy Strict](https://img.shields.io/badge/types-mypy%20strict-blue.svg)](https://mypy-lang.org/)
[![Tests: Automated Suite](https://img.shields.io/badge/tests-pytest%20suite-brightgreen.svg)](./DEVLOG.md)

---

## Overview

SwiftPY brings the joy and productivity of Laravel to the modern asynchronous Python ecosystem. It eliminates boilerplate and fragmented third-party glue code by providing a unified, cohesive framework built natively on Python 3.12+ and high-performance [`asyncpg`](https://github.com/MagicStack/asyncpg).

- **FastAPI-Grade Simplicity**: Clean, intuitive entrypoint (`app = Application()`), async-first request/response cycle (`Request.input()`, `request.<field>`, `abort()`, declarative `HttpException`), constructor-configured `Router(prefix=..., middleware=...)`, automatic path parameter type coercion, and scoped route groups with middleware pipelines.
- **Eloquent-Caliber Active Record ORM**: Typed models declaring class attributes via standard Python type hints (no `ClassVar` boilerplate), automatic irregular-aware table pluralization, dirty-tracking `save()`, expressive relationships (`HasMany`, `BelongsTo`, `HasOne`, `BelongsToMany`), and eager loading with strict N+1 query prevention (via model-level `prevent_lazy_loading = True`, raising `LazyLoadingDisabledError` on un-eager-loaded accesses).
- **Query Builder, GROUP BY & Dynamic Pagination**: Full length-aware `paginate()` and lightweight `simple_paginate()`, `limit()`/`offset()` (with `take()`/`skip()`), group-aware subquery counting, and aggregate selectors (`select_count`, `select_sum`, etc.) with `having` and `or_having` logical filtering.
- **Multi-Disk Storage & Streaming Engine**: Flysystem-inspired `Storage` abstraction supporting local and S3 disks, high-throughput streaming uploads/downloads, magic-byte validation, and `storage:link`.
- **Validation Ergonomics & Input Hardening**: `FormRequest` and `await validate(request, rules)` returning `ValidatedData` (supporting both dict and dot attribute access), `stop_on_first_error`, `ValidationException.single()`, automatic 422 JSON errors, configurable body streaming limits, Content-Length checks, and JSON recursion depth caps.
- **Authentication, Refresh Rotation & Session Revocation**: `JWTIssuer` with `refresh_token_pair()` rotation and reuse rejection, `auth().logout()` / `auth().logout_all()`, `TokenBlacklist`, `ApiKeyGuard`, and `AdminSessionStore.flush()`.
- **Structured Asynchronous Logging**: Non-blocking `Log` facade and `LogManager` (`single`, `daily`, `stderr`, `stack`) backed by bounded `QueueHandler` / `QueueListener` with clean ASGI startup/shutdown flushing and per-application isolation.
- **Native AI Subsystem (`swiftpy.ai`)**: Fluent async `AI` client across OpenAI and Anthropic providers with shared `AIHttpPool` pooling and SSE streaming, batched `EmbeddingsClient`, unified `VectorStore` adapters (`PgVectorAdapter` for PostgreSQL `pgvector`, `InMemoryVectorStore`, Qdrant, Pinecone, Chroma, LanceDB), nonce-bounded RAG pipelines (`rag()`, `Retriever`, Cohere/CrossEncoder rerankers), autonomous `@tool` / `Agent` loops with session memory, prompt templates (`prompts/`), moderation/rate-limit/budget middlewares, observability telemetry, and `AIFake` / `VectorStoreFake` test doubles.
- **Database Engine & Native Transactions**: Connection pool management, automatic transaction propagation across context variables, nested transaction savepoints, and strict SQL injection prevention with full identifier quoting for reserved words.
- **Fluent Migration DSL & Schema Builder**: Laravel-inspired `Blueprint` table definitions, safe statement ordering (drops executed before adds), column comments, unsigned checks, composite keys, and distributed PostgreSQL advisory locks.
- **At-Least-Once Queues & Signed Bus**: Robust background jobs with Redis visibility timeout leases, HMAC-SHA256 envelope signing, dead-letter routing, and a cryptographically protected cross-worker event bus.
- **WebSocket Gateway**: Structured WebSocket connection management, topic subscriptions, broadcast fan-out, ping/pong heartbeats, and strict same-origin handshake validation.
- **Zero-Config Provider Auto-Discovery**: Automatic service provider discovery in `app/providers/` that registers container dependencies behind the scenes without manual entrypoint registration.
- **Developer CLI (`swiftpy`)**: Built-in CLI for scaffolding new projects (`new`) and components (`make:*`), running/rolling back/resetting/seeding migrations (`migrate*`, `db:seed`), linking public storage, managing queue workers and failed jobs, precompiling caches, and starting the ASGI server.

---

## Status & Roadmap

All core architectural, storage, AI, and security hardening phases have been completed and verified against an automated PostgreSQL + Redis pytest suite:

| Milestone | Component Focus | Status |
|---|---|---|
| **Phase 0: Testing Harness & Quality Baseline** | CI harness, container test environments, strict ruff/mypy configurations | ✅ Completed |
| **Phase 1: Architecture & Foundation** | ASGI HTTP kernel, async router, route groups, IoC container, task isolation, middleware pipeline | ✅ Completed |
| **Phase 2: Authentication & Security** | Cryptographic JWT issuer, HMAC API key guards, server-side peppers, constant-time comparisons | ✅ Completed |
| **Phase 3: Database & Identifier Safety** | asyncpg connection pool, strict SQL identifier validation/quoting, nested transactions | ✅ Completed |
| **Phase 4: HTTP Kernel & Error Pipeline** | Request streaming limits (2 MiB default), typed response factories, fail-closed error pipeline | ✅ Completed |
| **Phase 5: Concurrency & DI Scoping** | ContextVar-backed scoped DI, async lifecycle hooks (`startup`/`shutdown`), graceful teardown | ✅ Completed |
| **Phase 5: Filesystem & Cloud Storage** | Multi-disk abstraction (`local`, `s3`), streaming uploads/downloads, magic-byte validation, `storage:link` | ✅ Completed |
| **Phase 6: Queues & Reliability** | At-least-once Redis queue leases, HMAC envelope signing, namespace allowlists, signed event bus | ✅ Completed |
| **Phase 7: Active Record ORM Hardening** | Mass assignment protection (`__fillable__`/`__guarded__`), eager loading N+1 prevention, dirty tracking | ✅ Completed |
| **Phase 8: Validation DSL Hardening** | Fail-closed FormRequest validation, hardened rule composition, input sanitization | ✅ Completed |
| **Phase 9: Real-time WebSockets** | Handshake validation, same-origin protection (D-14), bounded broadcast fan-out, lifecycle hooks | ✅ Completed |
| **Phase 10: Infra, CI & Hygiene** | Multi-stage Docker, pinned CI workflow, hardened compose, repo hygiene, CLI options | ✅ Completed |
| **Phases 11–13: Native AI Subsystem** | Multi-provider `AI` client, SSE streaming, `EmbeddingsClient`, `PgVectorAdapter` & vector stores, `rag()`, `Agent` / `@tool`, prompts, guardrails & `AIFake` | ✅ Completed |
| **Week 26: Security Audit & Hardening** | SQL injection audit, JWT & bcrypt API keys, JSON depth & streaming limits, traceback suppression | ✅ Completed |
| **Week 26: Pagination & Aggregation Subsystem** | Limit/offset parameterized SQL, length-aware `paginate()`, `simple_paginate()`, `group_by()` & `having()` | ✅ Completed |
| **Release v0.2.0: DX, Logging, Auth Refresh & Docs** | `ValidatedData` & `validate(request)`, `Request.input()` & `abort()`, `Router(prefix, middleware)`, `Blueprint.enum`/`medium_text`/`long_text`, `swiftpy.logging` (`Log`), `refresh_token_pair()` & `auth().logout()`, 17 user guides in `documentations/` | ✅ Completed |
| **Release v0.2.1: Auth Context on Token Issue** | `CurrentUser` / `auth()` available when an access token is issued, `user=` / `Model` subjects, opt-in `auth.hydrate_user` & `auth.passive_resolution` | ✅ Completed |
| **Release v0.2.2: Model `.update()`, `isset()`, `has_file()`, & Auth TTL** | `Model.update()` / `force_update()`, `JWT_ACCESS_TTL=3600`, 405 `"for route"` detail, `isset()` helper, `request.has_file()` (sync & `await`), CORS guide | ✅ Completed |

*See [`documentations/2026-11-14-installation-quick-start.md`](./documentations/2026-11-14-installation-quick-start.md) for the complete step-by-step documentation series and [`CHANGELOG.md`](./CHANGELOG.md) for release notes.*

---

## Verified Performance Benchmarks

All benchmarks were measured under concurrent load using `wrk` against the containerized production runtime in Docker with PostgreSQL and Redis connections active. See [`DEVLOG.md`](./DEVLOG.md) for full reproduction commands and raw latency distributions.

| Workload | Endpoint | Target Requirement | Measured Throughput / Latency | Status |
|---|---|---|---|---|
| **Simple JSON** | `GET /benchmarks/json` | $\ge$ 3,000 req/s | **6,507 req/s** (P50: 14.04ms, P90: 28.25ms) | 🚀 **Exceeded (+116.9%)** |
| **Authenticated DB Endpoint** | `GET /benchmarks/auth-db` | $\ge$ 1,000 req/s | **1,065 req/s** (P50: 78.02ms, P90: 164.12ms) | 🎯 **Target Met** |
| **RAG Pipeline (Vector + Prompt)** | `GET /benchmarks/rag` | < 900ms P95 latency | **P95: ~170ms** (268 req/s, P50: 61.41ms) | 🚀 **Exceeded (5.3x lower P95)** |
| **Streaming SSE Endpoint** | `GET /benchmarks/stream` | Chunked response | **4,114 req/s** (P50: 7.97ms, P90: 14.90ms) | ✅ **Production Ready** |
| **Authenticated Endpoint** | `GET /benchmarks/auth` | JWT verification | **2,939 req/s** (P50: 29.87ms, P90: 70.03ms) | ✅ **Production Ready** |
| **Database Read** | `GET /benchmarks/db` | QueryBuilder query | **1,476 req/s** (P50: 30.60ms, P90: 108.25ms) | ✅ **Production Ready** |

### Key Optimizations:
- **$O(1)$ Static Routing**: Direct dictionary hash lookup for non-parameterized routes bypasses regex scanning.
- **DI Reflection Caching**: Container parameter reflection (`inspect.signature` / `get_type_hints`) is cached permanently after the first call.
- **Redis Pipelining & Token Cache**: Batching blacklist commands into atomic pipelines with in-memory TTL caching speeds up JWT validation.
- **Precompiled Production Cache (`swiftpy cache:build`)**: Precompiles configuration (`bootstrap/cache/config.json`) and prompt templates (`bootstrap/cache/prompts.json`) into serialized JSON files to eliminate startup file parsing overhead (note: legacy `RouteCache` was removed under Decision `G-16` in favor of direct `$O(1)$` static route registration).

---

## What It Looks Like

All code examples below are verified by an automated test suite (`tests/docs/test_readme_examples.py`).

### 1. Minimal Application Entrypoint

No boilerplate or manual bootstrapping required—create your application and declare routes:

```python
# main.py
from swiftpy import (
    CORS,
    Application,
    CurrentUser,
    JWTGuard,
    Request,
    RequestId,
    Response,
    RouteGroup,
    SecurityHeaders,
    json_response,
)

app = Application(title="SwiftPY Store API", version="1.0.0")
app.middleware([RequestId, SecurityHeaders, CORS])


@app.get("/ping")
async def ping(request: Request) -> dict[str, str]:
    # Plain dict/list return values are automatically serialized to JSON (Decision G-16)
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
```

### 2. Active Record Models & Relationships

Declare models using Python type hints. Guard sensitive fields with `__fillable__` or `__guarded__`. Prevent silent N+1 queries using `.with_()` for explicit eager loading:

```python
# app/models/user.py
from swiftpy.database.orm import HasMany, HasOne, Model


class Post(Model):
    __table__ = "posts"
    __fillable__ = ["title", "user_id"]

    id: int
    title: str
    user_id: int


class Profile(Model):
    __table__ = "profiles"
    __fillable__ = ["bio", "user_id"]

    id: int
    bio: str
    user_id: int


class User(Model):
    __table__ = "users"
    __fillable__ = ["name", "email"]

    id: int
    name: str
    email: str

    posts = HasMany(Post, foreign_key="user_id")
    profile = HasOne(Profile, foreign_key="user_id")


# Querying and eager loading:
users = await User.with_("posts", "profile").where("name", "Alice").get()
for user in users:
    print(user.name, len(user.posts), user.profile.bio)

# Dirty-tracking save (updates only changed attributes and maintains updated_at):
user = await User.find(1)
if user is not None:
    user.name = "Alice Cooper"
    await user.save()
```

### 3. Fluent Query Builder, GROUP BY & Dynamic Pagination

Execute complex aggregated queries, group by columns with `HAVING`, and paginate results using length-aware `paginate()` or high-performance `simple_paginate()`:

```python
from app.models.user import User
from swiftpy.database.pagination import Paginator, SimplePaginator
from swiftpy.database.query_builder import Query

# 1. Length-aware pagination with metadata & total group count:
paginated_users: Paginator[User] = (
    await User.where("status", "active")
    .order_by("created_at", "DESC")
    .paginate(per_page=20, page=1)
)

print(paginated_users.total)          # e.g., 145
print(paginated_users.last_page)      # 8
print(paginated_users.has_more_pages) # True

# Export standard JSON payload with navigation metadata:
payload = paginated_users.to_dict()
# {
#     "data": [...],
#     "current_page": 1,
#     "per_page": 20,
#     "total": 145,
#     "last_page": 8,
#     "from": 1,
#     "to": 20,
#     "has_more_pages": True,
#     "has_previous_page": False
# }

# 2. Fast simple pagination (avoids COUNT(*) on large tables by querying per_page + 1):
simple_users: SimplePaginator[User] = (
    await User.order_by("id", "ASC").simple_paginate(per_page=20, page=1)
)

# 3. GROUP BY and HAVING with subquery group counting:
order_stats = (
    await Query.table("orders")
    .select("country")
    .select_count("id", alias="total_orders")
    .select_sum("amount", alias="total_revenue")
    .group_by("country")
    .having("total_orders", ">", 10)
    .order_by("total_revenue", "DESC")
    .paginate(per_page=5, page=1)
)
```

### 4. Multi-Disk Storage & Streaming Engine

Flysystem-inspired file storage abstraction supporting multi-disk scoping (`local`, `public`, `s3`), streaming I/O, and secure downloads:

```python
from swiftpy import Request, Response, json_response
from swiftpy.filesystem import Storage

# 1. Put file bytes or binary streams to configured disks:
await Storage.disk("local").put("reports/annual_2026.pdf", pdf_bytes)

# 2. Inspect existence, size, and metadata:
if await Storage.exists("reports/annual_2026.pdf"):
    size = await Storage.size("reports/annual_2026.pdf")
    mime = await Storage.mime_type("reports/annual_2026.pdf")

# 3. High-throughput chunked stream download:
@app.get("/reports/download")
async def download_report(request: Request) -> Response:
    return await Storage.download(
        "reports/annual_2026.pdf", name="Annual_Report_2026.pdf"
    )
```

### 5. Form Request Validation & Hardened Input

Declare typed validation schemas that fail closed before your controller handler is invoked:

```python
from app.models.user import User
from swiftpy import Response, json_response
from swiftpy.validation import FormRequest
from swiftpy.validation.rules import email, min_, required, string, unique


class RegisterRequest(FormRequest):
    def rules(self) -> dict[str, object]:
        return {
            "name": [required(), string(), min_(3)],
            "email": [required(), email(), unique("users", "email")],
            "password": [required(), string(), min_(8)],
        }


@app.post("/register")
async def register(form: RegisterRequest) -> Response:
    # Automatically validated before reaching handler; invalid input yields HTTP 422 JSON
    data = form.validated()
    user = await User.create(name=data["name"], email=data["email"])
    return json_response(user.to_dict(), status_code=201)
```

### 6. Fluent Migrations & Schema Builder

Define and alter tables using the expressive `Blueprint` DSL with safe identifier quoting and constraint generation:

```python
# database/migrations/2026_10_18_000001_create_posts_table.py
from swiftpy.database.migrations import Blueprint, Migration, Schema


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
```

### 7. Asynchronous Queues & Background Jobs

Define background jobs with automatic JSON serialization, HMAC envelope signing, retry with backoff, and dead letter queue routing:

```python
# app/jobs/send_welcome_email.py
from swiftpy.queue import Job


class SendWelcomeEmail(Job):
    tries = 3
    backoff = (1.0, 5.0, 15.0)

    def __init__(self, user_id: int, email: str) -> None:
        super().__init__(user_id=user_id, email=email)
        self.user_id = user_id
        self.email = email

    async def handle(self) -> None:
        # Business logic executed by the queue worker
        print(f"Sending welcome email to {self.email} for user {self.user_id}")

    async def failed(self, exception: BaseException) -> None:
        # Hook called when retries are exhausted and job moves to Dead Letter Queue
        print(f"Failed permanently for user {self.user_id}: {exception}")


# Dispatching jobs:
await SendWelcomeEmail(user_id=42, email="alice@example.com").dispatch()

# Or dispatch with delay:
await SendWelcomeEmail(user_id=42, email="alice@example.com").dispatch(delay=60.0)
```

### 8. Native AI Subsystem (LLM Client, Streaming, RAG & Agents)

Use `swiftpy.ai` to call OpenAI or Anthropic models through one shared connection pool, stream SSE chunks cleanly, query PostgreSQL `pgvector` or cloud vector stores, build grounded `rag()` pipelines, run tool-calling `Agent` sessions, and test deterministically offline with `AIFake`:

```python
# routes/assistant.py
from swiftpy.http.response import StreamingResponse
from swiftpy.ai import (
    AI,
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
from swiftpy.testing import AIFake

# 1. Fluent chat & SSE streaming with provider auto-inference
response = await (
    AI.chat(model="gpt-4o-mini")
    .system("Answer concisely in one paragraph.")
    .user("Summarize SwiftPY's transaction model.")
    .temperature(0.2)
    .max_tokens(300)
    .send()
)


@app.get("/ai/stream")
async def stream_answer(question: str = "Explain asyncpg pooling") -> StreamingResponse:
    async def chunks():
        client = AI.chat(model="claude-3-5-sonnet-20241022").user(question)
        async for token in client.stream():
            yield token.encode("utf-8")

    return StreamingResponse(
        chunks(), headers={"content-type": "text/plain; charset=utf-8"}
    )


# 2. Embeddings, Vector Stores (swap InMemoryVectorStore -> AI.vectorstore(adapter="pgvector", ...) for PostgreSQL) & RAG
store = InMemoryVectorStore(dimensions=1536, metric="cosine")
await store.create_collection("handbook", dimensions=1536, metric="cosine")
retriever = Retriever(vectorstore=store, embeddings=EmbeddingsClient(), provider="deterministic")
await retriever.add_documents(["SwiftPY manages asyncpg connections via DatabasePool."])

rag_result = await (
    rag()
    .retriever(retriever)
    .top_k(4)
    .formatter(XMLBlocksFormatter())
    .generator(AI.chat(model="gpt-4o-mini"))
    .run("How does SwiftPY manage PostgreSQL connections?")
)


# 3. Typed @tool + Stateful Agent with session memory (testable via AIFake)
@tool
async def lookup_order_status(order_id: int) -> dict[str, object]:
    """Look up fulfillment status for a customer order ID."""
    return {"order_id": order_id, "status": "shipped"}


with AIFake("Order #42 is shipped.") as fake:
    agent = Agent(
        name="SupportAssistant",
        system_prompt="Help customers track orders accurately.",
        tools=[lookup_order_status],
        memory=AgentMemory(backend=InMemoryMemoryBackend(), session_id="user-7"),
        model="gpt-4o-mini",
    )
    reply = await agent.run("Where is order #42?", session_id="user-7")
    fake.assert_called_once()
```

See the full [AI Subsystem Guide](./documentations/2026-11-15-ai-subsystem.md) for `PgVectorAdapter` setups, `prompts/` templates + `cache:build` precompilation, rerankers, rate-limit/budget middlewares, observability telemetry, and `VectorStoreFake`.

---

## Command Line Interface (`swiftpy`)

SwiftPY provides a unified CLI entrypoint for common development and production workflows:

```bash
# Scaffold a new SwiftPY project
swiftpy new my_service

# Start the ASGI server (production default: reload=False)
swiftpy start --host 0.0.0.0 --port 8000 --workers 4

# Start local dev server with auto-reload
swiftpy start --reload

# Start the background queue worker & inspect/retry Dead Letter Queue jobs
swiftpy queue:work --queue default --sleep 1.0
swiftpy queue:failed --limit 50
swiftpy queue:retry --all
swiftpy queue:flush

# Run, inspect, rollback, reset, and seed database migrations
swiftpy migrate
swiftpy migrate:status
swiftpy migrate:rollback
swiftpy migrate:reset
swiftpy db:seed

# Scaffold migrations, models, requests, schemas, API keys, mailables, and more
swiftpy make:migration create_posts_table
swiftpy make:model Post --migration
swiftpy make:request RegisterRequest
swiftpy make:schema OrderSchema
swiftpy make:mail WelcomeEmail
swiftpy make:api-key --prefix sk_live_

# Create symbolic links from public/storage to storage/app/public
swiftpy storage:link

# Precompile configuration and prompt templates (bootstrap/cache/)
swiftpy cache:build

# Clear precompiled application caches
swiftpy cache:clear
```

---

## Configuration Reference

SwiftPY reads environment variables and YAML/Python configuration files with strict validation at runtime:

| Environment Variable | Config Path | Default | Description |
|---|---|---|---|
| `APP_ENV` | `app.env` | `"local"` | Environment (`"local"`, `"testing"`, `"production"`). In production, `debug=True` is prohibited. |
| `APP_DEBUG` | `app.debug` | `False` | Enable debug mode (`true`/`false`); fails closed in production if enabled. |
| `APP_KEY` | `app.key` | `""` | Primary `>= 32`-byte application secret key for HMAC queue/event-bus signing and signed local URLs. |
| `JWT_SECRET` | `auth.guards.jwt.secret` | `None` | Secret key for JWT HS256 signing (`>= 32` bytes required when non-debug/production). |
| `AUTH_KEY_PEPPER` | `auth.guards.api_key.pepper` | `None` | Server pepper for API key HMAC-SHA256 verification (`>= 32` bytes required when non-debug/production). |
| `FILESYSTEM_DISK` | `filesystems.default` | `"local"` | Default disk driver (`"local"`, `"public"`, `"s3"`). |
| `MAIL_MAILER` | `mail.default` | `"smtp"` | Default mail transport (`"smtp"`, `"sendmail"`, `"resend"`, `"sendgrid"`, `"mailgun"`, `"console"`, `"array"`). Templates resolve under `views/`. |
| — | `http.max_body_size` | `2097152` (2 MiB) | Maximum allowed HTTP request body size in bytes (configured via `Config` or `Application(...)` / route). Returns 413 when exceeded. |
| — | `http.max_content_length` | `None` | Optional strict `Content-Length` ceiling in bytes before buffering chunks (configured via `Config`, `Application(...)`, or per-route). |
| — | `http.max_json_depth` | `32` | Maximum allowed JSON nesting depth before rejecting with HTTP 400 (configured via `Config`, `Application(...)`, or per-route). |
| — | `websockets.allowed_origins` | `None` | Allowed WebSocket origins (`Config` or `Application(allowed_origins=...)`). When unset, enforces strict same-origin policy against `Host` (D-14). |
| — | `websockets.max_message_size` | `1048576` (1 MiB) | Maximum incoming WebSocket message size in bytes (`Config`); exceeding closes connection with code 1009. |
| `DB_CONNECTION` | `database.default` | `"postgresql"` | Default database connection key (`"postgresql"`). |
| `DB_SSL` | `database.connections.postgresql.ssl` | `"disable"` | PostgreSQL SSL mode (`"disable"`, `"require"`, `"verify-ca"`, `"verify-full"`). |
| `DB_SSL_CA_FILE` | `database.connections.postgresql.ssl_ca_file` | `""` | Filepath to CA certificate when `DB_SSL` is `"verify-ca"` or `"verify-full"`. |
| `CORS_ALLOWED_ORIGINS` | `cors.allowed_origins` | `[]` | Comma-separated list of exact allowed CORS origins (never pair `"*"` with `CORS_ALLOW_CREDENTIALS=true`). See the [CORS Guide](./documentations/2026-11-21-cors.md). |
| `CORS_ALLOW_CREDENTIALS` | `cors.allow_credentials` | `False` | Whether cross-origin requests may include credentials/cookies. |
| `CORS_MAX_AGE` | `cors.max_age` | `600` | Preflight `OPTIONS` cache duration (`Access-Control-Max-Age`) in seconds. |
| `AI_ENABLED` | `ai.enabled` | `True` | Master toggle for `swiftpy.ai` requests (raises `AIModuleDisabledError` when disabled). |
| `AI_DEFAULT_PROVIDER` | `ai.default` | `"openai"` | Default LLM provider (`"openai"` or `"anthropic"`) when not inferred from model name. |
| `OPENAI_API_KEY` | `ai.providers.openai.api_key` | `""` | OpenAI API key for chat, streaming, and embeddings. |
| `OPENAI_MODEL` | `ai.providers.openai.model` | `"gpt-4o"` | Default OpenAI chat model (`ANTHROPIC_MODEL` defaults to `"claude-3-5-sonnet-20241022"`). |
| `AI_VECTORSTORE_DEFAULT` | `ai.vectorstores.default` | `"in_memory"` | Default vector store adapter (`"in_memory"`, `"pgvector"`, `"qdrant"`, `"pinecone"`, `"chroma"`, `"lancedb"`). |

---

## Lifecycle Hooks & Application Architecture

### ServiceProvider Asynchronous Lifecycle
SwiftPY manages startup and shutdown sequences through registered `ServiceProvider` instances with strict error isolation and rollback guarantees (Decision `D-7`):

```python
from swiftpy.core.providers.base import ServiceProvider

class BillingServiceProvider(ServiceProvider):
    def register(self) -> None:
        # Register bindings, singletons, and factories into the IoC container
        self.container.singleton(PaymentGateway, StripeGateway)

    def boot(self) -> None:
        # Synchronous boot tasks after all providers are registered
        pass

    async def startup(self) -> None:
        # Asynchronous initialization (e.g. connecting to external APIs, warming caches)
        await self.container.resolve(PaymentGateway).connect()

    async def shutdown(self) -> None:
        # Graceful asynchronous cleanup executed in reverse registration order
        await self.container.resolve(PaymentGateway).disconnect()
```

When `app.startup()` runs (or ASGI lifespan starts), providers initialize sequentially. If any provider raises an error during `startup()`, already-started providers are immediately rolled back and shut down in reverse order. During server shutdown, database pools and Redis connections are closed cleanly.

### Queue Delivery Semantics & At-Least-Once Guarantees
Background jobs run with **at-least-once** delivery guarantees:
- **Visibility Leases (`pop_and_lease`)**: When a worker acquires a job, an atomic Lua script moves the job from the ready queue to that worker's private in-flight processing list and creates an expiration timestamp lease in a sorted set.
- **Crash Recovery (`reap_expired_leases`)**: If a worker process is killed or crashes mid-job, remaining workers automatically reap expired leases and re-queue the job (or move it to DLQ if max attempts were reached).
- **Idempotency Requirement**: Because transient network timeouts or crashes can cause a job to be re-delivered, all `Job.handle()` methods must be implemented idempotently.
- **Transactional Dispatch (`after_commit=True`)**: Calling `job.dispatch(after_commit=True)` defers pushing the job until the active database transaction successfully commits. If the transaction rolls back, the job is dropped.

---

## Breaking Changes & Migration Guide

Across the hardening and audit remediation phases, architectural decisions (`D-1`–`D-14` and approval gates `G-1`–`G-16`, summarized in [`CHANGELOG.md`](./CHANGELOG.md)) enforce secure, fail-closed contracts:

- **D-1 (JWT Secret Enforcement)**: Weak or missing JWT secrets (< 32 bytes) raise `ConfigurationError` in production environments; hardcoded fallback secret removed from `config/auth.py`.
- **D-2 (API Key Format & Server Pepper)**: API keys follow `<prefix>.<secret>` (e.g. `sk_live_<hex>.<secret>`), look up single row by indexed prefix, and verify constant-time HMAC-SHA256 requiring `AUTH_KEY_PEPPER` (min 32 bytes) in production.
- **D-3 (Queue Envelope Signing & Processing Leases)**: Redis queues utilize visibility timeout leases to prevent message loss on worker failure. Payloads are signed with HMAC-SHA256 using `APP_KEY` and restricted to trusted namespaces (`get_trusted_namespaces()`).
- **D-4 (`__fillable__` Secure Default)**: Mass assignment denies all fields by default unless `__fillable__` or `__guarded__` is explicitly declared on the model (`__guarded__ = []` opt-out).
- **D-5 (`Model.to_dict()`, `__hidden__`, and `__repr__`)**: Models support `__hidden__` to exclude fields from serialization; `__repr__` displays only the primary key to prevent credential leakage.
- **D-6 (Route Precedence)**: Static route segments take precedence over dynamic parameter segments regardless of registration order.
- **D-7 (Container Re-binding Semantics)**: Re-binding a singleton into the container replaces any cached resolved instance and logs at debug level.
- **D-8 (Config Environment Variable Coercion)**: Coercion preserves strings unless the default type is known (e.g. `"0012"` is not silently cast to integer `12`).
- **D-9 (`FormRequest` Fail-Closed Validation)**: If `rules()` raises `TypeError` or any exception, the request fails closed with `ConfigurationError` (HTTP 500) rather than falling back to unvalidated `{}`.
- **D-10 (`update()`/`delete()` Without `WHERE`)**: Mass write/delete queries without a `WHERE` clause raise `QueryError` unless `.allow_all()` is explicitly chained.
- **G-1 (`swiftpy/ai` & Wheel Packaging)**: `swiftpy/ai` (LLM clients, embeddings, vector stores, RAG pipelines, agents, tools, prompt templates, observability), `swiftpy/admin`, `swiftpy/mail`, `swiftpy/filesystem`, and `swiftpy/validation` are fully implemented and distributed in the main `swiftpy-framework` wheel (dev test utilities remain in `swiftpy/testing`). `swiftpy/__init__.py` uses PEP 562 lazy exports so lightweight CLI/app imports avoid heavy optional subpackages until accessed.
- **G-16 (`dict` Auto-JSON & Cache Cleanup)**: Route handlers returning plain `dict` or `list` values are automatically serialized to JSON; legacy unbootstrapped `RouteCache` was removed (`swiftpy cache:build` compiles `config.json` and `prompts.json`); `/docs/` and `AGENTS.md` remain gitignored.
- **D-12 (Repository Hygiene)**: `.coverage` and cache artifacts untracked from git; `docs/`, `AGENTS.md`, and `DEVLOG.md` maintained untracked at repo root.
- **D-13 (Default `max_body_size`)**: Default 2 MiB request body limit (`413 Payload Too Large`) enforced across all buffered and streaming HTTP requests, configurable per app and per route.
- **D-14 (WebSocket Origin Validation)**: Defaults to strict same-origin checking against the `Host` header unless `WEBSOCKETS_ALLOWED_ORIGINS` is explicitly configured.
- **CLI Development Server Default (`swiftpy start`)**: `--reload` defaults to `False` for production safety (Decision under `P10-C1`; pass `--reload` for dev hot-reloading; `--reload` is refused when `--workers > 1`).

---

## Project Structure

SwiftPY follows a clean, intuitive layout inspired by modern application architectures:

```text
├── app/
│   ├── exceptions/         # Application-level exceptions
│   ├── http/               # Controllers, middleware, and request handlers
│   ├── models/             # Active Record ORM models
│   ├── providers/          # Service providers (auto-discovered on boot)
│   │   ├── app_service_provider.py
│   │   └── database_service_provider.py
│   ├── schemas/            # Typed dataclasses & request/response DTOs
│   └── services/           # Domain business logic
├── config/                 # Environment & framework configuration
│   ├── ai.py
│   ├── app.py
│   ├── auth.py
│   ├── cache.py
│   ├── cors.py
│   ├── database.py
│   ├── events.py
│   ├── filesystems.py
│   ├── logging.py
│   ├── mail.py
│   └── queue.py
├── database/
│   └── migrations/         # Timestamped database migrations
├── views/                  # Sandboxed Jinja2 HTML templates (admin/, mail/)
├── swiftpy/                # SwiftPY framework core
│   ├── admin/              # Auto-introspecting admin site, RBAC & session auth
│   ├── ai/                 # AI client, embeddings, vectorstores, RAG, agents, tools, prompts
│   ├── auth/               # JWT issuer, API key guards, token blacklist
│   ├── cache/              # Redis and memory cache drivers
│   ├── console/            # CLI commands, scaffolding (new/make:*), app loader
│   ├── core/               # Application kernel, DI container, config repository
│   ├── database/           # Pool, query builder, ORM, migrations, seeders, advisory lock
│   ├── events/             # Signed Redis event bus and local dispatcher
│   ├── filesystem/         # Multi-disk Storage (local, public, s3) and uploads
│   ├── helpers/            # Inflection (pluralize/snake_case), global helpers
│   ├── http/               # Router, request/response, middleware pipeline
│   ├── logging/            # Log facade, LogManager, async QueueHandler/QueueListener drivers
│   ├── mail/               # Mailables, SMTP/Sendmail/API transports, templates
│   ├── queue/              # At-least-once queue workers, job serialization
│   ├── support/            # Shared identifiers, keys, Redis config, string inflection
│   ├── testing/            # TestClient, TestCase, AIFake, MailFake, VectorStoreFake, DB fixtures
│   ├── validation/         # FormRequest, fail-closed Validator, builtin & DB rules
│   └── websockets/         # WebSocket manager, connections, broadcast
├── tests/                  # Automated pytest suite
├── docker-compose.yml      # Hardened local PostgreSQL & Redis environment
├── Dockerfile              # Multi-stage production container
├── main.py                 # Application entrypoint
└── pyproject.toml          # Project configuration & dependencies
```

---

## Development Setup

### Prerequisites
- Python 3.12+ (`uv` recommended)
- Docker and Docker Compose

### 1. Clone & Start Containers
```bash
git clone https://github.com/obrainwave/swiftpy.git
cd swiftpy
cp .env.example .env  # set strong APP_KEY, DB_PASSWORD, REDIS_PASSWORD, JWT_SECRET, AUTH_KEY_PEPPER
docker compose up -d
```

### 2. Run Migrations
```bash
docker compose exec app swiftpy migrate
```

### 3. Run Test Suites & Quality Checks
```bash
# Run full pytest test suite (requires PostgreSQL + Redis running)
uv run pytest -vv -ra --timeout=120 --timeout-method=thread

# Static type checking with Mypy in strict mode
uv run --with jinja2 --with aiosmtplib mypy swiftpy
uv run mypy --explicit-package-bases .

# Linting with Ruff
uv run ruff check .
```

---

## License

SwiftPY is open-source software licensed under the [MIT License](./LICENSE).
