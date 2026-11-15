from __future__ import annotations

import hashlib
import importlib.util
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.identifiers import validate_identifier
from swiftpy.database.migrations.lock import MigrationLock
from swiftpy.database.migrations.migration import Migration, MigrationProtocol
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import TransactionConnection, _active_connection


@dataclass(frozen=True)
class MigrationFile:
    """Represents a discovered migration file on the filesystem."""

    name: str
    path: Path


@dataclass(frozen=True)
class MigrationRecord:
    """Represents an applied migration record from the database migrations table."""

    id: int
    migration: str
    batch: int
    applied_at: datetime
    checksum: str | None = None


class _FunctionMigrationAdapter:
    """Adapts module-level up/down async functions into MigrationProtocol."""

    def __init__(self, up_fn: Any, down_fn: Any) -> None:
        self._up_fn = up_fn
        self._down_fn = down_fn

    async def up(self) -> None:
        if callable(self._up_fn):
            res = self._up_fn()
            if inspect.isawaitable(res):
                await res

    async def down(self) -> None:
        if callable(self._down_fn):
            res = self._down_fn()
            if inspect.isawaitable(res):
                await res


class Migrator:
    """
    Migration Engine for SwiftPY.

    Discovers timestamped migration files, tracks applied migrations across batches,
    and runs forward and backward migrations within transactional advisory locks.
    """

    def __init__(
        self,
        pool: DatabasePool | None = None,
        migrations_dir: Path | str | None = None,
        table_name: str = "migrations",
        lock_timeout: float = 300.0,
    ) -> None:
        self.pool: DatabasePool = pool or get_container().resolve(DatabasePool)
        if migrations_dir is None:
            self.migrations_dir: Path = Path.cwd() / "database" / "migrations"
        else:
            self.migrations_dir = Path(migrations_dir)
        self.table_name: str = validate_identifier(table_name)
        self.lock_timeout: float = lock_timeout

    @staticmethod
    def compute_checksum(file: MigrationFile) -> str:
        """Compute the SHA-256 hex digest of a migration file's contents."""
        return hashlib.sha256(file.path.read_bytes()).hexdigest()

    def get_migration_files(self) -> list[MigrationFile]:
        """Discover all migration Python files in the migrations directory, sorted by timestamp prefix."""
        if not self.migrations_dir.exists():
            return []

        files: list[MigrationFile] = []
        for p in self.migrations_dir.glob("*.py"):
            if p.name.startswith(("_", ".")) or p.name == "__init__.py":
                continue
            files.append(MigrationFile(name=p.stem, path=p))

        # Natural sort ensures strict timestamp-ordered execution (YYYY_MM_DD_HHMMSS_...)
        files.sort(key=lambda f: f.name)
        return files

    async def ensure_repository(self, conn: asyncpg.Connection) -> None:
        """Create the migrations tracking table if it does not already exist."""
        sql = f"""
            CREATE TABLE IF NOT EXISTS "{self.table_name}" (
                "id" SERIAL PRIMARY KEY,
                "migration" VARCHAR(255) NOT NULL UNIQUE,
                "batch" INTEGER NOT NULL,
                "checksum" VARCHAR(64),
                "applied_at" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """
        await conn.execute(sql)
        await conn.execute(
            f'ALTER TABLE "{self.table_name}" ADD COLUMN IF NOT EXISTS "checksum" VARCHAR(64);'
        )

    async def get_ran_migrations(self, conn: asyncpg.Connection) -> list[str]:
        """Fetch all migration names that have already been executed."""
        rows = await conn.fetch(
            f'SELECT "migration" FROM "{self.table_name}" ORDER BY "batch" ASC, "id" ASC;'
        )
        return [str(r["migration"]) for r in rows]

    async def _get_ran_checksums(
        self, conn: asyncpg.Connection
    ) -> dict[str, str | None]:
        """Fetch applied migration names mapped to their recorded SHA-256 checksums."""
        rows = await conn.fetch(
            f'SELECT "migration", "checksum" FROM "{self.table_name}" ORDER BY "batch" ASC, "id" ASC;'
        )
        return {
            str(r["migration"]): (
                str(r["checksum"]) if r["checksum"] is not None else None
            )
            for r in rows
        }

    async def get_last_batch(self, conn: asyncpg.Connection) -> int:
        """Fetch the highest batch number recorded."""
        val = await conn.fetchval(
            f'SELECT COALESCE(MAX("batch"), 0) FROM "{self.table_name}";'
        )
        return int(val or 0)

    def resolve_migration_instance(self, file: MigrationFile) -> MigrationProtocol:
        """Dynamically load and instantiate the migration class from a file."""
        module_name = f"swiftpy_migration_{file.name}"
        spec = importlib.util.spec_from_file_location(module_name, file.path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load migration file: {file.path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # 1. Look for a class that inherits from Migration or provides up/down
        migration_cls = self._find_migration_class(module)
        if migration_cls is not None:
            inst = migration_cls()
            if isinstance(inst, MigrationProtocol):
                return inst

        # 2. Look for module-level up/down async functions
        up_fn = getattr(module, "up", None)
        down_fn = getattr(module, "down", None)
        if callable(up_fn) and callable(down_fn):
            return _FunctionMigrationAdapter(up_fn, down_fn)

        raise RuntimeError(
            f"Migration file {file.path.name} must define a class with up() and down() methods "
            f"or module-level up() and down() functions."
        )

    @staticmethod
    def _find_migration_class(module: ModuleType) -> type[Any] | None:
        """Find a migration class defined in the loaded module."""
        candidates: list[type[Any]] = []

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue

            if issubclass(obj, Migration) and obj is not Migration:
                return obj

            if hasattr(obj, "up") and hasattr(obj, "down"):
                candidates.append(obj)

        if candidates:
            return candidates[0]
        return None

    async def run(
        self,
        step: int | None = None,
        on_applied: Callable[[str, float], None] | None = None,
    ) -> list[str]:
        """
        Run all pending migrations within a transactional advisory lock.
        Returns the list of migration names applied in this batch.
        """
        if step is not None and (
            isinstance(step, bool) or not isinstance(step, int) or step < 1
        ):
            raise ValueError(f"step must be an integer >= 1, got {step!r}")

        raw_pool = self.pool.get_pool()

        async with (
            raw_pool.acquire() as conn,
            MigrationLock(conn, timeout=self.lock_timeout),
        ):
            await self.ensure_repository(conn)

            files = self.get_migration_files()
            ran_checksums = await self._get_ran_checksums(conn)
            ran = list(ran_checksums.keys())

            # Verify no checksum drift on already-applied migrations
            for file in files:
                if file.name in ran_checksums:
                    recorded = ran_checksums[file.name]
                    current = self.compute_checksum(file)
                    if recorded is not None and recorded != current:
                        raise RuntimeError(
                            f"Migration checksum drift detected for '{file.name}': "
                            f"recorded {recorded}, current file {current}"
                        )

            pending = [f for f in files if f.name not in ran_checksums]
            if not pending:
                return []

            # Detect out-of-order unapplied migrations that sort before an already-applied migration
            if ran:
                max_applied = max(ran)
                for file in pending:
                    if file.name < max_applied:
                        raise RuntimeError(
                            f"Out-of-order unapplied migration '{file.name}' precedes "
                            f"already-applied migration '{max_applied}'."
                        )

            if step is not None:
                pending = pending[:step]

            next_batch = (await self.get_last_batch(conn)) + 1
            applied: list[str] = []

            for file in pending:
                started = time.perf_counter()
                checksum = self.compute_checksum(file)
                instance = self.resolve_migration_instance(file)

                await self._execute_in_transaction(
                    conn,
                    partial(
                        self._apply_migration_tx,
                        instance=instance,
                        migration_name=file.name,
                        batch=next_batch,
                        checksum=checksum,
                    ),
                )
                duration_ms = (time.perf_counter() - started) * 1000
                applied.append(file.name)
                if on_applied is not None:
                    on_applied(file.name, duration_ms)

            return applied

    async def rollback(
        self,
        steps: int = 1,
        on_rolled_back: Callable[[str, float], None] | None = None,
    ) -> list[str]:
        """
        Roll back the last batch of migrations (or the last N batches if steps > 1).
        Returns the list of migration names rolled back.
        """
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            raise ValueError(f"steps must be an integer >= 1, got {steps!r}")

        raw_pool = self.pool.get_pool()

        async with (
            raw_pool.acquire() as conn,
            MigrationLock(conn, timeout=self.lock_timeout),
        ):
            await self.ensure_repository(conn)

            last_batch = await self.get_last_batch(conn)
            if last_batch <= 0:
                return []

            min_batch = max(1, last_batch - steps + 1)
            rows = await conn.fetch(
                f'SELECT "migration" FROM "{self.table_name}" '
                f'WHERE "batch" >= $1 ORDER BY "id" DESC;',
                min_batch,
            )

            if not rows:
                return []

            files_by_name = {f.name: f for f in self.get_migration_files()}

            # Up-front validation: verify all migration files exist before executing rollbacks
            missing = [
                str(r["migration"])
                for r in rows
                if str(r["migration"]) not in files_by_name
            ]
            if missing:
                raise FileNotFoundError(
                    f"Cannot roll back, migration files missing on disk: {', '.join(missing)}"
                )

            rolled_back: list[str] = []

            for row in rows:
                name = str(row["migration"])
                file = files_by_name[name]
                started = time.perf_counter()
                instance = self.resolve_migration_instance(file)

                await self._execute_in_transaction(
                    conn,
                    partial(
                        self._rollback_migration_tx,
                        instance=instance,
                        migration_name=name,
                    ),
                )
                duration_ms = (time.perf_counter() - started) * 1000
                rolled_back.append(name)
                if on_rolled_back is not None:
                    on_rolled_back(name, duration_ms)

            return rolled_back

    async def _apply_migration_tx(
        self,
        tx_handle: TransactionConnection,
        *,
        instance: MigrationProtocol,
        migration_name: str,
        batch: int,
        checksum: str,
    ) -> None:
        await instance.up()
        await tx_handle.execute(
            f'INSERT INTO "{self.table_name}" ("migration", "batch", "checksum") '
            f"VALUES ($1, $2, $3);",
            migration_name,
            batch,
            checksum,
        )

    async def _rollback_migration_tx(
        self,
        tx_handle: TransactionConnection,
        *,
        instance: MigrationProtocol,
        migration_name: str,
    ) -> None:
        await instance.down()
        await tx_handle.execute(
            f'DELETE FROM "{self.table_name}" WHERE "migration" = $1;',
            migration_name,
        )

    @staticmethod
    async def _execute_in_transaction(
        conn: asyncpg.Connection,
        operation: Callable[[TransactionConnection], Awaitable[None]],
    ) -> None:
        """Bind a TransactionConnection handle per migration step and flush on_commit after closing."""
        tx_handle = TransactionConnection(conn)
        token = _active_connection.set(tx_handle)
        committed_callbacks: list[Callable[[], Any]] = []
        try:
            async with tx_handle.transaction(defer_callbacks=True):
                await operation(tx_handle)
            committed_callbacks = tx_handle.drain_commit_callbacks()
        finally:
            tx_handle._close()
            _active_connection.reset(token)

        if committed_callbacks:
            await TransactionConnection.run_commit_callbacks(committed_callbacks)

    async def reset(self) -> list[str]:
        """Roll back all migrations in reverse chronological order."""
        raw_pool = self.pool.get_pool()

        async with (
            raw_pool.acquire() as conn,
            MigrationLock(conn, timeout=self.lock_timeout),
        ):
            await self.ensure_repository(conn)

            rows = await conn.fetch(
                f'SELECT "migration" FROM "{self.table_name}" ORDER BY "id" DESC;'
            )
            if not rows:
                return []

            files_by_name = {f.name: f for f in self.get_migration_files()}

            # Up-front validation
            missing = [
                str(r["migration"])
                for r in rows
                if str(r["migration"]) not in files_by_name
            ]
            if missing:
                raise FileNotFoundError(
                    f"Cannot roll back, migration files missing on disk: {', '.join(missing)}"
                )

            rolled_back: list[str] = []

            for row in rows:
                name = str(row["migration"])
                file = files_by_name[name]
                instance = self.resolve_migration_instance(file)

                await self._execute_in_transaction(
                    conn,
                    partial(
                        self._rollback_migration_tx,
                        instance=instance,
                        migration_name=name,
                    ),
                )
                rolled_back.append(name)

            return rolled_back

    async def status(self) -> list[dict[str, Any]]:
        """Return the execution status of all discovered migration files and applied records."""
        raw_pool = self.pool.get_pool()

        async with raw_pool.acquire() as conn:
            await self.ensure_repository(conn)

            rows = await conn.fetch(
                f'SELECT "migration", "batch", "checksum", "applied_at" '
                f'FROM "{self.table_name}" ORDER BY "id" ASC;'
            )
            records = {str(r["migration"]): r for r in rows}

            files = self.get_migration_files()
            seen_files: set[str] = set()
            statuses: list[dict[str, Any]] = []

            for f in files:
                seen_files.add(f.name)
                rec = records.get(f.name)
                current_checksum = self.compute_checksum(f)
                recorded_checksum = (
                    str(rec["checksum"])
                    if rec is not None and rec["checksum"] is not None
                    else None
                )
                statuses.append(
                    {
                        "migration": f.name,
                        "ran": rec is not None,
                        "batch": rec["batch"] if rec else None,
                        "applied_at": rec["applied_at"] if rec else None,
                        "checksum": recorded_checksum or current_checksum,
                        "checksum_valid": (
                            True
                            if recorded_checksum is None
                            else recorded_checksum == current_checksum
                        ),
                        "missing_file": False,
                    }
                )

            for name, rec in records.items():
                if name in seen_files:
                    continue
                statuses.append(
                    {
                        "migration": name,
                        "ran": True,
                        "batch": rec["batch"],
                        "applied_at": rec["applied_at"],
                        "checksum": (
                            str(rec["checksum"])
                            if rec["checksum"] is not None
                            else None
                        ),
                        "checksum_valid": False,
                        "missing_file": True,
                    }
                )

            return statuses
