from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from datetime import date
from typing import Any

import asyncpg
import pytest

from swiftpy.database.migrations import Blueprint, Schema, SchemaGrammar, raw
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query

# ---------------------------------------------------------------------------
# Unit & SQL Compilation Tests
# ---------------------------------------------------------------------------


def test_blueprint_binary_and_jsonb_sql_compilation() -> None:
    """Verify Blueprint.binary() (bytes/bytearray/memoryview) and jsonb() SQL compilation."""
    bp = Blueprint("media_assets", is_create=True)
    bp.id()
    bp.binary("photo").default(b"\xca\xfe")
    bp.binary("signature").default(bytearray(b"\xca\xfe"))
    bp.binary("document").default(memoryview(b"\xca\xfe"))
    bp.binary("thumbnail").nullable()
    bp.binary("empty_blob").default(b"")
    bp.jsonb("options")
    bp.jsonb("prefs").default({"theme": "O'Reilly"})
    bp.boolean("is_active").default(True)

    sql = SchemaGrammar.compile_create(bp)[0]
    assert '"photo" BYTEA NOT NULL DEFAULT \'\\xcafe\'::bytea' in sql
    assert '"signature" BYTEA NOT NULL DEFAULT \'\\xcafe\'::bytea' in sql
    assert '"document" BYTEA NOT NULL DEFAULT \'\\xcafe\'::bytea' in sql
    assert '"thumbnail" BYTEA NULL' in sql
    assert '"empty_blob" BYTEA NOT NULL DEFAULT \'\\x\'::bytea' in sql
    assert '"options" JSONB NOT NULL' in sql
    assert '"prefs" JSONB NOT NULL DEFAULT \'{"theme": "O\'\'Reilly"}\'' in sql
    assert '"is_active" BOOLEAN NOT NULL DEFAULT TRUE' in sql


def test_blueprint_drop_column_single_varargs_and_list() -> None:
    """Verify drop_column() accepts a single column, varargs, or a sequence of columns."""
    bp = Blueprint("users", is_create=False)
    bp.drop_column("votes")
    bp.drop_column(["avatar", "location"])
    bp.drop_column("legacy_a", ["legacy_b", "legacy_c"])

    statements = SchemaGrammar.compile_table(bp)
    assert statements == [
        'ALTER TABLE "users" DROP COLUMN IF EXISTS "votes";',
        'ALTER TABLE "users" DROP COLUMN IF EXISTS "avatar";',
        'ALTER TABLE "users" DROP COLUMN IF EXISTS "location";',
        'ALTER TABLE "users" DROP COLUMN IF EXISTS "legacy_a";',
        'ALTER TABLE "users" DROP COLUMN IF EXISTS "legacy_b";',
        'ALTER TABLE "users" DROP COLUMN IF EXISTS "legacy_c";',
    ]

    bad_bp = Blueprint("users", is_create=False)
    with pytest.raises(ValueError, match="at least one column"):
        bad_bp.drop_column()
    with pytest.raises(ValueError, match="must not be empty"):
        bad_bp.drop_column([])
    bad_col_arg: Any = 123
    with pytest.raises(TypeError, match="str or sequence"):
        bad_bp.drop_column(bad_col_arg)
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        bad_bp.drop_column(["valid_col", "bad;col"])


def test_blueprint_change_and_using_sql_compilation() -> None:
    """Verify .change() and .using() compile declarative PostgreSQL ALTER COLUMN DDL."""
    bp_resize = Blueprint("users", is_create=False)
    bp_resize.string("name", 50).change()

    resize_sql = SchemaGrammar.compile_table(bp_resize)
    assert resize_sql == [
        'ALTER TABLE "users" DROP CONSTRAINT IF EXISTS "users_name_check";',
        'ALTER TABLE "users" ALTER COLUMN "name" DROP DEFAULT;',
        'ALTER TABLE "users" ALTER COLUMN "name" TYPE VARCHAR(50);',
        'ALTER TABLE "users" ALTER COLUMN "name" SET NOT NULL;',
        'COMMENT ON COLUMN "users"."name" IS NULL;',
    ]

    bp_keep = Blueprint("users", is_create=False)
    bp_keep.integer("votes").unsigned().default(1).comment("my comment").change()

    keep_sql = SchemaGrammar.compile_table(bp_keep)
    assert keep_sql == [
        'ALTER TABLE "users" DROP CONSTRAINT IF EXISTS "users_votes_check";',
        'ALTER TABLE "users" ALTER COLUMN "votes" DROP DEFAULT;',
        'ALTER TABLE "users" ALTER COLUMN "votes" TYPE INTEGER;',
        'ALTER TABLE "users" ALTER COLUMN "votes" SET NOT NULL;',
        'ALTER TABLE "users" ALTER COLUMN "votes" SET DEFAULT 1;',
        'ALTER TABLE "users" ADD CONSTRAINT "users_votes_check" CHECK ("votes" >= 0);',
        'COMMENT ON COLUMN "users"."votes" IS \'my comment\';',
    ]

    bp_using = Blueprint("users", is_create=False)
    bp_using.date("birthday").nullable().using("birthday::date").change()
    using_sql = SchemaGrammar.compile_table(bp_using)
    assert using_sql == [
        'ALTER TABLE "users" DROP CONSTRAINT IF EXISTS "users_birthday_check";',
        'ALTER TABLE "users" ALTER COLUMN "birthday" DROP DEFAULT;',
        'ALTER TABLE "users" ALTER COLUMN "birthday" TYPE DATE USING birthday::date;',
        'ALTER TABLE "users" ALTER COLUMN "birthday" DROP NOT NULL;',
        'COMMENT ON COLUMN "users"."birthday" IS NULL;',
    ]

    bp_using_raw = Blueprint("users", is_create=False)
    bp_using_raw.integer("age").using(raw("NULLIF(age, '')::integer")).change()
    assert (
        'ALTER TABLE "users" ALTER COLUMN "age" TYPE INTEGER USING NULLIF(age, \'\')::integer;'
        in SchemaGrammar.compile_table(bp_using_raw)
    )

    # Quoted identifiers, casts, and nested function calls in .using()
    bp_nested = Blueprint("users", is_create=False)
    bp_nested.string("email", 120).using("lower(btrim(coalesce(\"email\", '')))").change()
    assert (
        'ALTER TABLE "users" ALTER COLUMN "email" TYPE VARCHAR(120) USING lower(btrim(coalesce("email", \'\')));'
        in SchemaGrammar.compile_table(bp_nested)
    )
    bp_quoted_cast = Blueprint("users", is_create=False)
    bp_quoted_cast.integer("ext_num").using(raw('NULLIF("ext_num", \'\')::integer')).change()
    assert (
        'ALTER TABLE "users" ALTER COLUMN "ext_num" TYPE INTEGER USING NULLIF("ext_num", \'\')::integer;'
        in SchemaGrammar.compile_table(bp_quoted_cast)
    )

    # Apostrophe escaping in comments & string defaults
    bp_escape = Blueprint("users", is_create=False)
    bp_escape.string("bio", 200).default("O'Reilly's user").comment("user's bio note").change()
    escape_sql = SchemaGrammar.compile_table(bp_escape)
    assert 'ALTER TABLE "users" ALTER COLUMN "bio" SET DEFAULT \'O\'\'Reilly\'\'s user\';' in escape_sql
    assert 'COMMENT ON COLUMN "users"."bio" IS \'user\'\'s bio note\';' in escape_sql

    # Retaining unique() drops prior unique constraint idempotently before re-adding
    bp_unique = Blueprint("users", is_create=False)
    bp_unique.string("email", 200).unique().change()
    uq_sql = SchemaGrammar.compile_table(bp_unique)
    assert 'ALTER TABLE "users" DROP CONSTRAINT IF EXISTS "users_email_key";' in uq_sql
    assert 'ALTER TABLE "users" DROP CONSTRAINT IF EXISTS "uq_users_email";' in uq_sql
    assert 'ALTER TABLE "users" ADD CONSTRAINT "uq_users_email" UNIQUE ("email");' in uq_sql


def test_blueprint_change_and_using_validation_rejections() -> None:
    """Reject invalid combinations of .change(), .using(), and raw()."""
    create_bp = Blueprint("users", is_create=True)
    create_bp.string("name", 50).change()
    with pytest.raises(ValueError, match="cannot use .change\\(\\) inside Schema.create"):
        SchemaGrammar.compile_create(create_bp)

    create_using_bp = Blueprint("users", is_create=True)
    create_using_bp.date("birthday").using("birthday::date")
    with pytest.raises(ValueError, match="cannot use .using\\(\\) inside Schema.create"):
        SchemaGrammar.compile_create(create_using_bp)

    alter_using_only = Blueprint("users", is_create=False)
    alter_using_only.date("birthday").using("birthday::date")
    with pytest.raises(ValueError, match=".using\\(\\) can only be used with .change\\(\\)"):
        SchemaGrammar.compile_table(alter_using_only)

    alter_id_bp = Blueprint("users", is_create=False)
    alter_id_bp.id().change()
    with pytest.raises(ValueError, match="Auto-incrementing identity column"):
        SchemaGrammar.compile_table(alter_id_bp)

    alter_enum_unsigned = Blueprint("users", is_create=False)
    alter_enum_unsigned.enum("role", ["user", "admin"]).unsigned().change()
    with pytest.raises(ValueError, match="cannot also be marked .unsigned\\(\\)"):
        SchemaGrammar.compile_table(alter_enum_unsigned)

    col_bp = Blueprint("users", is_create=False)
    col = col_bp.date("birthday")
    for bad_expr in (
        "",
        "   ",
        "x; DROP TABLE users",
        "x -- comment",
        "x /* c */",
        "x */",
        "x\x00",
        "NULLIF(x, 'unclosed)::int",
        'NULLIF("unclosed, \'\')::int',
        "NULLIF(x, '')::int)",
        "((NULLIF(x, ''))::int",
    ):
        with pytest.raises(ValueError, match="USING expression"):
            col.using(bad_expr)
    with pytest.raises(ValueError, match="Raw SQL default expression must be a non-empty string"):
        raw("")
    with pytest.raises(ValueError, match="Raw SQL default expression must be a non-empty string"):
        raw("   ")
    for bad_raw in (
        raw("x; DROP TABLE users"),
        raw("x -- comment"),
        raw("x /* c */"),
        raw("x */"),
        raw("x\x00"),
        raw("NULLIF(x, '')::int)"),
    ):
        with pytest.raises(ValueError, match="USING expression"):
            col.using(bad_raw)
    bad_using_arg: Any = 123
    with pytest.raises(TypeError, match="USING expression"):
        col.using(bad_using_arg)

    # Chaining .unique() and .index("custom_idx") on the same column keeps distinct names
    both_create = Blueprint("users", is_create=True)
    both_create.string("email", 100).unique().index("idx_users_email_lookup")
    create_stmts = SchemaGrammar.compile_create(both_create)
    assert '"email" VARCHAR(100) NOT NULL UNIQUE' in create_stmts[0]
    assert 'CONSTRAINT "idx_users_email_lookup"' not in create_stmts[0]
    assert 'CREATE INDEX "idx_users_email_lookup" ON "users" ("email");' in create_stmts

    both_change = Blueprint("users", is_create=False)
    both_change.string("email", 160).unique().index("idx_users_email_lookup").change()
    change_stmts = SchemaGrammar.compile_table(both_change)
    assert 'ALTER TABLE "users" ADD CONSTRAINT "uq_users_email" UNIQUE ("email");' in change_stmts
    assert 'CREATE INDEX IF NOT EXISTS "idx_users_email_lookup" ON "users" ("email");' in change_stmts


@pytest.mark.asyncio
async def test_schema_introspection_argument_validation() -> None:
    """Validate input types and identifier checks for Schema.has_columns and Schema.has_index."""
    with pytest.raises(TypeError, match="non-empty sequence"):
        await Schema.has_columns("users", "email")
    with pytest.raises(ValueError, match="non-empty sequence"):
        await Schema.has_columns("users", [])
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        await Schema.has_columns("users", ["ok_col", "bad;col"])
    with pytest.raises(ValueError, match="expected 'table' or 'schema.table'"):
        await Schema.has_columns("a.b.c", ["id"])
    with pytest.raises(ValueError, match="expected 'table' or 'schema.table'"):
        await Schema.has_index("a.b.c", ["id"])

    with pytest.raises(ValueError, match="must not be empty"):
        await Schema.has_index("users", [])
    bad_idx_arg: Any = 123
    with pytest.raises(TypeError, match="index must be"):
        await Schema.has_index("users", bad_idx_arg)
    with pytest.raises(TypeError, match="Specify at most one"):
        await Schema.has_index("users", ["email"], "unique", type="unique")
    with pytest.raises(ValueError, match="Index type filter must not be empty"):
        await Schema.has_index("users", ["email"], "   ")
    with pytest.raises(TypeError, match="Index type filter must be a string"):
        await Schema.has_index("users", ["email"], type=bad_idx_arg)


# ---------------------------------------------------------------------------
# PostgreSQL Integration Tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def v040_table(
    ensure_db_pool: DatabasePool | None,
) -> AsyncGenerator[tuple[str, DatabasePool], None]:
    if ensure_db_pool is None:
        pytest.skip("PostgreSQL service is not available in local test environment")

    table_name = f"test_v040_{uuid.uuid4().hex[:8]}"
    await Schema.drop_if_exists(table_name)
    try:
        yield table_name, ensure_db_pool
    finally:
        await Schema.drop_if_exists(table_name)


@pytest.mark.asyncio
async def test_pg_has_table_has_columns_and_has_index_comprehensive(
    v040_table: tuple[str, DatabasePool],
) -> None:
    """Verify has_columns and has_index edge cases (empty table, duplicates, tuples, schema-qualified, partial/expr, other tables)."""
    table_name, db_pool = v040_table
    other_table = f"test_v040_other_{uuid.uuid4().hex[:8]}"
    custom_schema = f"test_schema_{uuid.uuid4().hex[:8]}"
    schema_table = f"{custom_schema}.scoped_items"

    assert not await Schema.has_table(table_name)
    assert not await Schema.has_columns(table_name, ["id"])

    try:
        def define(table: Blueprint) -> None:
            table.id()
            table.string("name", 25)
            table.string("email").unique()
            table.big_integer("org_id")
            table.string("slug", 60).index(f"idx_{table_name}_slug")
            table.string("status", 20).default("active")
            table.jsonb("options")
            table.index(["org_id", "slug"], name=f"idx_{table_name}_org_slug")

        await Schema.create(table_name, define)
        await Schema.create(
            other_table,
            lambda t: (
                t.id(),
                t.string("unrelated_col").index(f"idx_{other_table}_unrelated"),
            ),
        )

        # Create GIN index, partial index, expression index, same-columns alternate index, and custom schema table
        async with db_pool.get_pool().acquire() as conn:
            await conn.execute(
                f'CREATE INDEX "idx_{table_name}_options_gin" ON "{table_name}" USING gin ("options");'
            )
            await conn.execute(
                f'CREATE INDEX "idx_{table_name}_slug_alt" ON "{table_name}" ("slug");'
            )
            await conn.execute(
                f'CREATE INDEX "idx_{table_name}_partial_status" ON "{table_name}" ("status") WHERE "status" = \'active\';'
            )
            await conn.execute(
                f'CREATE INDEX "idx_{table_name}_expr_lower_name" ON "{table_name}" (lower("name"));'
            )
            await conn.execute(f'CREATE SCHEMA "{custom_schema}";')
            await conn.execute(
                f"""
                CREATE TABLE "{custom_schema}"."scoped_items" (
                    "id" BIGSERIAL PRIMARY KEY,
                    "code" VARCHAR(40) NOT NULL UNIQUE,
                    "meta" JSONB NOT NULL DEFAULT '{{}}'::jsonb
                );
                """
            )
            await conn.execute(
                f'CREATE INDEX "idx_scoped_meta_gin" ON "{custom_schema}"."scoped_items" USING gin ("meta");'
            )

        # Empty table (0 rows) still reports columns accurately; tuples & duplicates work
        assert await Schema.has_table(table_name)
        assert await Schema.has_column(table_name, "email")
        assert await Schema.has_columns(table_name, ["id", "name", "email", "options"])
        assert await Schema.has_columns(table_name, ("id", "name", "options"))
        assert await Schema.has_columns(table_name, ["id", "id", "name"])
        assert not await Schema.has_columns(table_name, ["id", "missing_col"])
        assert not await Schema.has_columns(table_name, ["missing_col", "missing_col"])

        # Schema-qualified lookups (public.<table_name> and <custom_schema>.<table_name>)
        assert await Schema.has_table(f"public.{table_name}")
        assert await Schema.has_column(f"public.{table_name}", "email")
        assert await Schema.has_columns(f"public.{table_name}", ("id", "slug"))
        assert await Schema.has_table(schema_table)
        assert await Schema.has_column(schema_table, "code")
        assert await Schema.has_columns(schema_table, ["id", "code", "meta"])
        assert not await Schema.has_columns(schema_table, ["id", "nonexistent"])
        assert await Schema.has_index(schema_table, ["id"], type="primary")
        assert await Schema.has_index(schema_table, ["code"], type="unique")
        assert await Schema.has_index(schema_table, "idx_scoped_meta_gin", type="gin")
        assert await Schema.has_index(schema_table, ["meta"], type="gin")
        assert not await Schema.has_table(f"{custom_schema}.missing_tbl")

        # Cross-table isolation: index on other_table never matches table_name
        assert await Schema.has_index(other_table, f"idx_{other_table}_unrelated")
        assert not await Schema.has_index(table_name, f"idx_{other_table}_unrelated")
        assert not await Schema.has_index(table_name, ["unrelated_col"])

        # Type isolation: primary vs unique vs btree vs gin
        assert await Schema.has_index(table_name, ["id"], "primary")
        assert await Schema.has_index(table_name, ["id"], type="primary")
        assert not await Schema.has_index(table_name, ["email"], "primary")
        assert not await Schema.has_index(table_name, ["id"], "gin")

        assert await Schema.has_index(table_name, ["email"], "unique")
        assert await Schema.has_index(table_name, ["email"], type="unique")
        assert not await Schema.has_index(table_name, ["slug"], "unique")
        assert not await Schema.has_index(table_name, ["email"], "gin")

        # Exact column order and duplicate column sequence in has_index
        assert await Schema.has_index(table_name, f"idx_{table_name}_slug")
        assert await Schema.has_index(table_name, f"idx_{table_name}_slug_alt")
        assert await Schema.has_index(table_name, ["slug"])
        assert not await Schema.has_index(table_name, ["slug", "slug"])
        assert await Schema.has_index(table_name, ["org_id", "slug"])
        assert await Schema.has_index(table_name, f"idx_{table_name}_org_slug")
        assert not await Schema.has_index(table_name, ["slug", "org_id"])
        assert await Schema.has_index(f"public.{table_name}", ["org_id", "slug"])
        assert not await Schema.has_index(table_name, "idx_nonexistent")

        # Partial index matches both by name and column; expression index matches by name only
        assert await Schema.has_index(table_name, f"idx_{table_name}_partial_status")
        assert await Schema.has_index(table_name, ["status"])
        assert await Schema.has_index(table_name, f"idx_{table_name}_expr_lower_name")
        assert not await Schema.has_index(table_name, ["name"])

        # Access method filters ("btree" vs "gin")
        assert await Schema.has_index(table_name, ["slug"], "btree")
        assert not await Schema.has_index(table_name, ["slug"], "gin")
        assert await Schema.has_index(table_name, ["options"], "gin")
        assert await Schema.has_index(table_name, f"idx_{table_name}_options_gin", type="gin")
        assert not await Schema.has_index(table_name, ["options"], "btree")
        assert not await Schema.has_index(table_name, ["options"], "unique")
        assert not await Schema.has_index(table_name, ["options"], "primary")
    finally:
        await Schema.drop_if_exists(other_table)
        async with db_pool.get_pool().acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{custom_schema}" CASCADE;')


@pytest.mark.asyncio
async def test_pg_binary_all_types_null_empty_and_query_builder(
    v040_table: tuple[str, DatabasePool],
) -> None:
    """Verify binary() defaults for bytes/bytearray/memoryview, NULL, empty bytes, and Query round-trips."""
    table_name, _db_pool = v040_table

    def define(table: Blueprint) -> None:
        table.id()
        table.string("label", 50)
        table.binary("photo").default(b"\xca\xfe")
        table.binary("signature").default(bytearray(b"\xbe\xef"))
        table.binary("document").default(memoryview(b"\xde\xad"))
        table.binary("optional_blob").nullable()
        table.binary("empty_blob").default(b"")

    await Schema.create(table_name, define)

    # Defaults materialized on insert when columns omitted
    await Query.table(table_name).insert({"label": "defaults"})
    # Explicit bytes, empty bytes, and NULL insert
    payload = b"\x89PNG\r\n\x1a\n\x00\x01"
    await Query.table(table_name).insert(
        {
            "label": "explicit",
            "photo": payload,
            "signature": bytearray(b"\x01\x02\x03"),
            "document": memoryview(b"\x04\x05"),
            "optional_blob": None,
            "empty_blob": b"",
        }
    )

    rows = await Query.table(table_name).order_by("id", "ASC").get()
    assert len(rows) == 2
    assert rows[0]["photo"] == b"\xca\xfe"
    assert rows[0]["signature"] == b"\xbe\xef"
    assert rows[0]["document"] == b"\xde\xad"
    assert rows[0]["optional_blob"] is None
    assert rows[0]["empty_blob"] == b""

    assert rows[1]["photo"] == payload
    assert rows[1]["signature"] == b"\x01\x02\x03"
    assert rows[1]["document"] == b"\x04\x05"
    assert rows[1]["optional_blob"] is None
    assert rows[1]["empty_blob"] == b""

    # Update & query binary column through Query builder
    updated_blob = b"\xff\x00\xab"
    await Query.table(table_name).where("label", "explicit").update(
        {"optional_blob": updated_blob}
    )
    fetched = await Query.table(table_name).where("optional_blob", updated_blob).first()
    assert fetched is not None
    assert fetched["label"] == "explicit"
    assert fetched["optional_blob"] == updated_blob


@pytest.mark.asyncio
async def test_pg_change_modifier_preservation_and_conversion_edge_cases(
    v040_table: tuple[str, DatabasePool],
) -> None:
    """Verify .change() modifier preservation, implicit casts, failure atomicity, and constraint interactions."""
    table_name, db_pool = v040_table

    def define(table: Blueprint) -> None:
        table.id()
        table.string("name", 25).default("guest")
        table.string("email", 80).unique()
        table.string("slug", 60).default("default-slug").index(f"idx_{table_name}_slug")
        table.small_integer("rank").default(1)
        table.integer("votes").unsigned().default(5).comment("initial vote count")
        table.string("birthday", 32).nullable()
        table.string("raw_age", 32).nullable()
        table.string("code", 20).default("10")
        table.boolean("is_active").default(True)
        table.jsonb("options").default({"mode": "default"})
        table.integer("legacy_a").default(0)
        table.string("avatar").nullable()
        table.string("location").nullable()

    await Schema.create(table_name, define)

    await Query.table(table_name).insert(
        {
            "name": "Alice LongName",
            "email": "alice@example.com",
            "slug": "alice",
            "rank": 7,
            "votes": 10,
            "birthday": "2001-05-09",
            "raw_age": "42",
            "code": "25",
        }
    )

    # 1. Implicit conversion without .using(): SMALLINT -> BIGINT;
    #    preserve ALL modifiers (.unsigned, .default, .comment, .unique, .index) across .change()
    def alter_preserve(table: Blueprint) -> None:
        table.big_integer("rank").default(1).change()
        table.integer("votes").unsigned().default(1).comment("O'Reilly's preserved votes").change()
        table.string("email", 160).unique().change()
        table.string("slug", 100).default("default-slug").index(f"idx_{table_name}_slug").change()
        table.date("birthday").nullable().using("birthday::date").change()
        table.integer("raw_age").nullable().using(raw('NULLIF("raw_age", \'\')::integer')).change()
        table.drop_column(["legacy_a", "avatar", "location"])

    await Schema.table(table_name, alter_preserve)

    assert not await Schema.has_column(table_name, "legacy_a")
    assert not await Schema.has_column(table_name, "avatar")
    assert not await Schema.has_column(table_name, "location")
    assert await Schema.has_index(table_name, f"idx_{table_name}_slug")
    assert await Schema.has_index(table_name, ["email"], type="unique")

    await Query.table(table_name).insert(
        {"name": "Bob", "email": "bob@example.com"}
    )
    rows = await Query.table(table_name).order_by("id", "ASC").get()
    assert len(rows) == 2
    assert rows[0]["rank"] == 7
    assert rows[0]["birthday"] == date(2001, 5, 9)
    assert rows[0]["raw_age"] == 42
    assert rows[1]["rank"] == 1
    assert rows[1]["votes"] == 1
    assert rows[1]["is_active"] is True
    raw_opts = rows[1]["options"]
    parsed_opts = json.loads(raw_opts) if isinstance(raw_opts, str) else raw_opts
    assert parsed_opts == {"mode": "default"}

    with pytest.raises(asyncpg.CheckViolationError):
        await Query.table(table_name).insert({"name": "BadVote", "email": "bv@example.com", "votes": -1})

    # Verify apostrophe-escaped comment and resized VARCHAR(160)
    async with db_pool.get_pool().acquire() as conn:
        email_len = await conn.fetchval(
            """
            SELECT character_maximum_length
            FROM information_schema.columns
            WHERE table_name = $1 AND column_name = 'email' AND table_schema = current_schema();
            """,
            table_name,
        )
        assert email_len == 160
        preserved_comment = await conn.fetchval(
            """
            SELECT col_description(c.oid, a.attnum)
            FROM pg_class c
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE c.relname = $1 AND a.attname = 'votes';
            """,
            table_name,
        )
        assert preserved_comment == "O'Reilly's preserved votes"

    # 2. Failed conversion with .using() ("bad-int" -> INTEGER) rolls back ENTIRE alter atomically
    await Query.table(table_name).insert(
        {"name": "BadCodeRow", "email": "badcode@example.com", "code": "not-an-int"}
    )
    with pytest.raises(asyncpg.InvalidTextRepresentationError):
        await Schema.table(
            table_name,
            lambda t: (
                t.integer("votes").unsigned().default(99).comment("should rollback").change(),
                t.integer("code").using("code::integer").change(),
            ),
        )
    # Votes comment & default remain untouched after failed Schema.table
    async with db_pool.get_pool().acquire() as conn:
        comment_after_fail = await conn.fetchval(
            """
            SELECT col_description(c.oid, a.attnum)
            FROM pg_class c
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE c.relname = $1 AND a.attname = 'votes';
            """,
            table_name,
        )
        assert comment_after_fail == "O'Reilly's preserved votes"

    # 3. Shrinking VARCHAR when existing value exceeds length fails & rolls back
    with pytest.raises(asyncpg.StringDataRightTruncationError):
        await Schema.table(table_name, lambda t: t.string("name", 5).change())

    # 4. NULL -> NOT NULL when NULL values exist fails & rolls back
    with pytest.raises(asyncpg.NotNullViolationError):
        await Schema.table(table_name, lambda t: t.integer("raw_age").nullable(False).change())

    # 5. Modifying non-existent column fails & rolls back earlier statements in same blueprint
    with pytest.raises(asyncpg.UndefinedColumnError):
        await Schema.table(
            table_name,
            lambda t: (
                t.integer("votes").nullable().change(),
                t.string("does_not_exist", 20).change(),
            ),
        )
    # Votes check constraint is STILL enforced after rollback of partial alter
    with pytest.raises(asyncpg.CheckViolationError):
        await Query.table(table_name).insert({"name": "StillGuarded", "email": "sg@example.com", "votes": -2})

    # 6. Dependent view blocks ALTER COLUMN TYPE cleanly & leaves column intact
    view_name = f"view_{table_name}"
    async with db_pool.get_pool().acquire() as conn:
        await conn.execute(f'CREATE VIEW "{view_name}" AS SELECT "id", "name" FROM "{table_name}";')
    try:
        with pytest.raises(asyncpg.FeatureNotSupportedError):
            await Schema.table(table_name, lambda t: t.string("name", 100).change())
    finally:
        async with db_pool.get_pool().acquire() as conn:
            await conn.execute(f'DROP VIEW IF EXISTS "{view_name}";')

    # 7. Drop unsigned constraint, then re-add replaced unsigned constraint, then toggle nullable <-> NOT NULL
    await Schema.table(
        table_name,
        lambda t: t.integer("votes").nullable().change(),
    )
    await Query.table(table_name).insert({"name": "NegVote", "email": "neg@example.com", "votes": -9})
    await Query.table(table_name).insert({"name": "NullVote", "email": "nv@example.com", "votes": None})

    # Backfill negative/null values and restore unsigned + NOT NULL
    await Query.table(table_name).where("email", "neg@example.com").update({"votes": 0})
    await Query.table(table_name).where("email", "nv@example.com").update({"votes": 0})
    await Schema.table(
        table_name,
        lambda t: t.integer("votes").unsigned().default(0).nullable(False).comment("restored").change(),
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await Query.table(table_name).insert({"name": "ReGuarded", "email": "rg@example.com", "votes": -1})

    await Schema.drop_if_exists(table_name)
    assert not await Schema.has_table(table_name)
