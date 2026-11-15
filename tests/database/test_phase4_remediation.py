from __future__ import annotations

import argparse
import ssl
import uuid
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from swiftpy.console.commands.seed import SeedCommand
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import get_container
from swiftpy.database.migrations.blueprint import Blueprint
from swiftpy.database.migrations.lock import MigrationLock
from swiftpy.database.migrations.migrator import Migrator
from swiftpy.database.migrations.schema import SchemaGrammar
from swiftpy.database.orm import (
    BelongsTo,
    BelongsToMany,
    Field,
    HasMany,
    HasOne,
    Model,
    Vector,
)
from swiftpy.database.pool import DatabasePool, _build_ssl_context
from swiftpy.database.query_builder import Query, QueryError
from swiftpy.database.transaction import db, get_active_connection, on_commit
from swiftpy.events.dispatcher import EventDispatcher


def test_db1_having_and_where_null_sql_injection_rejected() -> None:
    """DB-1: having() validates aggregates strictly and where_null/where_not_null validate boolean."""
    with pytest.raises(ValueError, match="HAVING|identifier|Invalid"):
        Query.table("orders").group_by("user_id").having(
            "COUNT(id) > 0; DROP TABLE users; --", "=", 1
        )

    with pytest.raises(ValueError, match="HAVING|identifier|COUNT"):
        Query.table("orders").group_by("user_id").having("SUM(*)", ">", 0)

    sql_agg, bindings_agg = (
        Query.table("orders")
        .group_by("user_id")
        .having("COUNT(orders.id)", ">", 5)
        .to_sql()
    )
    assert 'HAVING COUNT("orders"."id") > $1' in sql_agg
    assert bindings_agg == [5]

    sql_alias, bindings_alias = (
        Query.table("orders")
        .select_count("*", alias="cnt")
        .group_by("user_id")
        .having("cnt", ">=", 2)
        .to_sql()
    )
    assert "HAVING COUNT(*) >= $1" in sql_alias
    assert bindings_alias == [2]

    with pytest.raises(ValueError, match="boolean"):
        Query.table("users").where_null("deleted_at", boolean="OR 1=1 --")

    with pytest.raises(ValueError, match="boolean"):
        Query.table("users").where_not_null("deleted_at", boolean="AND 1=1")


def test_db2_select_raw_and_raw_bindings_validation() -> None:
    """DB-2: select_raw validates placeholders and rejects bare str/bytes bindings."""
    with pytest.raises(TypeError, match="sequence|bindings|str"):
        Query.table("users").select_raw("1 as one", "admin")

    with pytest.raises(TypeError, match="sequence|bindings|bytes"):
        Query.table("users").where_raw("name = ?", b"admin")

    with pytest.raises(TypeError, match="sequence|bindings|str"):
        Query.table("users").having_raw("COUNT(*) > ?", "5")

    with pytest.raises(QueryError, match="Placeholder count mismatch"):
        Query.table("users").select_raw("? as a")

    with pytest.raises(QueryError, match="Placeholder count mismatch"):
        Query.table("users").select_raw("1 as one", [99])

    sql, bindings = (
        Query.table("users")
        .select_raw("COALESCE(?, ?) AS x", ["?lit", "$2"])
        .where("id", 42)
        .to_sql()
    )
    assert sql == 'SELECT COALESCE($1, $2) AS x FROM "users" WHERE "id" = $3'
    assert bindings == ["?lit", "$2", 42]


def test_db5_ssl_cafile_errors_and_transaction_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB-5: Invalid SSL CA file raises ConfigurationError; database.timeouts.transaction wires PG timeout."""
    with pytest.raises(ConfigurationError, match="SSL"):
        _build_ssl_context("verify-ca", ssl_ca_file="/nonexistent/path/to/ca.pem")

    assert isinstance(_build_ssl_context("require"), ssl.SSLContext)


@pytest.mark.asyncio
async def test_db5_pool_wires_transaction_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict[str, Any] = {}
    fake_pool = AsyncMock()

    async def capture_create_pool(**kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return fake_pool

    monkeypatch.setattr(
        "swiftpy.database.pool.asyncpg.create_pool", capture_create_pool
    )

    cfg = Config(
        {
            "database": {
                "default": "postgresql",
                "connections": {
                    "postgresql": {
                        "host": "127.0.0.1",
                        "port": 5432,
                        "database": "swiftpy",
                        "user": "postgres",
                        "password": "secret",
                    }
                },
                "timeouts": {"statement": 12.0, "transaction": 45.0},
            }
        }
    )
    pool = DatabasePool(cfg)
    await pool.init()
    assert captured_kwargs["server_settings"] == {
        "statement_timeout": "12000",
        "idle_in_transaction_session_timeout": "45000",
    }


def test_db5_unsaved_model_without_pk_is_unhashable() -> None:
    """DB-5: Hashing an unsaved Model without PK fails instead of mutating hash after save()."""

    class HashCheckItem(Model):
        __table__ = "hash_check_items"
        __guarded__: ClassVar[list[str]] = []
        id: int
        name: str

    unsaved = HashCheckItem(name="draft")
    with pytest.raises(TypeError, match="primary key|unsaved"):
        hash(unsaved)

    saved = HashCheckItem(id=7, name="saved")
    assert hash(saved) == hash((HashCheckItem, 7))


@pytest.mark.asyncio
async def test_db3_on_commit_runs_after_active_connection_closes() -> None:
    """DB-3: on_commit runs outside _active_connection after pool connection closes (no pool deadlock)."""
    cfg = Config.load()
    cfg.set("database.pool.min_size", 1)
    cfg.set("database.pool.max_size", 1)
    pool = DatabasePool(cfg)
    await pool.init()
    container = get_container()
    prev_pool = container.resolve(DatabasePool) if container.has(DatabasePool) else None
    container.instance(DatabasePool, pool)

    table = f"test_db3_{uuid.uuid4().hex[:8]}"
    observed: list[tuple[bool, int]] = []

    try:
        raw_pool = pool.get_pool()
        async with raw_pool.acquire() as conn:
            await conn.execute(
                f'CREATE TABLE "{table}" ("id" SERIAL PRIMARY KEY, "val" TEXT NOT NULL);'
            )

        async def _after_commit() -> None:
            active = get_active_connection()
            count = await Query.table(table).count()
            observed.append((active is None, count))

        async with db.transaction():
            await Query.table(table).insert({"val": "inside"})
            on_commit(_after_commit)
            assert observed == []

        assert observed == [(True, 1)]
    finally:
        raw_pool = pool.get_pool()
        async with raw_pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{table}";')
        await pool.close()
        if prev_pool is not None:
            container.instance(DatabasePool, prev_pool)


@pytest.mark.asyncio
async def test_mig1_and_mig2_migrator_transaction_connection_lock_and_status(
    ensure_db_pool: DatabasePool,
    tmp_path: Path,
) -> None:
    """MIG-1/MIG-2: Migrator binds TransactionConnection, Lock preserves primary exc, status shows missing files."""
    # MIG-2: Vector rejects bool dimensions everywhere
    with pytest.raises(ValueError, match="positive integer"):
        Vector(True)

    bp = Blueprint("bad_vec_tbl")
    with pytest.raises(ValueError, match="positive integer"):
        bp.vector("emb", True)

    # Direct SchemaGrammar check if ColumnDefinition mutated manually
    bp2 = Blueprint("manual_vec_tbl")
    col = bp2.vector("emb", 4)
    col.dimensions = True
    with pytest.raises(ValueError, match="positive integer"):
        SchemaGrammar.compile_create(bp2)

    # MIG-2: MigrationLock does not mask original exception on release failure
    fake_conn = MagicMock()
    fake_conn.is_closed.return_value = False
    fake_conn.fetchval = AsyncMock(side_effect=[True, RuntimeError("unlock dropped")])
    with pytest.raises(ValueError, match="original failure"):
        async with MigrationLock(fake_conn):
            raise ValueError("original failure")

    # MIG-1 + MIG-2 status missing file
    mig_table = f"migrations_{uuid.uuid4().hex[:8]}"
    data_table = f"mig_items_{uuid.uuid4().hex[:8]}"
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    mig_file = mig_dir / "2026_11_14_000001_create_items.py"
    mig_file.write_text(
        f"""from __future__ import annotations
from swiftpy.database.migrations.migration import Migration
from swiftpy.database.query_builder import Query
from swiftpy.database.transaction import get_active_connection, on_commit
from swiftpy.database.migrations.blueprint import Blueprint
from swiftpy.database.migrations.schema import Schema

COMMIT_EVENTS: list[bool] = []

class CreateItemsMigration(Migration):
    async def up(self) -> None:
        conn = get_active_connection()
        assert hasattr(conn, "on_commit")
        def _define(table: Blueprint) -> None:
            table.id()
            table.string("name")
        await Schema.create("{data_table}", _define)
        on_commit(lambda: COMMIT_EVENTS.append(get_active_connection() is None))

    async def down(self) -> None:
        await Schema.drop_if_exists("{data_table}")
""",
        encoding="utf-8",
    )

    migrator = Migrator(pool=ensure_db_pool, migrations_dir=mig_dir, table_name=mig_table)
    try:
        applied = await migrator.run()
        assert applied == ["2026_11_14_000001_create_items"]

        # Remove migration file from disk and ensure status still surfaces applied DB record
        mig_file.unlink()
        statuses = await migrator.status()
        assert len(statuses) == 1
        assert statuses[0]["migration"] == "2026_11_14_000001_create_items"
        assert statuses[0]["ran"] is True
        assert statuses[0]["missing_file"] is True
        assert statuses[0]["checksum_valid"] is False
    finally:
        raw_pool = ensure_db_pool.get_pool()
        async with raw_pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{data_table}";')
            await conn.execute(f'DROP TABLE IF EXISTS "{mig_table}";')


@pytest.mark.asyncio
async def test_mig3_seed_command_ast_filter_exact_match_and_prod_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MIG-3: db:seed skips unrelated files, requires exact class match, guards production, and checks pool init."""
    (tmp_path / "aaa_exploding_unrelated.py").write_text(
        'raise RuntimeError("Should NOT execute unrelated seeder file!")\n',
        encoding="utf-8",
    )
    (tmp_path / "target_seeder.py").write_text(
        """from __future__ import annotations
from swiftpy.database.seeder import Seeder

class TargetSeeder(Seeder):
    ran = False
    async def run(self) -> None:
        TargetSeeder.ran = True
""",
        encoding="utf-8",
    )

    cls = SeedCommand._find_seeder_class("TargetSeeder", tmp_path)
    assert cls.__name__ == "TargetSeeder"

    # Case-mismatched class name must be rejected cleanly
    with pytest.raises(SystemExit, match="could not be found"):
        SeedCommand._find_seeder_class("targetseeder", tmp_path)

    # Register CLI flag --force cleanly
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    SeedCommand().register(subparsers)
    parsed_default = parser.parse_args(["db:seed"])
    parsed_force = parser.parse_args(["db:seed", "--force"])
    assert parsed_default.force is False
    assert parsed_force.force is True

    # Production guard rejects missing --force before pool/seeder setup
    monkeypatch.setenv("APP_ENV", "production")
    prod_args = argparse.Namespace(
        app="main:app",
        class_name="TargetSeeder",
        no_transaction=False,
        path=str(tmp_path),
        force=False,
    )
    with patch("swiftpy.console.commands.seed.load_application") as mock_blocked_load:
        mock_blocked_app = MagicMock()
        mock_blocked_app.config.get.return_value = "testing"
        mock_blocked_pool = MagicMock()
        mock_blocked_pool.init = AsyncMock()
        mock_blocked_pool.close = AsyncMock()
        mock_blocked_app.container.resolve.return_value = mock_blocked_pool
        mock_blocked_load.return_value = mock_blocked_app
        with pytest.raises(SystemExit):
            await SeedCommand._execute_seed(prod_args)
        mock_blocked_pool.init.assert_not_awaited()
        mock_blocked_pool.close.assert_not_awaited()

    # pool.close() is not invoked if pool.init() fails
    monkeypatch.setenv("APP_ENV", "testing")
    args = argparse.Namespace(
        app="main:app",
        class_name="TargetSeeder",
        no_transaction=False,
        path=str(tmp_path),
        force=True,
    )
    with patch("swiftpy.console.commands.seed.load_application") as mock_load:
        mock_app = MagicMock()
        mock_pool = MagicMock()
        mock_pool.init = AsyncMock(side_effect=RuntimeError("db down"))
        mock_pool.close = AsyncMock()
        mock_app.container.resolve.return_value = mock_pool
        mock_load.return_value = mock_app

        with pytest.raises(RuntimeError, match="db down"):
            await SeedCommand._execute_seed(args)
        mock_pool.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_orm_1_2_3_4_5_end_to_end_postgres(
    ensure_db_pool: DatabasePool,
) -> None:
    """ORM-1..5: defaults, JSON vs vector _persistable, custom PK, updating hook dirty tracking, BelongsToMany."""
    suffix = uuid.uuid4().hex[:8]
    parent_tbl = f"tenants_{suffix}"
    child_tbl = f"tenant_users_{suffix}"
    profile_tbl = f"tenant_profiles_{suffix}"
    tag_tbl = f"tenant_tags_{suffix}"
    pivot_tbl = f"tenant_tag_links_{suffix}"
    default_only_tbl = f"default_only_{suffix}"

    class TenantTag(Model):
        __table__ = tag_tbl
        __primary_key__ = "tag_uuid"
        __guarded__: ClassVar[list[str]] = []
        tag_uuid: uuid.UUID
        label: str

    class TenantUser(Model):
        __table__ = child_tbl
        __primary_key__ = "user_uuid"
        __guarded__: ClassVar[list[str]] = []
        user_uuid: uuid.UUID
        tenant_uuid: uuid.UUID
        username: str
        tenant = BelongsTo("Tenant", foreign_key="tenant_uuid")

    class TenantProfile(Model):
        __table__ = profile_tbl
        __primary_key__ = "prof_uuid"
        __guarded__: ClassVar[list[str]] = []
        prof_uuid: uuid.UUID
        tenant_uuid: uuid.UUID
        bio: str | None = None

    class Tenant(Model):
        __table__ = parent_tbl
        __primary_key__ = "tenant_uuid"
        __guarded__: ClassVar[list[str]] = []
        tenant_uuid: uuid.UUID
        name: str
        status: str = "draft"
        scores: list[int] = Field(default_factory=lambda: [1, 2, 3])
        meta: dict[str, Any] = Field(default_factory=lambda: {"active": True})
        embedding: list[float] | None = Vector(3)
        users = HasMany(TenantUser, foreign_key="tenant_uuid")
        profile = HasOne(TenantProfile, foreign_key="tenant_uuid")
        tags = BelongsToMany(
            TenantTag,
            pivot_table=pivot_tbl,
            foreign_pivot_key="tenant_uuid",
            related_pivot_key="tag_uuid",
        )

    class DefaultOnly(Model):
        __table__ = default_only_tbl
        __guarded__: ClassVar[list[str]] = []
        id: int
        label: str | None = None

    raw_pool = ensure_db_pool.get_pool()
    async with raw_pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE "{parent_tbl}" (
                "tenant_uuid" UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                "name" TEXT NOT NULL,
                "status" TEXT NOT NULL,
                "scores" JSONB NOT NULL,
                "meta" JSONB NOT NULL,
                "embedding" TEXT
            );
            CREATE TABLE "{child_tbl}" (
                "user_uuid" UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                "tenant_uuid" UUID NOT NULL REFERENCES "{parent_tbl}"("tenant_uuid") ON DELETE CASCADE,
                "username" TEXT NOT NULL
            );
            CREATE TABLE "{profile_tbl}" (
                "prof_uuid" UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                "tenant_uuid" UUID NOT NULL REFERENCES "{parent_tbl}"("tenant_uuid") ON DELETE CASCADE,
                "bio" TEXT
            );
            CREATE TABLE "{tag_tbl}" (
                "tag_uuid" UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                "label" TEXT NOT NULL UNIQUE
            );
            CREATE TABLE "{pivot_tbl}" (
                "tenant_uuid" UUID NOT NULL REFERENCES "{parent_tbl}"("tenant_uuid") ON DELETE CASCADE,
                "tag_uuid" UUID NOT NULL REFERENCES "{tag_tbl}"("tag_uuid") ON DELETE CASCADE
            );
            CREATE TABLE "{default_only_tbl}" (
                "id" SERIAL PRIMARY KEY,
                "label" TEXT NOT NULL DEFAULT 'server-default'
            );
            """
        )

    disp = EventDispatcher()
    EventDispatcher.set_instance(disp)

    def _on_updating(model: Tenant) -> None:
        if model.name == "Acme Renamed":
            model.status = "published"

    disp.listen("tenant.updating", _on_updating)

    try:
        # ORM-1 + ORM-2: create persists defaults & list[int] as JSON, save() coerces returned JSON
        t = await Tenant.create(name="Acme")
        assert t.status == "draft"
        assert t.scores == [1, 2, 3]
        assert t.meta == {"active": True}

        fetched_raw = await Query.table(parent_tbl).where("tenant_uuid", t.tenant_uuid).first()
        assert fetched_raw is not None
        assert fetched_raw["status"] == "draft"

        t2 = Tenant(name="Beta", scores=[4, 5], meta={"ok": True}, embedding=[1.0, 2.0, 3.0])
        await t2.save()
        assert isinstance(t2.scores, list) and t2.scores == [4, 5]
        assert isinstance(t2.meta, dict) and t2.meta == {"ok": True}
        assert t2.embedding == [1.0, 2.0, 3.0]

        # ORM-1: all DB-generated defaults create/save with empty insert_data
        d1 = await DefaultOnly.create()
        assert d1.id is not None and d1.label == "server-default"
        d2 = DefaultOnly()
        await d2.save()
        assert d2.id is not None and d2.label == "server-default"

        # ORM-4: updating hook mutation persisted and not left dirty
        t.name = "Acme Renamed"
        await t.save()
        assert t.status == "published"
        assert t.is_dirty() is False
        reloaded = await Tenant.find(t.tenant_uuid)
        assert reloaded is not None and reloaded.status == "published"

        # ORM-3: custom __primary_key__ across relationships and chunk_by_id
        u1 = await TenantUser.create(tenant_uuid=t.tenant_uuid, username="alice")
        await TenantProfile.create(tenant_uuid=t.tenant_uuid, bio="Founder")
        tag1 = await TenantTag.create(label="Enterprise")
        tag2 = await TenantTag.create(label="Cloud")
        await t.tags.attach(tag1)

        loaded_tenant = await Tenant.with_("users", "profile", "tags").where(
            "tenant_uuid", t.tenant_uuid
        ).first()
        assert loaded_tenant is not None
        assert len(loaded_tenant.users) == 1
        assert loaded_tenant.users[0].username == "alice"
        assert loaded_tenant.profile is not None and loaded_tenant.profile.bio == "Founder"
        assert len(loaded_tenant.tags) == 1

        chunked_ids: list[uuid.UUID] = []

        def _collect_chunk(batch: list[Tenant]) -> None:
            chunked_ids.extend(item.tenant_uuid for item in batch)

        await Tenant.chunk_by_id(1, _collect_chunk)
        assert set(chunked_ids) == {t.tenant_uuid, t2.tenant_uuid}

        # ORM-5: RelationshipQueryBuilder chained where_not_null preserves HasOne single result
        prof_res = await t.profile.where_not_null("bio")
        assert isinstance(prof_res, TenantProfile)
        assert prof_res.bio == "Founder"

        # ORM-5: BelongsToMany attach/detach on unsaved model raises QueryError
        unsaved_t = Tenant(name="Unsaved")
        with pytest.raises(QueryError, match="primary key|unsaved"):
            await unsaved_t.tags.detach()
        with pytest.raises(QueryError, match="primary key|unsaved"):
            await unsaved_t.tags.attach(tag1)

        # ORM-5: sync runs inside transaction so failure rolls back prior detach
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await t.tags.sync([tag2, uuid.uuid4()])
        remaining_tags = await t.tags.get()
        assert [tg.label for tg in remaining_tags] == ["Enterprise"]

        # Verify BelongsTo uses owner custom PK too
        loaded_user = await TenantUser.with_("tenant").where("user_uuid", u1.user_uuid).first()
        assert loaded_user is not None and loaded_user.tenant is not None
        assert loaded_user.tenant.tenant_uuid == t.tenant_uuid
    finally:
        async with raw_pool.acquire() as conn:
            await conn.execute(
                f"""
                DROP TABLE IF EXISTS "{pivot_tbl}";
                DROP TABLE IF EXISTS "{tag_tbl}";
                DROP TABLE IF EXISTS "{profile_tbl}";
                DROP TABLE IF EXISTS "{child_tbl}";
                DROP TABLE IF EXISTS "{parent_tbl}";
                DROP TABLE IF EXISTS "{default_only_tbl}";
                """
            )
