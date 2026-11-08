# SwiftPY

**An ASGI-native, Eloquent-style Python web framework combining the developer experience of FastAPI with the elegance and structure of Laravel.**

[![CI](https://github.com/your-org/swiftpy/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/your-org/swiftpy/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: Production-Hardened](https://img.shields.io/badge/status-production--hardened-green.svg)](./DEVLOG.md)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Types: Mypy Strict](https://img.shields.io/badge/types-mypy%20strict-blue.svg)](https://mypy-lang.org/)
[![Tests: 600+ Passing](https://img.shields.io/badge/tests-600%2B%20passing-brightgreen.svg)](./DEVLOG.md)

---

## Overview

SwiftPY brings the joy and productivity of Laravel to the modern asynchronous Python ecosystem. It eliminates boilerplate and fragmented third-party glue code by providing a unified, cohesive framework built natively on Python 3.12+ and high-performance [`asyncpg`](https://github.com/MagicStack/asyncpg).

- **FastAPI-Grade Simplicity**: Clean, intuitive entrypoint (`app = Application()`), async-first request/response cycle, automatic path parameter type coercion, and scoped route groups with middleware pipelines.
- **Eloquent-Caliber Active Record ORM**: Typed models declaring class attributes via standard Python type hints, automatic irregular-aware table pluralization, dirty-tracking `save()`, expressive relationships (`HasMany`, `BelongsTo`, `HasOne`, `BelongsToMany`), and eager loading with strict N+1 query prevention (via model-level `prevent_lazy_loading = True`, raising `LazyLoadingDisabledError` on un-eager-loaded accesses).
- **Database Engine & Native Transactions**: Connection pool management, automatic transaction propagation across context variables, nested transaction savepoints, and strict SQL injection prevention with full identifier quoting for reserved words.
- **Fluent Migration DSL & Schema Builder**: Laravel-inspired `Blueprint` table definitions, safe statement ordering (drops executed before adds), column comments, unsigned checks, composite keys, and distributed PostgreSQL advisory locks.
- **At-Least-Once Queues & Signed Bus**: Robust background jobs with Redis visibility timeout leases, HMAC-SHA256 envelope signing, dead-letter routing, and a cryptographically protected cross-worker event bus.
- **WebSocket Gateway**: Structured WebSocket connection management, topic subscriptions, broadcast fan-out, ping/pong heartbeats, and strict same-origin handshake validation.
- **Zero-Config Provider Auto-Discovery**: Automatic service provider discovery in `app/providers/` that registers container dependencies behind the scenes without manual entrypoint registration.
- **Developer CLI (`swiftpy`)**: Built-in CLI for running migrations, rolling back batches, inspecting migration status, scaffolding typed migrations, running queue workers, and starting the ASGI dev server.

---

## Status & Roadmap

All 10 core architectural and security hardening phases have been completed and verified against a comprehensive 600+ test suite:

| Milestone | Component Focus | Status |
|---|---|---|
| **Phase 0: Testing Harness & Quality Baseline** | CI harness, container test environments, strict ruff/mypy configurations | ✅ Completed |
| **Phase 1: Architecture & Foundation** | ASGI HTTP kernel, async router, route groups, IoC container, task isolation, middleware pipeline | ✅ Completed |
| **Phase 2: Authentication & Security** | Cryptographic JWT issuer, HMAC API key guards, server-side peppers, constant-time comparisons | ✅ Completed |
| **Phase 3: Database & Identifier Safety** | asyncpg connection pool, strict SQL identifier validation/quoting, nested transactions | ✅ Completed |
| **Phase 4: HTTP Kernel & Error Pipeline** | Request streaming limits (2 MiB default), typed response factories, fail-closed error pipeline | ✅ Completed |
| **Phase 5: Concurrency & DI Scoping** | ContextVar-backed scoped DI, async lifecycle hooks (`startup`/`shutdown`), graceful teardown | ✅ Completed |
| **Phase 6: Queues & Reliability** | At-least-once Redis queue leases, HMAC envelope signing, namespace allowlists, signed event bus | ✅ Completed |
| **Phase 7: Active Record ORM Hardening** | Mass assignment protection (`__fillable__`/`__guarded__`), eager loading N+1 prevention, dirty tracking | ✅ Completed |
| **Phase 8: Validation DSL Hardening** | Fail-closed FormRequest validation, hardened rule composition, input sanitization | ✅ Completed |
| **Phase 9: Real-time WebSockets** | Handshake validation, same-origin protection (D-14), bounded broadcast fan-out, lifecycle hooks | ✅ Completed |
| **Phase 10: Infra, CI & Hygiene** | Multi-stage Docker, pinned CI workflow, hardened compose, repo hygiene, CLI options | ✅ Completed |

*See [`DEVLOG.md`](./DEVLOG.md) for detailed test reports and verified architectural guarantees.*

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
- **Precompiled Production Cache (`swiftpy cache:build`)**: Precompiles routes, configuration, and prompt templates into serialized JSON files (`bootstrap/cache/`) to eliminate startup file parsing overhead.

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
async def ping(request: Request) -> Response:
    return json_response({"message": "pong"})


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
from typing import ClassVar
from swiftpy.database.orm import HasMany, HasOne, Model


class Post(Model):
    __table__ = "posts"
    __fillable__: ClassVar[list[str]] = ["title", "user_id"]

    id: int
    title: str
    user_id: int


class Profile(Model):
    __table__ = "profiles"
    __fillable__: ClassVar[list[str]] = ["bio", "user_id"]

    id: int
    bio: str
    user_id: int


class User(Model):
    __table__ = "users"
    __fillable__: ClassVar[list[str]] = ["name", "email"]

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

### 3. Fluent Migrations & Schema Builder

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

### 4. Asynchronous Queues & Background Jobs

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

---

## Command Line Interface (`swiftpy`)

SwiftPY provides a unified CLI entrypoint for common development and production workflows:

```bash
# Start the ASGI server (production default: reload=False)
swiftpy start --host 0.0.0.0 --port 8000 --workers 4

# Start local dev server with auto-reload
swiftpy start --reload

# Start the background queue worker
swiftpy queue:work --queue default --sleep 1.0

# Run pending database migrations
swiftpy migrate

# Inspect migration status and batch history
swiftpy migrate:status

# Rollback the last migration batch
swiftpy migrate:rollback

# Scaffold a new migration with smart table name inference
swiftpy make:migration create_posts_table
swiftpy make:migration add_status_to_orders

# Precompile routes, configuration, and prompt templates for zero cold-start latency
swiftpy cache:build

# Clear all precompiled application caches
swiftpy cache:clear
```

---

## Configuration Reference

SwiftPY reads environment variables and YAML/Python configuration files with strict validation at runtime:

| Environment Variable | Config Path | Default | Description |
|---|---|---|---|
| `APP_ENV` | `app.env` | `"development"` | Environment (`development`, `testing`, `production`). In production, `debug=True` is prohibited. |
| `APP_KEY` | `app.key` | None | Primary 32+ byte application secret key used for HMAC envelope signing on queue jobs and event bus. |
| `JWT_SECRET` | `auth.jwt.secret` | None | Secret key for JWT HS256 signing (at least 32 bytes required in production). |
| `AUTH_KEY_PEPPER` | `auth.guards.api_key.pepper` | None | Secret pepper for API key hashing (at least 32 bytes required in production). |
| `HTTP_MAX_BODY_SIZE` | `http.max_body_size` | `2097152` (2 MiB) | Maximum allowed HTTP request body size in bytes. Returns 413 when exceeded. |
| `WEBSOCKETS_ALLOWED_ORIGINS` | `websockets.allowed_origins` | None | Comma-separated list of allowed origins. When unset, enforces strict same-origin policy against `Host` (D-14). |
| `WEBSOCKETS_MAX_MESSAGE_SIZE` | `websockets.max_message_size` | `1048576` (1 MiB) | Maximum allowed incoming WebSocket message size. Exceeding closes connection with code 1009. |
| `DB_SSL` | `database.connections.postgresql.ssl` | `"disable"` | PostgreSQL SSL mode (`disable`, `require`, `verify-ca`, `verify-full`). |
| `DB_SSL_CA_FILE` | `database.connections.postgresql.ssl_ca_file` | None | Filepath to CA certificate when `DB_SSL` is `verify-ca` or `verify-full`. |
| `CORS_ALLOWED_ORIGINS` | `cors.allowed_origins` | `[]` | Comma-separated list of allowed CORS origins. |
| `CORS_ALLOW_CREDENTIALS` | `cors.allow_credentials` | `False` | Whether cross-origin requests may include credentials/cookies. |

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

Across the hardening phases, all architectural decisions were aligned with the specifications in `docs/code_review_fix_plan.md`:

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
- **D-11 (Placeholder Module Deprecation)**: Unimplemented placeholder modules (`swiftpy/ai/**`, `config/ai.py`, `config/logging.py`, etc.) are documented with `"""Not implemented."""` and excluded from wheel distributions.
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
│   └── services/           # Domain business logic
├── config/                 # Environment & framework configuration
│   ├── app.py
│   └── database.py
├── database/
│   └── migrations/         # Timestamped database migrations
├── swiftpy/                # SwiftPY framework core
│   ├── auth/               # JWT issuer, API key guards, token blacklist
│   ├── cache/              # Redis and memory cache drivers
│   ├── console/            # CLI commands, argument parsing, app loader
│   ├── core/               # Application kernel, DI container, config repository
│   ├── database/           # Pool, query builder, ORM, migrations, advisory lock
│   ├── events/             # Signed Redis event bus and local dispatcher
│   ├── helpers/            # Inflection (pluralize/snake_case), global helpers
│   ├── http/               # Router, request/response, middleware pipeline
│   ├── queue/              # At-least-once queue workers, job serialization
│   └── websockets/         # WebSocket manager, connections, broadcast
├── tests/                  # Pytest test suites (600+ tests)
├── docker-compose.yml      # Hardened local PostgreSQL & Redis environment
├── Dockerfile              # Multi-stage production container
├── main.py                 # Application entrypoint
└── pyproject.toml          # Project configuration & dependencies
```

---

## Development Setup

### Prerequisites
- Python 3.12+
- Docker and Docker Compose

### 1. Clone & Start Containers
```bash
git clone https://github.com/your-org/swiftpy.git
cd swiftpy
docker compose up -d
```

### 2. Run Migrations
```bash
docker compose exec app swiftpy migrate
```

### 3. Run Test Suites & Quality Checks
```bash
# Run full pytest test suite (600+ tests)
docker compose exec app pytest -q -p no:cacheprovider

# Static type checking with Mypy in strict mode
docker compose exec app mypy .

# Linting and formatting with Ruff
docker compose exec app ruff check .
docker compose exec app ruff format --check .
```

---

## License

SwiftPY is open-source software licensed under the [MIT License](./LICENSE).
