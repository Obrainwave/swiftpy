# SwiftPY

**An ASGI-native, Eloquent-style Python web framework combining the developer experience of FastAPI with the elegance and structure of Laravel.**

[![CI](https://github.com/your-org/swiftpy/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/your-org/swiftpy/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-blue.svg)](./DEVLOG.md)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Types: Mypy Strict](https://img.shields.io/badge/types-mypy%20strict-blue.svg)](https://mypy-lang.org/)

---

## Overview

SwiftPY brings the joy and productivity of Laravel to the modern asynchronous Python ecosystem. It eliminates boilerplate and fragmented third-party glue code by providing a unified, cohesive framework built natively on Python 3.12+ and high-performance [`asyncpg`](https://github.com/MagicStack/asyncpg).

- **FastAPI-Grade Simplicity**: Clean, intuitive entrypoint (`app = Application()`), async-first request/response cycle, automatic path parameter type coercion, and scoped route groups with middleware pipelines.
- **Eloquent-Caliber Active Record ORM**: Typed models declaring class attributes via standard Python type hints, automatic irregular-aware table pluralization, dirty-tracking `save()`, expressive relationships (`HasMany`, `BelongsTo`, `HasOne`, `BelongsToMany`), and eager loading with strict N+1 query prevention.
- **Database Engine & Native Transactions**: Connection pool management, automatic transaction propagation across context variables, nested transaction savepoints, and strict SQL injection prevention with full identifier quoting for reserved words.
- **Fluent Migration DSL & Schema Builder**: Laravel-inspired `Blueprint` table definitions, safe statement ordering (drops executed before adds), column comments, unsigned checks, composite keys, and distributed PostgreSQL advisory locks.
- **Zero-Config Provider Auto-Discovery**: Automatic service provider discovery in `app/providers/` that registers container dependencies behind the scenes without manual entrypoint registration.
- **Developer CLI (`swiftpy`)**: Built-in CLI for running migrations, rolling back batches, inspecting migration status, scaffolding typed migrations, and starting the ASGI dev server.

---

## Status & Roadmap

| Milestone | Component Focus | Status |
|---|---|---|
| **Phase 1: Core Foundation** | ASGI HTTP kernel, async router, route groups, IoC container, task isolation, middleware pipeline, config repository | ✅ Completed |
| **Phase 2: Data & ORM Essentials** | Connection pool, query builder, Active Record ORM, relationships, eager loading, migrations DSL, advisory locking, CLI | ✅ Completed |
| **Phase 3: Application Infrastructure** | Queue workers, event dispatcher, cache drivers (Redis), WebSockets | 🚧 In Progress |
| **Phase 4: AI Native Module** | LLM streaming clients, tool calling, embeddings, vector stores, RAG pipelines | ⬜ Planned |
| **Phase 5: Polish & PyPI Release** | Performance benchmarks, security audit, comprehensive documentation | ⬜ Planned |

*See [`DEVLOG.md`](./DEVLOG.md) for detailed test reports and verified architectural guarantees.*

---

## What It Looks Like

### 1. Minimal Application Entrypoint

No ugly provider lists or manual bootstrapping required—create your application in one line:

```python
# main.py
from app.http.middleware.test_middleware import TestMiddleware
from swiftpy import Application, Request, Response, RouteGroup, json_response

app = Application(title="SwiftPY Store API", version="1.0.0")


@app.get("/ping")
async def ping(request: Request) -> Response:
    return json_response({"message": "pong"})


@app.group("/api/v1", middleware=[TestMiddleware])
def v1_routes(group: RouteGroup) -> None:
    @group.get("/users/{user_id}")
    async def show_user(request: Request, user_id: int) -> Response:
        return json_response({"user_id": user_id, "ok": True})
```

### 2. Active Record Models & Relationships

Declare models using Python type hints. Unloaded relationships strictly raise `RelationNotLoadedError` to eliminate silent N+1 queries; use `.with_()` for explicit, high-performance eager loading:

```python
# app/models/user.py
from datetime import datetime
from swiftpy.database.orm import Model, HasMany, HasOne


class User(Model):
    __fillable__ = ["name", "email"]

    id: int
    name: str
    email: str
    created_at: datetime
    updated_at: datetime

    posts: HasMany = HasMany("Post")
    profile: HasOne = HasOne("Profile")


# Querying and eager loading:
users = await User.with_("posts", "profile").where("name", "Alice").get()
for user in users:
    print(user.name, len(user.posts), user.profile.bio)

# Dirty-tracking save (updates only changed attributes and maintains updated_at):
user = await User.find(1)
user.name = "Alice Cooper"
await user.save()
```

### 3. Fluent Migrations & Schema Builder

Define and alter tables using the expressive `Blueprint` DSL:

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

Define background jobs with automatic JSON serialization, retry with backoff, and dead letter queue routing:

```python
# app/jobs/send_welcome_email.py
from swiftpy.queue import Job


class SendWelcomeEmail(Job):
    tries = 3
    backoff = [1.0, 5.0, 15.0]

    def __init__(self, user_id: int, email: str) -> None:
        super().__init__(user_id, email)
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

SwiftPY provides a unified CLI entrypoint for common development workflows:

```bash
# Create a new migration with smart table name inference
swiftpy make:migration create_posts_table
swiftpy make:migration add_status_to_orders

# Run all outstanding migrations
swiftpy migrate

# Inspect migration status and batch history
swiftpy migrate:status

# Rollback the last migration batch
swiftpy migrate:rollback

# Start the queue worker process
swiftpy queue:work --queue default --sleep 1.0

# Start the ASGI development server with auto-reload
swiftpy start
```

---

## Project Structure

SwiftPY follows a clean, intuitive layout inspired by Laravel:

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
│   ├── console/            # CLI commands, argument parsing, app loader
│   ├── core/               # Application kernel, DI container, config repository
│   ├── database/           # Pool, query builder, ORM, migrations, advisory lock
│   ├── helpers/            # Inflection (pluralize/snake_case), global helpers
│   └── http/               # Router, request/response, middleware pipeline
├── tests/                  # Pytest test suites (unit, integration, stress, migrations)
├── docker-compose.yml      # Local PostgreSQL & Redis environment
├── main.py                 # Application entrypoint
└── pyproject.toml          # Project configuration & dependencies
```

---

## Development Setup

### Prerequisites
- Python 3.12+
- Docker and Docker Compose
- [`uv`](https://github.com/astral-sh/uv) (recommended Python package manager)

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
# Run full pytest test suite (56 tests)
docker compose exec app uv run pytest

# Run isolated migration system & CLI test suite
docker compose exec app python tests/migrations/test_migrations.py

# Static type checking with Mypy in strict mode
docker compose exec app uv run mypy swiftpy main.py

# Linting and formatting with Ruff
docker compose exec app uv run ruff check .
docker compose exec app uv run ruff format --check .
```

---

## License

SwiftPY is open-source software licensed under the [MIT License](./LICENSE).
