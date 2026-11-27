# Database Pool, Query Builder, Transactions & Migrations

SwiftPY provides an async-first PostgreSQL layer built on `asyncpg`: **`DatabasePool`** manages connection lifecycles automatically, **`Query.table(...)`** builds parameterized SQL with strict identifier validation, **`db.transaction()`** supports nested savepoints and post-commit callbacks, **`Paginator` / `SimplePaginator`** handle pagination, and **`Schema` / `Blueprint` / `Seeder`** manage database structure and seed data.

Quick imports from `swiftpy.database`:

```python
from swiftpy.database import (
    Blueprint,
    DatabasePool,
    Paginator,
    Query,
    QueryError,
    RecordNotFoundError,
    Schema,
    Seeder,
    SimplePaginator,
    db,
    escape_like,
    on_commit,
)
from swiftpy.database.migrations import Migration, raw
from swiftpy.database.query_builder import Column, Raw
```

---

## Configuration & Connection Pool (`DatabasePool`)

### Project Configuration (`config/database.py`)

When you generate a project with `swiftpy new`, `config/database.py` loads connection, pool, and timeout settings from `.env`:

```python
# config/database.py
from swiftpy import env

CONFIG: dict[str, object] = {
    "default": env("DB_CONNECTION", "postgresql"),
    "connections": {
        "postgresql": {
            "host": env("DB_HOST", "127.0.0.1"),
            "port": int(env("DB_PORT", "5432")),
            "database": env("DB_DATABASE", "swiftpy"),
            "user": env("DB_USERNAME", "postgres"),
            "password": env("DB_PASSWORD", ""),
            "ssl": env("DB_SSL", "disable"),
            "ssl_ca_file": env("DB_SSL_CA_FILE", ""),
        },
    },
    "pool": {
        "min_size": int(env("DB_POOL_MIN_SIZE", "5")),
        "max_size": int(env("DB_POOL_MAX_SIZE", "20")),
        "max_queries": int(env("DB_POOL_MAX_QUERIES", "50000")),
        "max_inactive_connection_lifetime": float(
            env("DB_POOL_MAX_INACTIVE_LIFETIME", "300.0")
        ),
        "command_timeout": float(env("DB_COMMAND_TIMEOUT", "60.0")),
    },
    "timeouts": {
        "connect": float(env("DB_CONNECT_TIMEOUT", "10.0")),
        "statement": float(env("DB_STATEMENT_TIMEOUT", "30.0")),
        "transaction": float(env("DB_TRANSACTION_TIMEOUT", "60.0")),
    },
}
```

### Automatic Startup & Shutdown Lifecycle

When database configuration is present, `Application` registers `DatabasePool` as a container singleton via `DatabaseServiceProvider` (`app/providers/database_service_provider.py`), runs `await pool.init()` during ASGI startup, and gracefully runs `await pool.close()` on shutdown. Minimal single-file apps with no `config/database.py` (or no `"database"` config key) do not initialize a pool.

If you need raw `asyncpg` pool access in a custom service or background script, inject `DatabasePool`:

```python
from __future__ import annotations

from swiftpy.database import DatabasePool


async def database_version(db_pool: DatabasePool) -> str:
    pool = db_pool.get_pool()
    async with pool.acquire() as conn:
        return str(await conn.fetchval("SELECT version()"))
```

### Production Security & SSL Modes

- **Non-empty password in production / non-debug:** `DatabasePool.init()` raises `ConfigurationError` if `password` is empty when `app.env` is `"production"`/`"prod"` or `app.debug` is `False`. Set `DB_PASSWORD` explicitly in `.env` or environment secrets.
- **PostgreSQL SSL (`DB_SSL` & `DB_SSL_CA_FILE`):** Supported `ssl` modes are `"disable"` (default), `"require"` (encrypted without certificate verification), `"verify-ca"` (verifies certificate against CA file), and `"verify-full"` (verifies certificate and hostname). Any unrecognized value raises `ConfigurationError`.
- **PostgreSQL session timeouts:** `database.timeouts.statement` sets `statement_timeout` (default `30.0` seconds) and `database.timeouts.transaction` sets `idle_in_transaction_session_timeout` (default `60.0` seconds in `config/database.py`).

---

## Fluent Query Builder (`Query.table(...)`)

Every query starts from `Query.table("table_name")`. All column/table identifiers are validated against SQL injection, and all dynamic values compile to numbered PostgreSQL parameters (`$1`, `$2`, ...).

### Selecting Columns, Joins & Distinct Rows

```python
from __future__ import annotations

from typing import Any

from swiftpy.database import Query
from swiftpy.database.query_builder import Column


async def fetch_recent_orders() -> list[dict[str, Any]]:
    return await (
        Query.table("orders")
        .select(
            "orders.id",
            "orders.total",
            "users.email",
            Column("orders.created_at", alias="ordered_at"),
        )
        .join("users", "users.id", "=", "orders.user_id")  # or .left_join(...) / .right_join(...)
        .where("orders.status", "paid")
        .latest("orders.created_at")
        .limit(20)
        .get()
    )
```

Useful projection & join helpers:

- `.distinct()` (`SELECT DISTINCT`) or `.distinct("customer_id")` (PostgreSQL `SELECT DISTINCT ON ("customer_id")`).
- `.select_raw(sql, bindings=None)` — trusted SQL expression using `?` placeholders compiled into `$1, $2, ...`.
- `.select_count(column="*", alias=None)`, `.select_sum(column, alias=None)`, `.select_avg(...)`, `.select_min(...)`, `.select_max(...)`.
- `.join(table, first, op, second, type_="INNER")`, `.left_join(...)`, `.right_join(...)` — supports `=`, `!=`, `<`, `>`, `<=`, `>=` and `INNER`, `LEFT`, `RIGHT`, `FULL`.

### Filtering (`WHERE`), Subqueries, LIKE Escaping & Conditionals

```python
from __future__ import annotations

from typing import Any

from swiftpy.database import Query


async def search_users(
    role_filter: str | None,
    search_term: str | None,
) -> list[dict[str, Any]]:
    active_org_ids = Query.table("organizations").select("id").where("active", True)

    query = (
        Query.table("users")
        .select("id", "name", "email", "role")
        .where_null("deleted_at")
        .where_in("org_id", active_org_ids)  # Subquery or list/tuple
        .where_group(
            lambda q: q.where("status", "active").or_where("status", "trial")
        )
        .when(role_filter, lambda q, role: q.where("role", role))
        .when(
            search_term,
            lambda q, term: q.where_ilike("email", str(term), match="contains"),
        )
        .order_by("name", "ASC")
    )
    return await query.get()
```

Key filtering methods:

- **Equality & comparison:** `.where(col, val)` (`=`), `.where(col, op, val)` (allows `=`, `!=`, `<>`, `<`, `>`, `<=`, `>=`, `LIKE`, `ILIKE`, `IN`, `NOT IN`), and `.or_where(...)`.
- **`NULL` handling:** `.where("deleted_at", None)` compiles to `IS NULL` and `.where("deleted_at", "!=", None)` compiles to `IS NOT NULL` (or use `.where_null()`, `.or_where_null()`, `.where_not_null()`, `.or_where_not_null()`). Passing `None` with `<`, `>`, `LIKE`, or `IN` raises `QueryError`.
- **Lists & subqueries:** `.where_in(col, values)`, `.or_where_in(...)`, `.where_not_in(...)`, `.or_where_not_in(...)`. Accepts any non-string `Sequence` (`list`, `tuple`, or custom `Sequence`) as well as `QueryBuilder` subqueries; passing `str`, `bytes`, `bytearray`, or `memoryview` raises `QueryError`. Empty sequences compile safely (`IN []` -> `WHERE FALSE`; `NOT IN []` -> `WHERE TRUE`).
- **Ranges:** `.where_between(col, (min_v, max_v))`, `.where_not_between(...)`, `.or_where_between(...)`, `.or_where_not_between(...)`.
- **Multi-column comparisons:** `.where_any(columns, operator, value)`, `.where_all(...)`, and `.where_none(...)` compare the same value against a non-empty list/tuple of columns. `where_any` matches one or more columns, `where_all` requires every comparison, and `where_none` negates the OR group. Each value is bound separately; the one-argument form defaults to equality.
- **Column comparisons & existence:** `.where_column("orders.user_id", "=", "users.id")` compares two validated identifiers. `.where_exists(query)` / `.where_not_exists(query)` accept a `QueryBuilder`, including a correlated subquery; use the outer table's real name in `where_column`. Table aliases are not supported, and the subquery must use a different table.
- **Time comparisons:** `.where_past("due_at")` and `.where_future("starts_at")` compare against PostgreSQL `CURRENT_TIMESTAMP`; `or_where_past` and `or_where_future` are available. `CURRENT_TIMESTAMP` is fixed at the start of the current transaction. For `timestamp without time zone`, PostgreSQL's session time zone applies. SQL NULL timestamps do not match.
- **Escaped `LIKE` / `ILIKE`:** `.where_like(col, value, case_insensitive=False, match="contains")` and `.where_ilike(...)` (plus `or_where_like` / `or_where_ilike`). Supported `match` modes are `"contains"`, `"starts_with"` / `"prefix"`, `"ends_with"` / `"suffix"`, and `"exact"`. User characters `%`, `_`, and `\` are automatically escaped via `escape_like()` with `ESCAPE '\'`.
- **Nested parentheses:** `.where_group(callback)` / `.or_where_group(callback)` group boolean expressions (`AND (...)` / `OR (...)`).
- **Conditionals:** `.when(cond, fn, default=None)` and `.unless(cond, fn, default=None)`.
- **Trusted raw SQL:** `.where_raw(sql, bindings=None)` / `.or_where_raw(...)` with `?` placeholders (placeholder count mismatches raise `QueryError`).

> [!CAUTION]
> Never pass untrusted user strings directly into `select_raw()`, `where_raw()`, or `having_raw()`. Always use `?` placeholders in `sql` and pass user values in `bindings`.

### JSON Columns

JSON paths use PostgreSQL's `->` separator, not dots. Dots already qualify identifiers (`users.settings` means a table-qualified column); `users.settings.theme` would otherwise be ambiguous. A path is a validated column followed by one or more identifier or non-negative array-index segments:

```python
from swiftpy.database import Query

query = (
    Query.table("users")
    .where("settings->theme", "dark")
    .where_in("settings->languages", ["en", "fr"])
    .where_json_contains("settings->roles", "editor")
    .where_json_contains_key("settings->theme")
)
```

In addition to plain columns, `->` paths are accepted only in `where`/`or_where`, `where_in`/`where_not_in` and their `or_` variants, the `where_json_*` methods, and `update()` keys. The path is sent as a bound `text[]`; values remain bound parameters. JSON paths are not accepted in `select`, `order_by`, `group_by`, `pluck`, `value`, null/range filters, joins, inserts, upserts, counters, or direct instance `Model.update(...)`.

For `where()` and `where_in()` on a JSON path:

| Operator | Value | Behavior |
|---|---|---|
| `=` | JSON value | JSONB equality; Python values are JSON-encoded at registration |
| `=` / `!=` / `<>` | `None` | `=` matches JSON null and missing paths; `!=` / `<>` matches only non-null JSON values |
| `!=` / `<>` | non-`None` value | JSONB inequality; missing paths and SQL NULL do not match |
| `<`, `>`, `<=`, `>=` | string or number (not bool) | Compare only rows whose JSON type matches, so mixed-type rows do not raise cast errors |
| `LIKE`, `ILIKE` | string | Compare JSON string values only |
| `IN`, `NOT IN` | list/tuple of JSON scalars | Compare JSONB values; an empty list becomes `FALSE` / `TRUE` |

Use the dedicated helpers for PostgreSQL containment, key existence, and array length:

```python
from swiftpy.database import Query

query = (
    Query.table("users")
    .where_json_contains("settings->roles", "editor")
    .where_json_doesnt_contain("settings->roles", "banned")
    .where_json_contains_key("settings->theme")
    .where_json_doesnt_contain_key("settings->retired")
    .where_json_length("settings->languages", ">=", 2)
)
```

Containment follows PostgreSQL `jsonb @>` semantics: the scalar `"editor"` is contained in an array such as `["editor", "admin"]`; an array value means that all its elements must be present. `where_json_doesnt_contain()` also matches a missing path or SQL NULL. A JSON `null` value counts as an existing key. `where_json_length()` compares array lengths only; an object, scalar, missing path, or SQL NULL never matches, including for `!=`.

JSON path updates use `jsonb_set()` and can be mixed with ordinary assignments:

```python
from swiftpy.database import Query

await (
    Query.table("users")
    .where("id", user_id)
    .update({"settings->theme": "dark", "settings->notifications->email": True})
)
```

A SQL NULL JSON column is treated as `{}`, and assigning Python `None` writes JSON `null`. PostgreSQL `jsonb_set()` does not create missing intermediate parents: when `settings->notifications` does not already exist, setting `settings->notifications->email` leaves the document unchanged. Updating both an entire JSON column and one of its paths in the same statement is rejected. JSON path updates are available through query-builder `.update(...)`, not a persisted model instance's `update(...)`.

### Unions

`QueryBuilder.union(query)` removes duplicate rows; `union_all(query)` preserves them. Each branch must be a `QueryBuilder` that selects the same number of compatible columns. A branch's own ordering and pagination stay inside that branch; ordering/limit/offset on the base builder apply to the combined result. An order column must name an output column. Clauses added after `union()` still belong to the base query. Nested unions and union methods on `ModelQueryBuilder` / `Model` are not supported.

```python
from swiftpy.database import Query

combined = (
    Query.table("current_users").select("id", "email")
    .union_all(Query.table("archived_users").select("id", "email"))
    .order_by("id")
    .limit(50)
)
rows = await combined.get()
```

Union queries support `get()`, `first()`, `simple_paginate()`, `to_sql()`, and the debugging helpers below. Aggregates, `exists()`, `paginate()`, `pluck()`, `value()`, writes, and chunking raise `QueryError` rather than silently operating on only one branch. A union builder can be used as a subquery.

### Pessimistic Row Locks

`shared_lock()` emits `FOR SHARE`; `lock_for_update()` emits `FOR UPDATE`. Locks require a live SwiftPY transaction when a read is executed:

```python
from swiftpy.database import Query, db

async with db.transaction():
    user = await Query.table("users").where("id", user_id).lock_for_update().first()
```

The lock is applied to reads such as `get()`, `first()`, `value()`, and `simple_paginate()`. Calling `to_sql()` only compiles SQL and does not require a transaction. Locks cannot be combined with `DISTINCT`, `GROUP BY`, `HAVING`, or unions; they are rejected on write operations. Aggregates and the count query in `paginate()` do not carry the row lock, while the page's data query does.

### Query Debugging

`to_raw_sql()` produces a human-readable rendering; `dump()` logs parameterized SQL plus bindings, and `dump_raw_sql()` logs the rendering. The `dd()` variants log and raise `QueryDumpError` to stop the current flow. Enable INFO logging for `swiftpy.database.query_builder` to see these records.

```python
from swiftpy.database import Query

query = Query.table("users").where("email", "ada@example.com")
sql, bindings = query.to_sql()  # Parameterized SQL and safe binding values
debug_sql = query.to_raw_sql()  # Human-readable only
query.dump()
```

`to_raw_sql()` output is for human debugging only: never execute it or use it to construct another query. Its string escaping assumes PostgreSQL's default `standard_conforming_strings=on`, and values that cannot be represented as ordinary PostgreSQL text literals (such as Python strings containing NUL bytes or unsupported custom objects) are rendered diagnostically rather than as executable SQL literals. Bindings may contain credentials or tokens, so keep `dump*()` and `dd*()` out of production code paths.

### Grouping, `HAVING`, Ordering & Terminals

```python
from __future__ import annotations

from typing import Any

from swiftpy.database import Query


async def top_spending_customers(min_total: float) -> list[dict[str, Any]]:
    return await (
        Query.table("orders")
        .select("user_id")
        .select_count("*", alias="order_count")
        .select_sum("total", alias="lifetime_spend")
        .where("status", "paid")
        .group_by("user_id")
        .having("lifetime_spend", ">=", min_total)
        .having("COUNT(*)", ">", 1)
        .order_by("lifetime_spend", "DESC")
        .limit(10)
        .get()
    )
```

- **Ordering & limits:** `.order_by(col, "ASC" | "DESC")`, `.latest(col="created_at")`, `.oldest(col="created_at")`, `.in_random_order()`, `.limit(n)` / `.take(n)`, `.offset(n)` / `.skip(n)`.
- **Inspection & reuse:** `.clone()` creates an independent builder copy; `.to_sql()` returns `(sql, bindings)`.
- **Read terminal methods:**
  - `await q.get()` -> `list[dict[str, Any]]`
  - `await q.first()` -> `dict[str, Any] | None`
  - `await q.first_or_fail()` -> `dict[str, Any]` (raises `RecordNotFoundError`, mapped automatically to HTTP `404`)
  - `await q.value("email")` -> scalar value from the first matching row (`None` if empty)
  - `await q.pluck("email")` -> `list[Any]`, or `await q.pluck("email", key="id")` -> `dict[Any, Any]`
  - `await q.exists()` -> `bool`
  - Aggregates: `await q.count(col="*")`, `await q.sum(col)`, `await q.avg(col)`, `await q.min(col)`, `await q.max(col)`

### Writes, Atomic Counters, Upserts & Chunking

```python
from __future__ import annotations

from typing import Any

from swiftpy.database import Query


async def manage_inventory() -> None:
    # 1. Insert returning integer id (or specify returning="uuid" / ["id", "sku"])
    new_id = await Query.table("products").insert({"sku": "BK-101", "stock": 25})
    created = await Query.table("products").insert_returning(
        {"sku": "BK-102", "stock": 10},
        returning=["id", "sku", "stock"],
    )

    # 2. Targeted UPDATE / DELETE (WHERE is required unless .allow_all() is called)
    await Query.table("products").where("id", new_id).decrement(
        "stock", 2, extra={"sku": "BK-101-V2"}
    )
    await Query.table("products").where("id", created["id"]).increment("stock", 5)

    # 3. PostgreSQL ON CONFLICT upsert
    await Query.table("products").upsert(
        [
            {"sku": "BK-101-V2", "stock": 30},
            {"sku": "BK-103", "stock": 15},
        ],
        unique_by="sku",
        update_columns=["stock"],
    )
```

Batch processing large tables cleanly without exhausting memory:

```python
async def process_page(rows: list[dict[str, Any]]) -> bool | None:
    for row in rows:
        # Process row; return False at any time to stop early
        _ = row["id"]
    return None


await Query.table("events").where("processed", False).chunk_by_id(500, process_page)
```

- Use `.chunk_by_id(size, callback, column="id")` whenever rows might be updated or deleted while iterating (cursor advances by `column > last_id`); `.chunk(size, callback)` paginates via `OFFSET`/`LIMIT`.
- Both `Query.table("...")` `.update(...)`, `.delete()`, `.increment(...)`, and `.decrement(...)` **refuse to run without a `WHERE` clause** (`QueryError`) unless you explicitly call `.allow_all()`.
- `.insert()`, `.insert_returning()`, `.update()`, `.increment()` / `.decrement()` (`extra`), and `.upsert()` accept any `Mapping[str, Any]` (and `Sequence[Mapping[str, Any]]` / `Sequence[str]` on `upsert()`), so read-only mappings (such as `MappingProxyType`) and narrowed dictionary types (`dict[str, str]`) type-check cleanly without casts.

---

## Pagination (`paginate()` & `simple_paginate()`)

Use `.paginate(per_page=15, page=1)` for full page counts (`total`, `last_page`) or `.simple_paginate(per_page=15, page=1)` when you only need "Next / Previous" navigation without a `COUNT(*)` query:

```python
from __future__ import annotations

from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.database import Query


async def list_articles(request: Request) -> Response:
    page = max(1, int(request.query_params.get("page", "1")))
    per_page = min(100, max(1, int(request.query_params.get("per_page", "15"))))

    paginator = await (
        Query.table("articles")
        .select("id", "title", "created_at")
        .where("published", True)
        .latest("created_at")
        .paginate(per_page=per_page, page=page)
    )
    return json_response(paginator.to_dict())
```

`Paginator.to_dict()` produces:

```json
{
  "data": [{"id": 1, "title": "Hello", "created_at": "..."}],
  "current_page": 1,
  "per_page": 15,
  "total": 42,
  "last_page": 3,
  "from": 1,
  "to": 15,
  "has_more_pages": true,
  "has_previous_page": false
}
```

- When `.group_by()` or `.having()` is present, `paginate()` automatically counts groups accurately via `SELECT COUNT(*) AS total FROM (...) AS grouped_results`.
- `simple_paginate()` fetches `per_page + 1` rows and omits `"total"` and `"last_page"` from `to_dict()`, avoiding full-table counts on large datasets.
- Both `Paginator` and `SimplePaginator` support `len(p)`, indexing (`p[0]`), iteration (`for item in p`), `.items`, `.has_more_pages`, `.has_previous_page`, `.first_item`, and `.last_item`.

---

## Transactions, Nested Savepoints & `on_commit`

Use `async with db.transaction():` to execute multiple queries, ORM operations, schema calls, or validation rules atomically on one connection:

```python
from __future__ import annotations

from swiftpy.database import Query, db, on_commit


async def transfer_funds(from_id: int, to_id: int, amount: float) -> None:
    async with db.transaction():
        await Query.table("accounts").where("id", from_id).decrement("balance", amount)
        await Query.table("accounts").where("id", to_id).increment("balance", amount)

        async def notify_ledger() -> None:
            # Runs only after the outermost transaction commits
            await send_receipt_webhook(from_id, to_id, amount)

        on_commit(notify_ledger)
```

How `db.transaction()` behaves:

- **Automatic connection sharing:** All `Query.table(...)`, ORM models, `Schema` calls, and `unique`/`exists` validation checks inside `async with db.transaction():` share the active `TransactionConnection` automatically via SwiftPY's task `Context`.
- **Nested savepoints:** Entering a nested `async with db.transaction():` creates a PostgreSQL `SAVEPOINT`. If the inner block raises and you catch the exception, only the inner savepoint rolls back cleanly.
- **`on_commit(callback)` deferral:** Accepts sync or `async def` callbacks. Nested callbacks bubble up to the outermost transaction, execute only if the outermost commit succeeds, and are discarded on rollback. Calling `on_commit(...)` outside any transaction executes the callback immediately.
- **Connection serialization & close safety:** Concurrent awaits inside one transaction serialize automatically on a lock (do not expect query parallelism inside a single transaction), and once the block exits, the handle closes permanently so leaked background tasks raise `TransactionClosedError` instead of reusing a released pool connection.

> [!NOTE]
> `db_transaction` exported from `swiftpy.testing` is a **pytest fixture** for automatic per-test rollback. In application code, always use `async with db.transaction():` from `swiftpy.database`.

---

## Database Migrations (`Schema`, `Blueprint` & CLI)

### Generating Migration Files (`swiftpy make:migration`)

```bash
# Auto-infers table "posts" and uses the create stub
swiftpy make:migration create_posts_table

# Explicit table flags
swiftpy make:migration create_posts_table --create posts
swiftpy make:migration add_published_at_to_posts_table --table posts
```

Options:

- `--create <table>` — use the `Schema.create(...)` template for `<table>`.
- `--table <table>` — use the `Schema.table(...)` alter template for `<table>`.
- `--path <dir>` — write to a custom directory instead of `database/migrations/`.
- `--force` — overwrite an existing file.
- You can also run `swiftpy make:model Post -m` to scaffold both a model and its creation migration together.

Starter projects already include `database/migrations/2026_01_01_000000_create_users_table.py`:

```python
# database/migrations/2026_01_01_000000_create_users_table.py
from __future__ import annotations

from swiftpy.database.migrations import Blueprint, Migration, Schema


class CreateUsersTable(Migration):
    async def up(self) -> None:
        def define(table: Blueprint) -> None:
            table.id()
            table.string("name")
            table.string("email").unique()
            table.string("password")
            table.boolean("is_admin").default(False)
            table.timestamps()

        await Schema.create("users", define)

    async def down(self) -> None:
        await Schema.drop_if_exists("users")
```

### Defining Tables & Columns (`Blueprint` & `Schema`)

For the complete reference of every `Schema` method (`create`, `table`, `drop`, `drop_if_exists`, `has_table`, `has_column`), `Blueprint` column type (including `enum`, `medium_text`, `long_text`, `jsonb`, `vector`, and `soft_deletes`), fluent modifiers, foreign keys, and PostgreSQL caveats (no `.after()` or `Schema.rename`), see [Database Schema, `Blueprint` & Migrations](2026-11-19-database-schema-and-blueprint.md).

```python
from __future__ import annotations

from swiftpy.database.migrations import Blueprint, Migration, Schema, raw


class CreateArticlesAndTags(Migration):
    async def up(self) -> None:
        def define_articles(table: Blueprint) -> None:
            table.id()
            table.uuid("public_id").unique()
            table.big_integer("author_id").unsigned()
            table.string("title", 200)
            table.text("body").nullable()
            table.decimal("reading_minutes", precision=6, scale=2).default(1.0)
            table.jsonb("metadata").default({})
            table.vector("embedding", dimensions=1536).nullable()
            table.timestamps(tz=True)
            table.soft_deletes()

            table.foreign_key(
                column="author_id",
                references="id",
                on="users",
                on_delete="CASCADE",
            )
            table.index(["author_id", "created_at"], name="idx_articles_author_created")

        await Schema.create("articles", define_articles)

    async def down(self) -> None:
        await Schema.drop_if_exists("articles")
```

Supported `Blueprint` methods:

- **Primary keys & IDs:** `id(name="id")` (`BIGSERIAL PRIMARY KEY`), `increments(name="id")` (`SERIAL PRIMARY KEY`), `big_increments(name="id")` (`BIGSERIAL PRIMARY KEY`), or table-level composite `table.primary(["user_id", "role_id"], name=...)`.
- **Column types:** `uuid()`, `string(name, length=255)`, `text()` (unbounded `TEXT`, up to ~1 GB in PostgreSQL), `medium_text()` and `long_text()` (aliases for `text()` since PostgreSQL has no separate size variants), `enum(name, values)` (`VARCHAR(255)` with a named `{table}_{col}_check` `CHECK` constraint), `integer()`, `big_integer()`, `small_integer()`, `boolean()`, `decimal(name, precision=8, scale=2)`, `float()`, `binary(name)` (`BYTEA`), `json()`, `jsonb()`, `date()`, `time()`, `timestamp(name, tz=True)`, `timestamps(tz=True)`, `soft_deletes(name="deleted_at", tz=True)`, `vector(name, dimensions=1536)` (for `pgvector`).
- **Column modifiers:** `.nullable(True)`, `.default(value)` (use `raw("CURRENT_TIMESTAMP")` or `raw("gen_random_uuid()")` for SQL expressions; passing raw SQL keyword strings emits a `DeprecationWarning`), `.unique(name=None)`, `.index(name=None)`, `.primary()`, `.comment("text")`, `.unsigned()` (`CHECK ("col" >= 0)`), `.change()` (modify an existing column cleanly inside `Schema.table`), and `.using(expr)` (`USING` cast clause for `.change()`). Column positioning via `.after()` is intentionally not implemented because PostgreSQL only appends added columns to the end of a table.
- **Indexes & Foreign Keys:** `table.unique(cols, name=None)`, `table.index(cols, name=None)`, `table.foreign(col, name=None).references("id").on("users").cascade_on_delete()` (also `.null_on_delete()`, `.cascade_on_update()`, `.on_delete(...)`, `.on_update(...)`), or one-call `table.foreign_key(column, references, on, on_delete=..., on_update=..., name=None)`.
- **Altering & Inspecting Tables:** `await Schema.table("articles", fn)` supports `.change()` (modifying column types/nullability/defaults/constraints in place), `drop_column(*names)` (accepting variadic strings or sequences such as `["col_a", "col_b"]`), `rename_column(old, new)`, `drop_index(name)`, `drop_unique(name)`, and `drop_foreign(name)` alongside adding new columns/indexes/foreign keys. Drops always execute before renames and adds/modifications, so a single `Schema.table` call can drop and re-add a constraint or column cleanly. You can also inspect schema state with `await Schema.has_table("articles")`, `await Schema.has_column("articles", "title")`, `await Schema.has_columns("articles", ["title", "body"])`, and `await Schema.has_index("articles", "idx_articles_title")`.

### Running Migrations with the CLI

```bash
# Apply all pending migrations
swiftpy migrate

# Apply only the next N pending migrations
swiftpy migrate --step 1

# Roll back the last migration batch (or last N batches)
swiftpy migrate:rollback
swiftpy migrate:rollback --step 2

# Roll back every applied migration
swiftpy migrate:reset

# Display applied/pending status and batch numbers
swiftpy migrate:status
```

Shared migration flags: `--app <module:attr>` (defaults to `main:app`), `--path <dir>` (defaults to `database/migrations`), and `--force` (required when `APP_ENV=production`).

How `Migrator` protects your database:

- **PostgreSQL advisory lock (`MigrationLock`):** Acquires `pg_try_advisory_lock` so concurrent containers or CI jobs cannot run overlapping migrations.
- **Per-migration transaction:** Each migration's `up()` / `down()` and its `migrations` table record run atomically inside a transaction.
- **SHA-256 checksum & order validation:** Stores a checksum for each applied migration file and raises an error if an applied file was modified or if an unapplied file sorts earlier than an already-applied migration.

---

## Database Seeders (`Seeder` & `swiftpy db:seed`)

### Generating and Writing Seeders

```bash
swiftpy make:seeder UserSeeder
```

This creates `database/seeders/user_seeder.py`. Organize child seeders under `DatabaseSeeder` in `database/seeders/database_seeder.py`:

```python
# database/seeders/user_seeder.py
from __future__ import annotations

from swiftpy.database import Query, Seeder


class UserSeeder(Seeder):
    async def run(self) -> None:
        await Query.table("users").upsert(
            [
                {
                    "name": "Alice Doe",
                    "email": "alice@example.com",
                    "password": "<hashed-password>",
                    "is_admin": True,
                }
            ],
            unique_by="email",
            update_columns=["name", "is_admin"],
        )
```

```python
# database/seeders/database_seeder.py
from __future__ import annotations

from swiftpy.database import Seeder

from database.seeders.user_seeder import UserSeeder


class DatabaseSeeder(Seeder):
    async def run(self) -> None:
        await self.call(UserSeeder)
        # Seed sample data cleanly with Model Factories:
        # await User.factory().count(25).has_posts(3).create()
```

Generate a factory for any model with `swiftpy make:factory UserFactory` (or `swiftpy make:model Post -f`); see [Model Factories](2026-11-15-orm-models-and-relationships.md#model-factories-factory-fake--sequence) for `Factory`, `Fake`, `Sequence`, states, and relationship helpers.

### Running Seeders

```bash
# Run DatabaseSeeder inside an automatic db.transaction()
swiftpy db:seed

# Run a specific seeder class or dotted import path
swiftpy db:seed --class UserSeeder
swiftpy db:seed --class database.seeders.user_seeder.UserSeeder

# Skip wrapping the seeder in a single transaction
swiftpy db:seed --no-transaction
```

Options: `--class` (defaults to `DatabaseSeeder`), `--no-transaction`, `--app` (`main:app`), `--path` (`database/seeders`), and `--force` (required in production).

---

## Important Defaults & Production Safeguards

- **Production empty-password guard:** Leaving `DB_PASSWORD` empty raises `ConfigurationError` when `APP_ENV=production` or `app.debug=False`.
- **Production CLI guard:** `swiftpy migrate`, `swiftpy migrate:rollback`, `swiftpy migrate:reset`, and `swiftpy db:seed` exit with code `1` in production unless `--force` is explicitly passed.
- **Unguarded mass-write protection (`D-10`):** `QueryBuilder.update()`, `delete()`, `increment()`, and `decrement()` raise `QueryError` without a `WHERE` clause unless `.allow_all()` is called.
- **SQL identifier validation:** Table names, column names, aliases, and blueprint constraints are checked against strict `^[a-zA-Z_][a-zA-Z0-9_]*$` rules (max 63 characters) before SQL compilation.
- **Transactional pool safety:** Operations on a single `db.transaction()` share and serialize access to one connection, and post-exit usage raises `TransactionClosedError`.

---

## Next Step

Continue to [Database Schema, `Blueprint` & Migrations](2026-11-19-database-schema-and-blueprint.md) for the complete column type, foreign key, and alteration reference (or jump ahead to [Active Record ORM & Relationships](2026-11-15-orm-models-and-relationships.md)).
