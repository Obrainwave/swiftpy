# Developer CLI (`swiftpy`) & Testing Utilities

SwiftPY pairs a full developer CLI (`swiftpy`) for project scaffolding, migrations, cache compilation, queue workers, and server startup with an async testing harness (`TestClient`, `TestResponse`, `TestCase`, `db_transaction`, `Mail.fake()`, `AIFake`, and `VectorStoreFake`).

---

## Part 1: The `swiftpy` CLI Reference

Invoke the CLI through your project environment (`uv run swiftpy <command>` or `swiftpy <command>` inside an activated virtualenv):

```bash
uv run swiftpy --version
uv run swiftpy --help
```

### Project Bootstrap & Development Server

| Command | Options & Arguments | Purpose |
|---|---|---|
| `swiftpy new <project_name>` | `[--force]` | Creates a complete starter SwiftPY project (`app/`, `config/`, `database/`, `routes/`, `storage/`, `main.py`, `.env`, `.env.example`, `pyproject.toml`, `README.md`). Fails cleanly if directory exists unless `--force` is passed. |
| `swiftpy start [target]` | `[--host] [--port] [--workers] [--reload] [--env-file] [--access-log/--no-access-log] [--log-level]` | Boots your ASGI application via Uvicorn (defaults target to `main:app`). Always verify worker concurrency and `.env` configuration before using multiple workers. |

### Scaffolding Generators (`swiftpy make:*`)

Use the `make:*` commands to generate typed stubs conforming to SwiftPY's conventions (`--path <dir>` overrides the default folder; `--force` overwrites existing files):

| Command | Default Destination | Extra Options |
|---|---|---|
| `swiftpy make:model <Name>` | `app/models/` | `-m` / `--migration`, `--table <table>`, `--migration-path <dir>`, `--path`, `--force` |
| `swiftpy make:migration <name>` | `database/migrations/` | `--create <table>`, `--table <table>`, `--path`, `--force` |
| `swiftpy make:controller <Name>` | `app/http/controllers/` | `--path`, `--force` |
| `swiftpy make:resource <Name>` | `app/http/resources/` | `--path`, `--force` |
| `swiftpy make:request <Name>` | `app/http/requests/` | `--path`, `--force` |
| `swiftpy make:schema <Name>` | `app/schemas/` | `--path`, `--force` |
| `swiftpy make:service <Name>` | `app/services/` | `--path`, `--force` |
| `swiftpy make:repository <Name>` | `app/repositories/` | `--path`, `--force` |
| `swiftpy make:middleware <Name>` | `app/http/middleware/` | `--path`, `--force` |
| `swiftpy make:mail <Name>` | `app/mail/` | `--path`, `--force` |
| `swiftpy make:job <Name>` | `app/jobs/` | `--path`, `--force` |
| `swiftpy make:event <Name>` | `app/events/` | `--path`, `--force` |
| `swiftpy make:listener <Name>` | `app/listeners/` | `--path`, `--force` |
| `swiftpy make:provider <Name>` | `app/providers/` | `--path`, `--force` |
| `swiftpy make:seeder <Name>` | `database/seeders/` | `--path`, `--force` |
| `swiftpy make:agent <Name>` | `app/ai/agents/` | `--path`, `--force` |
| `swiftpy make:tool <Name>` | `app/ai/tools/` | `--path`, `--force` |
| `swiftpy make:prompt <name>` | `prompts/` (`summarise`, `summarise.v2`) | `--path`, `--force` |
| `swiftpy make:api-key` | Config/table guidance + key generation | `--prefix`, `--pepper`, `-m` / `--migration`, `--path`, `--force` |

> [!TIP]
> Remember that `swiftpy make:schema` and starter projects scaffold strongly typed `swiftpy.schemas.BaseSchema` DTOs in `app/schemas/` (see [Application Schemas (`app/schemas`)](2026-11-19-application-schemas.md)), and `swiftpy make:repository` creates zero-boilerplate repositories (`model = YourModel`) with full Pyrefly/Mypy inference out of the box (see [Active Record ORM & Relationships](2026-11-15-orm-models-and-relationships.md)).

### Migrations & Database Seeders

| Command | Options | Behavior |
|---|---|---|
| `swiftpy migrate` | `[--app] [--path] [--step] [--force]` | Runs pending migrations in `database/migrations/` inside advisory locks. |
| `swiftpy migrate:rollback` | `[--app] [--path] [--step] [--force]` | Reverts the last migration batch (or `--step` count). |
| `swiftpy migrate:reset` | `[--app] [--path] [--force]` | Rolls back all applied migrations. |
| `swiftpy migrate:status` | `[--app] [--path]` | Prints applied vs. pending migration status and batch numbers. |
| `swiftpy db:seed` | `[--class] [--no-transaction] [--app] [--path] [--force]` | Executes database seeders (`DatabaseSeeder` by default, or specific `--class`). |

Pass `--app main:app` so migrations and seeders boot your full application `Config`, service providers, and database pool cleanly.

### Queues, Task Scheduler, Caching & Storage Commands

```bash
# Precompile config/*.py and prompts into bootstrap/cache/
uv run swiftpy cache:build --app main:app
uv run swiftpy cache:clear

# Symlink storage/app/public -> public/storage
uv run swiftpy storage:link

# Run queue worker & manage failed jobs
uv run swiftpy queue:work --app main:app --queue default --concurrency 4
uv run swiftpy queue:failed --app main:app --queue default --limit 20
uv run swiftpy queue:retry all --app main:app --queue default
uv run swiftpy queue:flush --app main:app --queue default

# Inspect and run scheduled tasks (cron, intervals, and one-time jobs)
uv run swiftpy schedule:list --app main:app --json
uv run swiftpy schedule:run --app main:app
uv run swiftpy schedule:work --app main:app --sleep 1.0
```

Full option summary:

- `cache:build` (alias `cache build`): `[--app] [--cache-dir]`
- `cache:clear` (alias `cache clear`): `[--cache-dir]`
- `storage:link` (alias `storage link`): `[--force]`
- `queue:work`: `[--queue] [--sleep] [--max-jobs] [--stop-when-empty] [--timeout] [--concurrency] [--prefix] [--app]`
- `queue:failed`: `[--queue] [--limit] [--prefix] [--app]`
- `queue:retry <id|all>`: `[--queue] [--prefix] [--app]`
- `queue:flush`: `[--queue] [--prefix] [--app]`
- `schedule:list`: `[--app] [--json] [--prefix]`
- `schedule:run`: `[--app] [--limit] [--prefix]`
- `schedule:work`: `[--app] [--sleep] [--max-ticks] [--prefix]`

---

## Part 2: Testing Utilities (`swiftpy.testing`)

Quick imports:

```python
from swiftpy.testing import (
    AIFake,
    TestCase,
    TestClient,
    TestResponse,
    VectorStoreFake,
    WebSocketTestSession,
    db_transaction,
    transactional_test,
)
```

### Async HTTP & WebSocket Testing with `TestClient`

`TestClient` executes ASGI requests and lifespan hooks in-process without starting a live TCP socket:

```python
from __future__ import annotations

import pytest

from swiftpy import Application, Request
from swiftpy.testing import TestClient


@pytest.mark.asyncio
async def test_health_and_authenticated_profile() -> None:
    app = Application(
        config={
            "app": {"name": "Orders API", "env": "testing", "debug": True},
        },
        auto_discover_providers=False,
    )

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        return {"status": "ok"}

    async with TestClient(app) as client:
        resp = await client.get("/health")
        resp.assert_ok().assert_json({"status": "ok"})
```

#### Useful `TestClient` Helpers

- `async with TestClient(app) as client:` — runs `startup()` on entry and `shutdown()` cleanly on exit.
- Auth & header derivation (returns a new derived `TestClient`):
  - `client.with_headers({"X-Tenant": "acme"})`
  - `client.with_token(access_token)` (attaches `Authorization: Bearer <token>`)
  - `client.with_api_key(raw_key, header="X-API-Key")`
- Request methods (all `async`): `.get()`, `.post()`, `.put()`, `.patch()`, `.delete()`, `.head()`, `.options()`, `.request()`.
- WebSockets:
  ```python
  async with client.websocket_connect("/ws/chat", subprotocols=["bearer", jwt_token]) as ws:
      await ws.send_json({"text": "hello"})
      msg = await ws.receive_json()
      await ws.close()
  ```
  `WebSocketTestSession` provides `send_text`, `send_bytes`, `send_json`, `receive_raw`, `receive_text`, `receive_bytes`, `receive_json`, and `close`.

#### Fluent `TestResponse` Assertions

Inspect `.status_code`, `.headers`, `.body`, `.text` (`str` property), and `.json()`, or chain fluent assertion helpers:

- Status assertions: `assert_status(code)`, `assert_ok()` (`200`), `assert_created()` (`201`), `assert_no_content()` (`204`), `assert_bad_request()` (`400`), `assert_unauthorized()` (`401`), `assert_forbidden()` (`403`), `assert_not_found()` (`404`), `assert_method_not_allowed()` (`405`), `assert_validation_error(field=None)` (`422`), `assert_server_error()` (`500`).
- Payload & header assertions: `assert_json(expected)`, `assert_json_fragment(fragment)`, `assert_header(name, expected=None)`.

### Class-Based Tests (`TestCase`)

Subclass `TestCase` when grouping tests around a shared application factory:

```python
from __future__ import annotations

import pytest

from swiftpy import Application
from swiftpy.testing import TestCase


class OrdersApiCase(TestCase):
    def create_app(self) -> Application:
        app = Application(
            config={"app": {"name": "Orders", "env": "testing", "debug": True}},
            auto_discover_providers=False,
        )

        @app.get("/ping")
        async def ping() -> dict[str, bool]:
            return {"pong": True}

        return app


@pytest.mark.asyncio
async def test_orders_ping() -> None:
    async with OrdersApiCase() as case:
        resp = await case.get("/ping")
        resp.assert_ok().assert_json({"pong": True})
```

### Automatic Per-Test Database Rollback (`db_transaction` & `transactional_test`)

Import `db_transaction` into your test module so pytest injects an isolated `TransactionConnection` per test and rolls it back automatically afterward — even without writing custom fixture boilerplate:

```python
from __future__ import annotations

import pytest

from swiftpy.database import Query
from swiftpy.testing import db_transaction  # noqa: F401  # pytest fixture import


@pytest.mark.asyncio
async def test_insert_is_rolled_back(db_transaction) -> None:
    await Query.table("users").insert({"name": "Test User", "email": "t@example.com", "password": "secret"})
    row = await Query.table("users").where("email", "t@example.com").first()
    assert row is not None
```

`transactional_test` provides the underlying async context manager if you want explicit transaction rollback blocks inside custom fixtures.

### Subsystem Fakes (`Mail.fake()`, `AIFake`, `VectorStoreFake`)

| Fake | Import | Main Assertions & Usage |
|---|---|---|
| `Mail.fake()` / `MailFake` | `from swiftpy.mail import Mail` | `fake = Mail.fake()` intercepts outbound emails (`assert_sent`, `assert_not_sent`, `assert_queued`, `assert_nothing_sent`, `reset`); call `Mail.restore()` when finished. |
| `AIFake` | `from swiftpy.testing import AIFake` | `with AIFake({"gpt-4o-mini": "mocked reply"}) as fake:` (or `embedding_fixtures=[[0.1, 0.2]]`) records LLM/embedding calls and provides `assert_called`, `assert_called_once`, `assert_not_called`, `assert_called_with`, `assert_embedding_called`, `assert_embedding_called_once`, `assert_embedding_not_called`, and `assert_embedding_called_with`. |
| `VectorStoreFake` | `from swiftpy.testing import VectorStoreFake` | In-memory vector store replacement (`VectorStoreFake(collection_name="docs", dimensions=2)`) with `recorded()`, `assert_called()`, `assert_upserted()`, `assert_searched()`, and `reset()`. |

> [!NOTE]
> **Testing `Storage`:** SwiftPY does **not** include a `StorageFake` class. For storage unit/integration tests, configure a temporary `StorageConfig` using `pytest`'s `tmp_path` as the `"local"` disk root so files stay isolated and automatically cleaned up.

### Static Type Checking (`pyrefly` & `mypy --strict`)

SwiftPY ships a PEP 561 `py.typed` marker alongside generic ORM, relationship (`RelationshipQueryBuilder[T]`, `SingleRelationshipQueryBuilder[T]`), and `@dataclass_transform` `BaseSchema` annotations:

```bash
# Type-check application code strictly with Mypy
uv run mypy --strict .

# Type-check with Pyrefly (verifying no implicit Any leaks)
uvx pyrefly check --error=no-any-return-implicit .
```

---

## Documentation Series Complete

You have reached the end of the core SwiftPY documentation series. Review the full guide index anytime in [`docs/2026-11-14_documentation_plan.md`](../docs/2026-11-14_documentation_plan.md).
