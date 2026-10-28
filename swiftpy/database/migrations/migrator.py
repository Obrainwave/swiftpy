from __future__ import annotations

import importlib.util
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import asyncpg

from swiftpy.core.bootstrap import get_container
from swiftpy.database.identifiers import validate_identifier
from swiftpy.database.migrations.lock import MigrationLock
from swiftpy.database.migrations.migration import Migration, MigrationProtocol
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import _active_connection


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
                "applied_at" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """
        await conn.execute(sql)

    async def get_ran_migrations(self, conn: asyncpg.Connection) -> list[str]:
        """Fetch all migration names that have already been executed."""
        rows = await conn.fetch(
            f'SELECT "migration" FROM "{self.table_name}" ORDER BY "batch" ASC, "id" ASC;'
        )
        return [str(r["migration"]) for r in rows]

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
        raw_pool = self.pool.get_pool()

        async with (
            raw_pool.acquire() as conn,
            MigrationLock(conn, timeout=self.lock_timeout),
        ):
            await self.ensure_repository(conn)

            files = self.get_migration_files()
            ran = await self.get_ran_migrations(conn)

            pending = [f for f in files if f.name not in ran]
            if not pending:
                return []

            if step is not None and step > 0:
                pending = pending[:step]

            next_batch = (await self.get_last_batch(conn)) + 1
            applied: list[str] = []

            # Bind connection to context so Schema operations reuse this exact transaction
            token = _active_connection.set(conn)
            try:
                for file in pending:
                    started = time.perf_counter()
                    instance = self.resolve_migration_instance(file)

                    async with conn.transaction():
                        await instance.up()
                        await conn.execute(
                            f'INSERT INTO "{self.table_name}" ("migration", "batch") VALUES ($1, $2);',
                            file.name,
                            next_batch,
                        )
                    duration_ms = (time.perf_counter() - started) * 1000
                    applied.append(file.name)
                    if on_applied is not None:
                        on_applied(file.name, duration_ms)
            finally:
                _active_connection.reset(token)

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
        if steps <= 0:
            return []

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

            token = _active_connection.set(conn)
            try:
                for row in rows:
                    name = str(row["migration"])
                    file = files_by_name[name]
                    started = time.perf_counter()
                    instance = self.resolve_migration_instance(file)

                    async with conn.transaction():
                        await instance.down()
                        await conn.execute(
                            f'DELETE FROM "{self.table_name}" WHERE "migration" = $1;',
                            name,
                        )
                    duration_ms = (time.perf_counter() - started) * 1000
                    rolled_back.append(name)
                    if on_rolled_back is not None:
                        on_rolled_back(name, duration_ms)
            finally:
                _active_connection.reset(token)

            return rolled_back

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

            token = _active_connection.set(conn)
            try:
                for row in rows:
                    name = str(row["migration"])
                    file = files_by_name[name]
                    instance = self.resolve_migration_instance(file)

                    async with conn.transaction():
                        await instance.down()
                        await conn.execute(
                            f'DELETE FROM "{self.table_name}" WHERE "migration" = $1;',
                            name,
                        )
                    rolled_back.append(name)
            finally:
                _active_connection.reset(token)

            return rolled_back

    async def status(self) -> list[dict[str, Any]]:
        """Return the execution status of all discovered migration files."""
        raw_pool = self.pool.get_pool()

        async with raw_pool.acquire() as conn:
            await self.ensure_repository(conn)

            rows = await conn.fetch(
                f'SELECT "migration", "batch", "applied_at" FROM "{self.table_name}";'
            )
            records = {str(r["migration"]): r for r in rows}

            files = self.get_migration_files()
            statuses: list[dict[str, Any]] = []

            for f in files:
                rec = records.get(f.name)
                statuses.append(
                    {
                        "migration": f.name,
                        "ran": rec is not None,
                        "batch": rec["batch"] if rec else None,
                        "applied_at": rec["applied_at"] if rec else None,
                    }
                )

            return statuses
