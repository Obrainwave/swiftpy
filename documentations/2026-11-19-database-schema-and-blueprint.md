# Database Schema, `Blueprint` & Migrations

SwiftPY provides an async, PostgreSQL-focused schema builder through `Schema`, `Blueprint`, and `Migration`. Migration files live in `database/migrations/` and run inside advisory-locked execution order via the `swiftpy migrate*` CLI commands.

This guide is the dedicated reference for `Schema`, every `Blueprint` column type and fluent modifier, table alterations, foreign keys, and PostgreSQL caveats.

Quick imports:

```python
from __future__ import annotations

from swiftpy.database.migrations import (
    Blueprint,
    ColumnDefinition,
    DefaultRaw,
    ForeignKeyDefinition,
    Migration,
    Schema,
    raw,
)
```

---

## Generating Migrations

Create standalone migration files (or scaffold a model together with its migration) using the CLI:

```bash
# Auto-infers table "orders" -> Schema.create("orders", ...)
uv run swiftpy make:migration create_orders_table

# Explicit create or alter stubs
uv run swiftpy make:migration create_orders_table --create orders
uv run swiftpy make:migration add_status_to_orders_table --table orders

# Model + migration together
uv run swiftpy make:model Order -m
```

Useful `make:migration` options:

- `--create <table>` — use the table creation stub (`Schema.create`).
- `--table <table>` — use the table alteration stub (`Schema.table`).
- `--path <dir>` — override the target folder (defaults to `database/migrations/`).
- `--force` — overwrite an existing migration file.

---

## `Schema` Operations

Every migration subclass implements `async def up(self)` and `async def down(self)` using `Schema`'s async methods:

```python
# database/migrations/2026_02_01_000001_create_orders_table.py
from __future__ import annotations

from swiftpy.database.migrations import Blueprint, Migration, Schema, raw


class CreateOrdersTable(Migration):
    async def up(self) -> None:
        def define(table: Blueprint) -> None:
            table.id()
            table.big_integer("user_id").unsigned()
            table.foreign("user_id").references("id").on("users").cascade_on_delete()
            table.string("reference", length=64).unique()
            table.enum("status", ["pending", "paid", "cancelled"]).default("pending")
            table.decimal("total_amount", precision=12, scale=2).default(0)
            table.jsonb("metadata").nullable()
            table.timestamp("placed_at").default(raw("CURRENT_TIMESTAMP"))
            table.timestamps()
            table.soft_deletes()

        await Schema.create("orders", define)

    async def down(self) -> None:
        await Schema.drop_if_exists("orders")
```

Supported `Schema` methods:

| Method | Signature | Description |
|---|---|---|
| `Schema.create` | `await Schema.create(table_name, callback)` | Executes `CREATE TABLE` followed by standalone indexes and column comments compiled from `Blueprint`. |
| `Schema.table` | `await Schema.table(table_name, callback)` | Alters an existing table (`ALTER TABLE` / `CREATE INDEX` / `DROP INDEX`). |
| `Schema.drop` | `await Schema.drop(table_name)` | Executes `DROP TABLE "<table_name>";`. |
| `Schema.drop_if_exists` | `await Schema.drop_if_exists(table_name)` | Executes `DROP TABLE IF EXISTS "<table_name>";`. |
| `Schema.has_table` | `await Schema.has_table(table_name) -> bool` | Checks PostgreSQL metadata for the existence of `table_name`. |
| `Schema.has_column` | `await Schema.has_column(table_name, column_name) -> bool` | Checks whether `column_name` exists on `table_name`. |

> [!NOTE]
> SwiftPY does **not** implement `Schema.rename(...)`. If you need to rename a table, use `SchemaGrammar` or execute explicit PostgreSQL DDL carefully rather than calling `Schema.rename`.

---

## `Blueprint` Column Types Reference

Every column name and identifier passes through `validate_identifier()` before SQL generation, preventing unsafe characters or unquoted SQL keywords.

### Primary Keys

| Method | PostgreSQL DDL | Notes |
|---|---|---|
| `table.id(name="id")` | `BIGSERIAL PRIMARY KEY` | Standard 64-bit auto-incrementing primary key (`table.big_increments()` alias behavior). |
| `table.big_increments(name="id")` | `BIGSERIAL PRIMARY KEY` | Explicit 64-bit auto-incrementing primary key. |
| `table.increments(name="id")` | `SERIAL PRIMARY KEY` | 32-bit auto-incrementing primary key. |

### Strings, Text & Enums

| Method | PostgreSQL DDL | Notes |
|---|---|---|
| `table.uuid(name)` | `UUID` | Native PostgreSQL UUID column. |
| `table.string(name, length=255)` | `VARCHAR(length)` / `VARCHAR` | Configurable bounded length (`VARCHAR` when `length=None`). |
| `table.text(name)` | `TEXT` | Variable unlimited-length text. |
| `table.medium_text(name)` | `TEXT` | Mapped cleanly to PostgreSQL `TEXT`. |
| `table.long_text(name)` | `TEXT` | Mapped cleanly to PostgreSQL `TEXT`. |
| `table.enum(name, values)` | `VARCHAR(<N>)` + value check | Validates allowed non-empty string `values`; validates `.default(...)` against `values` (unless nullable `None` or `raw(...)`). |
| `table.remember_token(name="remember_token", length=100)` | `VARCHAR(length) NULL` | Nullable persistent token column used by `Auth.attempt(..., remember=True)`. |

### Numeric & Boolean Types

| Method | PostgreSQL DDL | Notes |
|---|---|---|
| `table.integer(name)` | `INTEGER` | 32-bit signed integer. |
| `table.big_integer(name)` | `BIGINT` | 64-bit signed integer (ideal for foreign keys referencing `table.id()`). |
| `table.small_integer(name)` | `SMALLINT` | 16-bit signed integer. |
| `table.decimal(name, precision=8, scale=2)` | `NUMERIC(precision, scale)` | Exact fixed-point numeric type (`1 <= precision <= 1000`, `0 <= scale <= precision`). |
| `table.float(name)` | `DOUBLE PRECISION` | 64-bit IEEE floating-point number. |
| `table.boolean(name)` | `BOOLEAN` | True/false boolean flag. |

### Dates, Timestamps & Soft Deletes

| Method | PostgreSQL DDL | Notes |
|---|---|---|
| `table.date(name)` | `DATE` | Calendar date (`YYYY-MM-DD`). |
| `table.time(name)` | `TIME` | Time of day without date. |
| `table.timestamp(name, tz=True)` | `TIMESTAMPTZ` / `TIMESTAMP` | Timezone-aware by default (`tz=False` emits `TIMESTAMP`). |
| `table.timestamps(tz=True)` | `created_at` & `updated_at` | Adds both `created_at` and `updated_at` (`TIMESTAMPTZ` by default, `DEFAULT CURRENT_TIMESTAMP`). |
| `table.soft_deletes(name="deleted_at", tz=True)` | Nullable timestamp | Adds a nullable deletion marker column (`deleted_at` by default). |

### JSON & pgvector Columns

| Method | PostgreSQL DDL | Notes |
|---|---|---|
| `table.json(name)` | `JSON` | Raw JSON text storage. |
| `table.jsonb(name)` | `JSONB` | Binary decomposed JSON (recommended on PostgreSQL). |
| `table.vector(name, dimensions=1536)` | `vector(dimensions)` | pgvector column (ensure `CREATE EXTENSION IF NOT EXISTS vector;` is enabled first). |

---

## Fluent Column Modifiers (`ColumnDefinition`)

Chain modifiers onto any column definition returned by `Blueprint`:

```python
table.string("slug", length=120).unique()
table.string("nickname", length=80).nullable()
table.integer("stock_qty").unsigned().default(0).comment("Available inventory")
table.timestamp("synced_at").nullable().default(raw("CURRENT_TIMESTAMP")).index()
```

| Modifier | Behavior |
|---|---|
| `.nullable(value=True)` | Marks column `NULL` (default for new columns is `NOT NULL`; pass `nullable(False)` to require a value). |
| `.default(val)` | Sets SQL default literal (`bool`, `int`, `float`, quoted `str`, or `raw("...")` / `DefaultRaw`). |
| `.unique(name=None)` | Adds a `UNIQUE` column or named table constraint. |
| `.index(name=None)` | Creates a standalone `CREATE INDEX` statement after table creation/alteration. |
| `.primary()` | Marks the column as `PRIMARY KEY` (for non-serial keys like `uuid`). |
| `.comment(text)` | Emits PostgreSQL `COMMENT ON COLUMN` after table creation/alteration. |
| `.unsigned()` | Emits a non-negative `CHECK ("col" >= 0)` constraint for numeric columns. |

> [!WARNING]
> **No `.after()` modifier on PostgreSQL:** `ColumnDefinition` does **not** define `.after("other_column")`. PostgreSQL appends new columns at the end of the table row during `ALTER TABLE ... ADD COLUMN` and ignores physical column positioning hints.

---

## Foreign Keys & Table-Level Indexes

### Foreign Keys (`foreign` / `foreign_key`)

Neither `table.foreign(...)` nor `table.foreign_key(...)` adds the column for you — always declare the column (for example `table.big_integer("author_id").unsigned()`) first, then attach the constraint using either fluent `table.foreign(...)` or the one-call `table.foreign_key(...)` helper:

```python
# Option 1: Declare column + fluent constraint chain (`table.foreign(...) -> ForeignKeyDefinition`)
table.big_integer("author_id").unsigned()
table.foreign("author_id").references("id").on("users").cascade_on_delete()

# Option 2: Declare nullable column + one-call helper (`table.foreign_key(...) -> Blueprint`)
table.big_integer("reviewer_id").nullable()
table.foreign_key(
    "reviewer_id",
    references="id",
    on="users",
    on_delete="SET NULL",
    name="fk_posts_reviewer",
)
```

Supported `ForeignKeyDefinition` methods:

- `.references(column)` — target column name (defaults to `"id"`).
- `.on(table)` — target table name (**required**).
- `.on_delete(action)` / `.on_update(action)` — allowed actions: `"CASCADE"`, `"SET NULL"`, `"SET DEFAULT"`, `"RESTRICT"`, `"NO ACTION"`.
- `.cascade_on_delete()` / `.cascade_on_update()` — shortcuts for `"CASCADE"`.

Default auto-generated foreign key constraint names follow `fk_<table>_<column>_<ref_table>`.

### Composite Primary Keys, Indexes & Uniques

```python
def define(table: Blueprint) -> None:
    table.big_integer("user_id")
    table.big_integer("role_id")
    table.primary(["user_id", "role_id"])
    table.unique(["user_id", "role_id"], name="uq_user_roles_pair")
    table.index(["role_id"])
```

---

## Altering Existing Tables (`Schema.table`)

When compiling `Schema.table(name, fn)`, `SchemaGrammar` orders statements safely within the blueprint so drops run cleanly before renames and new columns/constraints:

1. **Drops:** `drop_foreign(name)`, `drop_unique(name)`, `drop_index(name)`, `drop_column(*names)` (each compiled with `IF EXISTS`)
2. **Renames:** `rename_column(from_name, to_name)` (`ALTER TABLE ... RENAME COLUMN ... TO ...`)
3. **Additions:** new columns (`ALTER TABLE ... ADD COLUMN ...`), standalone indexes, column comments, composite indexes/uniques, and foreign keys.

```python
class UpdateOrdersTable(Migration):
    async def up(self) -> None:
        def alter(table: Blueprint) -> None:
            table.rename_column("total_amount", "subtotal_amount")
            table.decimal("tax_amount", precision=12, scale=2).default(0)
            table.string("tracking_code", length=64).nullable().index("idx_orders_tracking_code")

        await Schema.table("orders", alter)

    async def down(self) -> None:
        def revert(table: Blueprint) -> None:
            table.drop_index("idx_orders_tracking_code")
            table.drop_column("tracking_code", "tax_amount")
            table.rename_column("subtotal_amount", "total_amount")

        await Schema.table("orders", revert)
```

---

## Running & Rolling Back Migrations

See [Developer CLI & Testing Utilities](2026-11-19-cli-and-testing.md) for the full `swiftpy` command options:

```bash
uv run swiftpy migrate --app main:app
uv run swiftpy migrate:status --app main:app
uv run swiftpy migrate:rollback --app main:app --step 1
uv run swiftpy migrate:reset --app main:app
```

> [!NOTE]
> **PostgreSQL sequences and ID gaps:** Because `table.id()` maps to `BIGSERIAL` (backed by a non-transactional PostgreSQL sequence), rolled-back inserts or unique-constraint errors consume sequence numbers cleanly without data loss, leaving harmless gaps (`1, 4, ...`). See [Active Record ORM & Relationships](2026-11-15-orm-models-and-relationships.md) for details and invoice-number alternatives.

---

## Next Step

Continue to [Active Record ORM & Relationships](2026-11-15-orm-models-and-relationships.md) to map tables to typed `Model` classes with dirty tracking, `HasMany`/`BelongsTo` eager loading, and N+1 protection.
