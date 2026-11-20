# tests/migrations/test_migrations.py

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import asyncpg
import pytest

from app.providers.app_service_provider import AppServiceProvider
from app.providers.database_service_provider import DatabaseServiceProvider
from swiftpy.console.cli import CLI
from swiftpy.console.commands.make import MakeCommand
from swiftpy.core.bootstrap import create_app, get_container
from swiftpy.database.migrations import (
    Blueprint,
    ColumnDefinition,
    MigrationLock,
    MigrationLockError,
    Migrator,
    Schema,
    SchemaGrammar,
    raw,
)
from swiftpy.database.orm.model import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query


@pytest.mark.asyncio
async def test_blueprint_dsl_and_schema_grammar() -> None:
    """Verify the Blueprint DSL constructs all column types, constraints, and indexes correctly."""
    print("\n==== BLUEPRINT DSL & SCHEMA GRAMMAR ====")

    blueprint = Blueprint("test_catalog", is_create=True)
    blueprint.id()
    blueprint.uuid("guid").unique()
    blueprint.string("name", 150)
    blueprint.text("description").nullable()
    blueprint.integer("quantity").default(0).unsigned()
    blueprint.big_integer("views").default(0)
    blueprint.small_integer("priority").default(1)
    blueprint.boolean("is_active").default(True)
    blueprint.decimal("price", precision=10, scale=2).default(9.99)
    blueprint.float("rating").default(0.0)
    blueprint.jsonb("metadata").default("{}")
    blueprint.date("release_date").nullable()
    blueprint.timestamps()
    blueprint.soft_deletes()
    blueprint.index(["name", "is_active"], name="idx_catalog_name_active")

    statements = SchemaGrammar.compile_create(blueprint)
    assert len(statements) == 2  # CREATE TABLE + CREATE INDEX
    create_sql = statements[0]
    index_sql = statements[1]

    assert 'CREATE TABLE "test_catalog"' in create_sql
    assert '"id" BIGSERIAL PRIMARY KEY' in create_sql
    assert '"guid" UUID NOT NULL UNIQUE' in create_sql
    assert '"name" VARCHAR(150) NOT NULL' in create_sql
    assert '"description" TEXT NULL' in create_sql
    assert '"quantity" INTEGER NOT NULL DEFAULT 0 CHECK ("quantity" >= 0)' in create_sql
    assert '"is_active" BOOLEAN NOT NULL DEFAULT TRUE' in create_sql
    assert '"price" NUMERIC(10, 2) NOT NULL DEFAULT 9.99' in create_sql
    assert '"created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP' in create_sql
    assert '"deleted_at" TIMESTAMPTZ NULL' in create_sql
    assert (
        'CREATE INDEX "idx_catalog_name_active" ON "test_catalog" ("name", "is_active");'
        in index_sql
    )

    print("PASS: Blueprint DSL constructs all column types and compiles PostgreSQL DDL")


@pytest.mark.asyncio
async def test_schema_create_alter_and_drop_table() -> None:
    """Test full DDL execution lifecycle against PostgreSQL."""
    print("\n==== SCHEMA TABLE DDL (CREATE, ALTER, DROP) ====")

    table_name = "test_e2e_products"

    # 1. Clean slate
    await Schema.drop_if_exists(table_name)
    assert not await Schema.has_table(table_name)

    # 2. Create table
    await Schema.create(
        table_name,
        lambda table: (
            table.id(),
            table.string("sku", 50).unique(),
            table.string("title"),
            table.integer("stock").default(10),
            table.timestamps(),
        ),
    )

    assert await Schema.has_table(table_name)
    assert await Schema.has_column(table_name, "id")
    assert await Schema.has_column(table_name, "sku")
    assert await Schema.has_column(table_name, "stock")
    assert not await Schema.has_column(table_name, "discount")

    # 3. Alter table
    await Schema.table(
        table_name,
        lambda table: (
            table.decimal("discount", precision=5, scale=2).default(0.0),
            table.rename_column("title", "headline"),
        ),
    )

    assert await Schema.has_column(table_name, "discount")
    assert await Schema.has_column(table_name, "headline")
    assert not await Schema.has_column(table_name, "title")

    # 4. Drop column
    await Schema.table(
        table_name,
        lambda table: (table.drop_column("discount"),),
    )
    assert not await Schema.has_column(table_name, "discount")

    # 5. Drop table
    await Schema.drop(table_name)
    assert not await Schema.has_table(table_name)

    print(
        "PASS: Schema table lifecycle (create, alter, drop) verified against PostgreSQL"
    )


@pytest.mark.asyncio
async def test_schema_drops_before_adds_and_unique_constraint() -> None:
    """Test that SchemaGrammar.compile_table executes drops before adds and handles drop_unique."""
    print("\n==== SCHEMA ALTER: DROPS BEFORE ADDS & DROP_UNIQUE ====")

    table_name = "test_alter_ordering"
    await Schema.drop_if_exists(table_name)

    # 1. Initial table
    def create_def(t: Blueprint) -> None:
        t.id()
        t.string("slug").unique(name="uq_test_slug")
        t.string("summary")

    await Schema.create(table_name, create_def)

    # 2. In one blueprint: drop unique constraint and drop+re-add column
    def alter_def(t: Blueprint) -> None:
        t.drop_unique("uq_test_slug")
        t.drop_column("summary")
        t.string("summary", 200).nullable()
        t.unique(["slug"], name="uq_test_slug_new")

    await Schema.table(table_name, alter_def)

    assert await Schema.has_column(table_name, "summary")
    await Schema.drop_if_exists(table_name)

    print(
        "PASS: Drops run before adds in alter table and drop_unique executes constraint drop"
    )


@pytest.mark.asyncio
async def test_foreign_key_dsl() -> None:
    """Test foreign key constraint definition and referential integrity DDL."""
    print("\n==== FOREIGN KEY REFERENTIAL INTEGRITY ====")

    parent_table = "test_fk_authors"
    child_table = "test_fk_books"

    await Schema.drop_if_exists(child_table)
    await Schema.drop_if_exists(parent_table)

    await Schema.create(
        parent_table,
        lambda t: (
            t.id(),
            t.string("name"),
            t.timestamps(),
        ),
    )

    await Schema.create(
        child_table,
        lambda t: (
            t.id(),
            t.big_integer("author_id"),
            t.string("title"),
            t.foreign_key(
                column="author_id",
                references="id",
                on=parent_table,
                on_delete="CASCADE",
            ),
        ),
    )

    assert await Schema.has_table(parent_table)
    assert await Schema.has_table(child_table)

    await Schema.drop_if_exists(child_table)
    await Schema.drop_if_exists(parent_table)

    print("PASS: Foreign key constraints created and dropped successfully")


@pytest.mark.asyncio
async def test_postgresql_advisory_lock(
    ensure_db_pool: DatabasePool | None = None,
) -> None:
    """Verify PostgreSQL advisory lock acquires, releases, and prevents concurrent runs."""
    print("\n==== POSTGRESQL ADVISORY LOCKING ====")

    pool_inst = ensure_db_pool or get_container().resolve(DatabasePool)
    raw_pool = pool_inst.get_pool()

    conn1 = await raw_pool.acquire()
    conn2 = await raw_pool.acquire()

    try:
        lock1 = MigrationLock(conn1, timeout=0.5)
        lock2 = MigrationLock(conn2, timeout=0.3, poll_interval=0.1)

        # Conn 1 acquires lock
        acquired = await lock1.acquire()
        assert acquired is True
        assert lock1.is_locked is True

        # Conn 2 must fail with MigrationLockError
        with pytest.raises(MigrationLockError):
            await lock2.acquire()

        # Conn 1 releases lock
        released = await lock1.release()
        assert released is True
        assert lock1.is_locked is False

        # Now Conn 2 can acquire lock
        acquired2 = await lock2.acquire()
        assert acquired2 is True
        assert lock2.is_locked is True
        await lock2.release()
    finally:
        await raw_pool.release(conn1)
        await raw_pool.release(conn2)

    print("PASS: Advisory lock acquired, concurrency blocked, and released cleanly")


@pytest.mark.asyncio
async def test_migrator_discovery_and_ordering(
    tmp_path: Path | None = None,
    ensure_db_pool: DatabasePool | None = None,
) -> None:
    """Verify migration files are discovered and sorted strictly by timestamp prefix."""
    print("\n==== MIGRATOR DISCOVERY & CHRONOLOGICAL ORDERING ====")

    target_dir = tmp_path or Path(tempfile.mkdtemp())
    m_dir = target_dir / "migrations"
    m_dir.mkdir(parents=True, exist_ok=True)

    f3 = m_dir / "2026_01_03_000000_third_migration.py"
    f1 = m_dir / "2026_01_01_000000_first_migration.py"
    f2 = m_dir / "2026_01_02_000000_second_migration.py"
    ignored = m_dir / "_ignored_helper.py"

    for f in (f3, f1, f2, ignored):
        f.write_text("# migration stub", encoding="utf-8")

    pool_inst = ensure_db_pool or get_container().resolve(DatabasePool)
    migrator = Migrator(pool=pool_inst, migrations_dir=m_dir)
    discovered = migrator.get_migration_files()

    assert len(discovered) == 3
    assert discovered[0].name == "2026_01_01_000000_first_migration"
    assert discovered[1].name == "2026_01_02_000000_second_migration"
    assert discovered[2].name == "2026_01_03_000000_third_migration"

    print(
        "PASS: Migration files discovered and sorted chronologically by timestamp prefix"
    )


@pytest.mark.asyncio
async def test_migrator_run_rollback_and_idempotency(
    tmp_path: Path | None = None,
    ensure_db_pool: DatabasePool | None = None,
) -> None:
    """
    Test that migrations run forward and backward completely, and verify
    idempotency: running migrate twice in a row does nothing the second time.
    """
    print("\n==== MIGRATOR RUN, ROLLBACK & IDEMPOTENCY ====")

    target_dir = tmp_path or Path(tempfile.mkdtemp())
    m_dir = target_dir / "migrations"
    m_dir.mkdir(parents=True, exist_ok=True)

    # Migration 1: create users table
    m1 = m_dir / "2026_10_18_000001_create_mig_users_table.py"
    m1.write_text(
        """from swiftpy.database.migrations import Migration, Schema

class CreateMigUsersTable(Migration):
    async def up(self) -> None:
        await Schema.create("mig_users", lambda t: (
            t.id(),
            t.string("name"),
            t.timestamps(),
        ))

    async def down(self) -> None:
        await Schema.drop_if_exists("mig_users")
""",
        encoding="utf-8",
    )

    # Migration 2: create posts table
    m2 = m_dir / "2026_10_18_000002_create_mig_posts_table.py"
    m2.write_text(
        """from swiftpy.database.migrations import Migration, Schema

class CreateMigPostsTable(Migration):
    async def up(self) -> None:
        await Schema.create("mig_posts", lambda t: (
            t.id(),
            t.big_integer("user_id"),
            t.string("title"),
            t.timestamps(),
        ))

    async def down(self) -> None:
        await Schema.drop_if_exists("mig_posts")
""",
        encoding="utf-8",
    )

    pool_inst = ensure_db_pool or get_container().resolve(DatabasePool)
    migrator = Migrator(
        pool=pool_inst, migrations_dir=m_dir, table_name="test_migrations_table"
    )

    # Cleanup prior runs
    await Schema.drop_if_exists("mig_posts")
    await Schema.drop_if_exists("mig_users")
    await Schema.drop_if_exists("test_migrations_table")

    # 1. Run migrations batch 1
    applied = await migrator.run()
    assert len(applied) == 2
    assert "2026_10_18_000001_create_mig_users_table" in applied
    assert "2026_10_18_000002_create_mig_posts_table" in applied

    assert await Schema.has_table("mig_users")
    assert await Schema.has_table("mig_posts")

    # 2. Idempotency test: running migrate twice in a row does nothing
    applied_second = await migrator.run()
    assert applied_second == []
    assert len(applied_second) == 0

    # 3. Add a third migration in batch 2
    m3 = m_dir / "2026_10_18_000003_create_mig_comments_table.py"
    m3.write_text(
        """from swiftpy.database.migrations import Migration, Schema

class CreateMigCommentsTable(Migration):
    async def up(self) -> None:
        await Schema.create("mig_comments", lambda t: (
            t.id(),
            t.string("body"),
        ))

    async def down(self) -> None:
        await Schema.drop_if_exists("mig_comments")
""",
        encoding="utf-8",
    )

    applied_batch2 = await migrator.run()
    assert applied_batch2 == ["2026_10_18_000003_create_mig_comments_table"]
    assert await Schema.has_table("mig_comments")

    # 4. Status check
    statuses = await migrator.status()
    assert len(statuses) == 3
    assert all(s["ran"] is True for s in statuses)
    assert statuses[0]["batch"] == 1
    assert statuses[1]["batch"] == 1
    assert statuses[2]["batch"] == 2

    # 5. Rollback batch 2 only
    rolled_back_batch2 = await migrator.rollback(steps=1)
    assert rolled_back_batch2 == ["2026_10_18_000003_create_mig_comments_table"]
    assert not await Schema.has_table("mig_comments")
    assert await Schema.has_table("mig_users")  # Still present from batch 1
    assert await Schema.has_table("mig_posts")

    # 6. Rollback batch 1
    rolled_back_batch1 = await migrator.rollback(steps=1)
    assert len(rolled_back_batch1) == 2
    assert not await Schema.has_table("mig_users")
    assert not await Schema.has_table("mig_posts")

    # 7. Rollback when nothing left
    rolled_back_empty = await migrator.rollback(steps=1)
    assert rolled_back_empty == []

    # Cleanup
    await Schema.drop_if_exists("test_migrations_table")

    print("PASS: Migrations run forward, backward, and verified strictly idempotent")


@pytest.mark.asyncio
async def test_cli_make_migrate_status_rollback_flow(
    tmp_path: Path | None = None,
    ensure_db_pool: DatabasePool | None = None,
) -> None:
    """Verify the full CLI workflow via argparse dispatch: make:migration -> migrate -> migrate:status -> migrate:rollback."""
    print("\n==== CLI MAKE, MIGRATE, STATUS & ROLLBACK FLOW ====")

    target_dir = tmp_path or Path(tempfile.mkdtemp())
    m_dir = target_dir / "cli_migrations"

    cli = CLI()

    # 1. Generate migration using `swiftpy make:migration`
    cli.run(["make:migration", "create_cli_widgets_table", "--path", str(m_dir)])

    generated_files = list(m_dir.glob("*_create_cli_widgets_table.py"))
    assert len(generated_files) == 1
    migration_file = generated_files[0]
    content = migration_file.read_text(encoding="utf-8")
    assert "class CreateCliWidgetsTable(Migration):" in content
    assert 'await Schema.create("cli_widgets"' in content

    # Clean slate in DB
    await Schema.drop_if_exists("cli_widgets")

    # 2. Run migration using `swiftpy migrate`
    cli.run(["migrate", "--path", str(m_dir)])
    assert await Schema.has_table("cli_widgets")

    # 3. Check status using `swiftpy migrate:status`
    cli.run(["migrate:status", "--path", str(m_dir)])

    # 4. Idempotency test via CLI: running migrate a second time does nothing
    cli.run(["migrate", "--path", str(m_dir)])
    assert await Schema.has_table("cli_widgets")

    # 5. Rollback using `swiftpy migrate:rollback`
    cli.run(["migrate:rollback", "--path", str(m_dir)])
    assert not await Schema.has_table("cli_widgets")

    # Cleanup
    await Schema.drop_if_exists("cli_widgets")

    print("PASS: Full CLI migration workflow verified via argparse command dispatch")


@pytest.mark.asyncio
async def test_concurrent_runs_apply_each_migration_once(
    tmp_path: Path | None = None,
    ensure_db_pool: DatabasePool | None = None,
) -> None:
    """Verify advisory lock ensures exactly one runner applies a pending migration during concurrency."""
    print("\n==== CONCURRENT MIGRATION RUNS (ADVISORY LOCK DISCRIMINATION) ====")

    target_dir = tmp_path or Path(tempfile.mkdtemp())
    slow_content = """from __future__ import annotations

import asyncio

from swiftpy.database.migrations import Migration


class SlowMigration(Migration):
    async def up(self) -> None:
        await asyncio.sleep(0.5)

    async def down(self) -> None:
        pass
"""
    (target_dir / "2026_01_01_000001_slow.py").write_text(
        slow_content, encoding="utf-8"
    )

    pool_inst = ensure_db_pool or get_container().resolve(DatabasePool)
    runners = [
        Migrator(pool=pool_inst, migrations_dir=target_dir, table_name="mig_lock_test")
        for _ in range(3)
    ]
    try:
        results = await asyncio.gather(*(m.run() for m in runners))
        assert sorted(len(r) for r in results) == [0, 0, 1]
    finally:
        async with pool_inst.get_pool().acquire() as conn:
            await conn.execute('DROP TABLE IF EXISTS "mig_lock_test"')

    print(
        "PASS: Advisory lock prevents concurrent runners from applying the same migration twice"
    )


@pytest.mark.asyncio
async def test_make_migration_generated_file_runs_and_rolls_back(
    tmp_path: Path | None = None,
    ensure_db_pool: DatabasePool | None = None,
) -> None:
    """Verify generated migration file runs up() creating table, and down() dropping table."""
    print("\n==== GENERATED MIGRATION FILE EXECUTION & ROLLBACK ====")

    target_dir = tmp_path or Path(tempfile.mkdtemp())
    args = argparse.Namespace(
        name="create_widgets_table",
        path=str(target_dir),
        create=None,
        table=None,
    )
    MakeCommand.handle_migration(args)
    files = list(target_dir.glob("*_create_widgets_table.py"))
    assert len(files) == 1

    pool_inst = ensure_db_pool or get_container().resolve(DatabasePool)
    await Schema.drop_if_exists("widgets")
    migrator = Migrator(
        pool=pool_inst, migrations_dir=target_dir, table_name="mig_widget_test"
    )
    try:
        applied = await migrator.run()
        assert len(applied) == 1
        assert await Schema.has_table("widgets")

        rolled_back = await migrator.rollback(steps=1)
        assert len(rolled_back) == 1
        assert not await Schema.has_table("widgets")
    finally:
        await Schema.drop_if_exists("widgets")
        async with pool_inst.get_pool().acquire() as conn:
            await conn.execute('DROP TABLE IF EXISTS "mig_widget_test"')

    print("PASS: Generated migration from make:migration successfully runs up and down")


@pytest.mark.asyncio
async def test_reserved_word_columns_round_trip(
    ensure_db_pool: DatabasePool | None = None,
) -> None:
    """Verify DDL and Query builder properly quote reserved identifiers (e.g. 'from', 'to')."""
    print("\n==== RESERVED WORD IDENTIFIERS ROUND-TRIP ====")

    def define(table: Blueprint) -> None:
        table.id()
        table.string("from")
        table.string("to")

    await Schema.drop_if_exists("messages")
    await Schema.create("messages", define)
    try:
        await Query.table("messages").insert({"from": "a", "to": "b"})
        row = await Query.table("messages").where("from", "a").first()
        assert row is not None
        assert row["to"] == "b"

        await Query.table("messages").where("from", "a").update({"to": "c"})
        updated = await Query.table("messages").where("from", "a").first()
        assert updated is not None
        assert updated["to"] == "c"
    finally:
        await Schema.drop_if_exists("messages")

    print("PASS: Reserved words quoted properly in DDL and DML queries")


@pytest.mark.asyncio
async def test_unsigned_check_and_comment(
    ensure_db_pool: DatabasePool | None = None,
) -> None:
    """Verify column unsigned CHECK constraint and COMMENT ON COLUMN."""
    print("\n==== UNSIGNED CHECK CONSTRAINTS & COMMENTS ====")

    pool_inst = ensure_db_pool or get_container().resolve(DatabasePool)
    table_name = "test_col_unsigned_comment"
    await Schema.drop_if_exists(table_name)

    def define(t: Blueprint) -> None:
        t.id()
        t.integer("score").unsigned().comment("User score, non-negative")

    await Schema.create(table_name, define)

    try:
        async with pool_inst.get_pool().acquire() as conn:
            comment = await conn.fetchval(
                """
                SELECT col_description(c.oid, a.attnum)
                FROM pg_class c
                JOIN pg_attribute a ON a.attrelid = c.oid
                WHERE c.relname = $1 AND a.attname = 'score';
                """,
                table_name,
            )
            assert comment == "User score, non-negative"

        await Query.table(table_name).insert({"score": 10})
        row = await Query.table(table_name).first()
        assert row is not None
        assert row["score"] == 10

        with pytest.raises(asyncpg.CheckViolationError):
            await Query.table(table_name).insert({"score": -5})
    finally:
        await Schema.drop_if_exists(table_name)

    print("PASS: Unsigned CHECK constraint and COMMENT ON COLUMN verified")


@pytest.mark.asyncio
async def test_composite_primary_key(
    ensure_db_pool: DatabasePool | None = None,
) -> None:
    """Verify composite primary key declaration on pivot tables."""
    print("\n==== COMPOSITE PRIMARY KEY ====")

    table_name = "test_post_tag_pivot"
    await Schema.drop_if_exists(table_name)

    def define(t: Blueprint) -> None:
        t.big_integer("post_id")
        t.big_integer("tag_id")
        t.primary(["post_id", "tag_id"], name="pk_post_tag")

    await Schema.create(table_name, define)

    try:
        await Query.table(table_name).insert({"post_id": 1, "tag_id": 10})
        with pytest.raises(asyncpg.UniqueViolationError):
            await Query.table(table_name).insert({"post_id": 1, "tag_id": 10})

        await Query.table(table_name).insert({"post_id": 1, "tag_id": 20})
        count = await Query.table(table_name).count()
        assert count == 2
    finally:
        await Schema.drop_if_exists(table_name)

    print("PASS: Composite primary key constraints on pivot tables verified")


@pytest.mark.asyncio
async def test_updated_at_maintained_on_save(
    ensure_db_pool: DatabasePool | None = None,
) -> None:
    """Verify Model.save() updates updated_at timestamp when modifying fields."""
    print("\n==== MODEL UPDATED_AT MAINTAINED ON SAVE ====")

    table_name = "test_timestamp_users"
    await Schema.drop_if_exists(table_name)

    def define(t: Blueprint) -> None:
        t.id()
        t.string("name")
        t.timestamps()

    await Schema.create(table_name, define)

    class TimestampUser(Model):
        __table__ = table_name
        id: int
        name: str
        created_at: datetime
        updated_at: datetime
        __guarded__: ClassVar[list[str]] = []

    try:
        user = await TimestampUser.create({"name": "Alice"})
        initial_created = user.created_at
        initial_updated = user.updated_at
        assert initial_created is not None
        assert initial_updated is not None

        await asyncio.sleep(0.05)
        user.name = "Alice Smith"
        await user.save()

        assert user.name == "Alice Smith"
        assert user.created_at == initial_created
        assert user.updated_at > initial_updated

        fresh = await TimestampUser.find(user.id)
        assert fresh is not None
        assert fresh.name == "Alice Smith"
        assert fresh.updated_at > initial_updated
    finally:
        await Schema.drop_if_exists(table_name)

    print("PASS: Model updated_at automatically maintained upon update save")


def test_blueprint_validation_and_raw_defaults() -> None:
    """Verify length/precision/scale range validation, type_name whitelist, 63-char identifier limit, and raw() defaults."""
    bp = Blueprint("valid_table", is_create=True)

    # 1. Invalid string lengths
    with pytest.raises(ValueError, match="Column length"):
        bp.string("bad_len_zero", 0)
    with pytest.raises(ValueError, match="Column length"):
        bp.string("bad_len_neg", -5)
    with pytest.raises(ValueError, match="Column length"):
        bp.string("bad_len_huge", 20_000_000)
    with pytest.raises(ValueError, match="Column length"):
        bp.string("bad_len_bool", True)

    # 2. Invalid decimal precision/scale
    with pytest.raises(ValueError, match="Decimal precision"):
        bp.decimal("bad_prec_zero", precision=0, scale=0)
    with pytest.raises(ValueError, match="Decimal precision"):
        bp.decimal("bad_prec_huge", precision=1001, scale=2)
    with pytest.raises(ValueError, match="Decimal scale"):
        bp.decimal("bad_scale_neg", precision=8, scale=-1)
    with pytest.raises(ValueError, match="Decimal scale"):
        bp.decimal("bad_scale_gt_prec", precision=5, scale=6)

    # 3. Unknown type_name rejected
    with pytest.raises(ValueError, match="Unsupported column type"):
        ColumnDefinition("col", "varchar(50); DROP TABLE users;--")

    # 4. Identifier > 63 chars rejected
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        Blueprint("a" * 64)
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        bp.string("b" * 64)

    # Generated index name > 63 chars rejected during compilation
    long_bp = Blueprint("a" * 40, is_create=True)
    long_bp.string("b" * 30).index()
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        SchemaGrammar.compile_create(long_bp)

    # 5. raw() default vs deprecated magic string vs literal escaping
    bp_defaults = Blueprint("defaults_table", is_create=True)
    bp_defaults.timestamp("ts_raw").default(raw("now()"))
    bp_defaults.string("author").default("O'Reilly")
    sql_list = SchemaGrammar.compile_create(bp_defaults)
    assert '"ts_raw" TIMESTAMPTZ NOT NULL DEFAULT now()' in sql_list[0]
    assert "\"author\" VARCHAR(255) NOT NULL DEFAULT 'O''Reilly'" in sql_list[0]

    with pytest.deprecated_call(match="use raw"):
        assert SchemaGrammar._format_default_value("NOW()") == "NOW()"

    with pytest.raises(ValueError, match="finite"):
        SchemaGrammar._format_default_value(float("nan"))


def test_enum_long_text_and_no_after_sql_compilation() -> None:
    """Verify Blueprint.enum(), long_text()/medium_text() TEXT compilation, validation, and no .after()."""
    # 1. PostgreSQL does not support column placement via .after() (G-1 decision: not implemented)
    assert not hasattr(Blueprint("orders"), "after")
    assert not hasattr(ColumnDefinition("status", "string"), "after")

    # 2. long_text and medium_text compile identically to PostgreSQL TEXT
    bp_text = Blueprint("articles", is_create=True)
    bp_text.text("summary")
    bp_text.medium_text("excerpt").nullable()
    bp_text.long_text("content")
    text_sql = SchemaGrammar.compile_create(bp_text)[0]
    assert '"summary" TEXT NOT NULL' in text_sql
    assert '"excerpt" TEXT NULL' in text_sql
    assert '"content" TEXT NOT NULL' in text_sql

    # 3. enum() compiles to VARCHAR(255) + named CHECK constraint + literal escaping
    bp_enum = Blueprint("orders", is_create=True)
    bp_enum.id()
    bp_enum.enum("status", ["pending", "paid", "failed"]).default("pending")
    bp_enum.enum("label", ["draft", "owner's", "TRUE"]).default("owner's")
    bp_enum.enum("note_kind", ["a", "b"]).default(None).nullable()
    enum_sql = SchemaGrammar.compile_create(bp_enum)[0]

    assert (
        '"status" VARCHAR(255) NOT NULL DEFAULT \'pending\' '
        'CONSTRAINT "orders_status_check" CHECK ("status" IN (\'pending\', \'paid\', \'failed\'))'
    ) in enum_sql
    assert (
        '"label" VARCHAR(255) NOT NULL DEFAULT \'owner\'\'s\' '
        'CONSTRAINT "orders_label_check" CHECK ("label" IN (\'draft\', \'owner\'\'s\', \'TRUE\'))'
    ) in enum_sql
    assert (
        '"note_kind" VARCHAR(255) NULL DEFAULT NULL '
        'CONSTRAINT "orders_note_kind_check" CHECK ("note_kind" IN (\'a\', \'b\'))'
    ) in enum_sql

    # 4. Keyword-like enum default stays a quoted SQL literal instead of a bare keyword
    bp_kw = Blueprint("kw_table", is_create=True)
    bp_kw.enum("flag", ["TRUE", "FALSE", "NULL"]).default("TRUE")
    kw_sql = SchemaGrammar.compile_create(bp_kw)[0]
    assert '"flag" VARCHAR(255) NOT NULL DEFAULT \'TRUE\'' in kw_sql

    # 5. Enum validation rejections
    bad_bp = Blueprint("bad_enums", is_create=True)
    with pytest.raises(ValueError, match="non-empty"):
        bad_bp.enum("empty", [])
    with pytest.raises(TypeError, match="bare string"):
        bad_bp.enum("bare_str", "active")
    with pytest.raises(ValueError, match="Duplicate"):
        bad_bp.enum("dups", ["a", "b", "a"])
    with pytest.raises(TypeError, match="strings"):
        bad_bp.enum("non_str", json.loads('["ok", 42]'))
    with pytest.raises(ValueError, match="enum values"):
        bad_bp.enum("status", ["pending", "paid"]).default("archived")

    # Non-nullable enum with default(None) fails during compilation
    non_null_bp = Blueprint("bad_null_default", is_create=True)
    non_null_bp.enum("status", ["pending", "paid"]).default(None)
    with pytest.raises(ValueError, match="Non-nullable"):
        SchemaGrammar.compile_create(non_null_bp)

    # ColumnDefinition("x", "enum") without enum_values fails on compile
    with pytest.raises(ValueError, match="non-empty"):
        SchemaGrammar._compile_column(ColumnDefinition("orphan_enum", "enum"))

    # ALTER TABLE ADD COLUMN emits named CHECK constraint on enum
    alter_bp = Blueprint("orders", is_create=False)
    alter_bp.enum("priority", ["low", "high"]).default("low")
    alter_sql = SchemaGrammar.compile_table(alter_bp)
    assert (
        'ALTER TABLE "orders" ADD COLUMN "priority" VARCHAR(255) NOT NULL '
        "DEFAULT 'low' CONSTRAINT \"orders_priority_check\" CHECK (\"priority\" IN ('low', 'high'));"
    ) in alter_sql


@pytest.mark.asyncio
async def test_enum_and_long_text_pg_integration(
    ensure_db_pool: DatabasePool,
) -> None:
    """Verify enum CHECK constraints, long_text storage, and ALTER TABLE enum against PostgreSQL."""
    assert ensure_db_pool is not None
    table_name = "test_enum_items"
    await Schema.drop_if_exists(table_name)

    def create_def(t: Blueprint) -> None:
        t.id()
        t.enum("status", ["draft", "published"]).default("draft")
        t.medium_text("excerpt").nullable()
        t.long_text("content")
        t.timestamps()

    await Schema.create(table_name, create_def)

    try:
        # 1. Valid default and explicit enum + large payload (> 65KB TEXT)
        large_content = "x" * 100_000
        await Query.table(table_name).insert({"content": large_content})
        await Query.table(table_name).insert(
            {"status": "published", "excerpt": "Short", "content": "Body"}
        )

        rows = await Query.table(table_name).order_by("id", "ASC").get()
        assert len(rows) == 2
        assert rows[0]["status"] == "draft"
        assert len(rows[0]["content"]) == 100_000
        assert rows[1]["status"] == "published"

        # 2. Invalid enum value violates PostgreSQL CHECK constraint
        with pytest.raises(asyncpg.CheckViolationError):
            await Query.table(table_name).insert(
                {"status": "archived", "content": "Rejected"}
            )

        # 3. Alter table adding an enum column enforces its named CHECK constraint
        def alter_def(t: Blueprint) -> None:
            t.enum("priority", ["low", "high"]).default("low")

        await Schema.table(table_name, alter_def)
        await Query.table(table_name).insert(
            {"status": "draft", "priority": "high", "content": "Urgent"}
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await Query.table(table_name).insert(
                {"status": "draft", "priority": "critical", "content": "Rejected"}
            )
    finally:
        await Schema.drop_if_exists(table_name)


@pytest.mark.asyncio
async def test_migrator_checksum_drift_and_out_of_order(
    tmp_path: Path,
    ensure_db_pool: DatabasePool,
) -> None:
    """Verify checksum storage, checksum drift detection, out-of-order migration detection, and step validation."""
    m_dir = tmp_path / "drift_migrations"
    m_dir.mkdir(parents=True, exist_ok=True)

    m1 = m_dir / "2026_10_18_000001_first.py"
    m1_original = """from swiftpy.database.migrations import Migration

class FirstMig(Migration):
    async def up(self) -> None:
        pass
    async def down(self) -> None:
        pass
"""
    m1.write_text(m1_original, encoding="utf-8")

    m3 = m_dir / "2026_10_18_000003_third.py"
    m3.write_text(
        """from swiftpy.database.migrations import Migration

class ThirdMig(Migration):
    async def up(self) -> None:
        pass
    async def down(self) -> None:
        pass
""",
        encoding="utf-8",
    )

    migrator = Migrator(
        pool=ensure_db_pool,
        migrations_dir=m_dir,
        table_name="test_mig_checksums",
    )
    await Schema.drop_if_exists("test_mig_checksums")

    try:
        # Invalid step values rejected
        with pytest.raises(ValueError, match="step must be an integer >= 1"):
            await migrator.run(step=0)
        with pytest.raises(ValueError, match="steps must be an integer >= 1"):
            await migrator.rollback(steps=0)

        applied = await migrator.run()
        assert applied == [
            "2026_10_18_000001_first",
            "2026_10_18_000003_third",
        ]

        statuses = await migrator.status()
        assert all(s["checksum_valid"] is True for s in statuses)

        # 1. Modify already-applied m1 on disk -> checksum drift error
        m1.write_text(m1_original + "\n# modified after apply\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="checksum drift"):
            await migrator.run()

        # Restore m1
        m1.write_text(m1_original, encoding="utf-8")

        # 2. Add out-of-order unapplied migration 000002 (sorts before applied 000003)
        m2 = m_dir / "2026_10_18_000002_second_out_of_order.py"
        m2.write_text(m1_original, encoding="utf-8")
        with pytest.raises(RuntimeError, match="Out-of-order unapplied migration"):
            await migrator.run()
    finally:
        await Schema.drop_if_exists("test_mig_checksums")


@pytest.mark.asyncio
async def test_cli_step_validation_and_production_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify CLI rejects --step < 1 and refuses production migrations without --force."""
    m_dir = tmp_path / "prod_cli_migrations"
    cli = CLI()
    cli.run(["make:migration", "create_prod_guard_items_table", "--path", str(m_dir)])
    await Schema.drop_if_exists("prod_guard_items")

    try:
        # 1. Negative or zero --step exits non-zero
        with pytest.raises(SystemExit) as exc_info:
            cli.run(["migrate", "--path", str(m_dir), "--step", "0"])
        assert exc_info.value.code == 1

        with pytest.raises(SystemExit) as exc_info2:
            cli.run(["migrate", "--path", str(m_dir), "--step", "-1"])
        assert exc_info2.value.code == 1

        with pytest.raises(SystemExit) as exc_info3:
            cli.run(["migrate:rollback", "--path", str(m_dir), "--step", "0"])
        assert exc_info3.value.code == 1

        # 2. Production guard refuses migrate / rollback / reset without --force
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(SystemExit) as prod_exc:
            cli.run(["migrate", "--path", str(m_dir)])
        assert prod_exc.value.code == 1
        assert not await Schema.has_table("prod_guard_items")

        # 3. With --force, migrate and migrate:reset succeed in production
        cli.run(["migrate", "--path", str(m_dir), "--force"])
        assert await Schema.has_table("prod_guard_items")

        with pytest.raises(SystemExit) as reset_exc:
            cli.run(["migrate:reset", "--path", str(m_dir)])
        assert reset_exc.value.code == 1

        cli.run(["migrate:reset", "--path", str(m_dir), "--force"])
        assert not await Schema.has_table("prod_guard_items")
    finally:
        await Schema.drop_if_exists("prod_guard_items")


async def main() -> None:
    """Standalone runner that executes all Week 9 Migration and CLI tests with detailed status output."""
    app = create_app(providers=[AppServiceProvider, DatabaseServiceProvider])
    db_pool = app.container.resolve(DatabasePool)
    await db_pool.init()

    try:
        await test_blueprint_dsl_and_schema_grammar()
        await test_schema_create_alter_and_drop_table()
        await test_schema_drops_before_adds_and_unique_constraint()
        await test_foreign_key_dsl()
        await test_postgresql_advisory_lock(db_pool)
        await test_migrator_discovery_and_ordering(ensure_db_pool=db_pool)
        await test_migrator_run_rollback_and_idempotency(ensure_db_pool=db_pool)
        await test_cli_make_migrate_status_rollback_flow(ensure_db_pool=db_pool)
        await test_concurrent_runs_apply_each_migration_once(ensure_db_pool=db_pool)
        await test_make_migration_generated_file_runs_and_rolls_back(
            ensure_db_pool=db_pool
        )
        await test_reserved_word_columns_round_trip(ensure_db_pool=db_pool)
        await test_unsigned_check_and_comment(ensure_db_pool=db_pool)
        await test_composite_primary_key(ensure_db_pool=db_pool)
        await test_updated_at_maintained_on_save(ensure_db_pool=db_pool)

        print("\n==== ALL WEEK 9 MIGRATION SYSTEM & CLI TESTS PASSED ====\n")
    finally:
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
