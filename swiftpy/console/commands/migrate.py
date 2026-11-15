from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import os
import sys
from pathlib import Path
from typing import Any

from swiftpy.console.loader import load_application
from swiftpy.core.config import env
from swiftpy.database.migrations.migrator import Migrator
from swiftpy.database.pool import DatabasePool


def _paint(text: str, code: str) -> str:
    """Format text with ANSI escape codes only if stdout is a terminal and NO_COLOR is unset."""
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        return f"\033[{code}m{text}\033[0m"
    return text


def _run_coroutine(coro: Any) -> Any:
    """Run an async coroutine synchronously, safely handling existing active event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


class MigrateCommand:
    """Console commands for running and inspecting database migrations."""

    @staticmethod
    def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--app",
            default="main:app",
            help="Application target (module:attribute)",
        )
        parser.add_argument(
            "--path",
            default=None,
            help="Path to the migrations directory",
        )

    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        # 1. `swiftpy migrate`
        migrate_parser = subparsers.add_parser(
            "migrate",
            help="Run outstanding database migrations",
        )
        MigrateCommand._add_common_arguments(migrate_parser)
        migrate_parser.add_argument(
            "--step",
            dest="step",
            type=int,
            default=None,
            help="The number of migrations to execute",
        )
        migrate_parser.add_argument(
            "--force",
            dest="force",
            action="store_true",
            default=False,
            help="Force the operation to run when in production",
        )
        migrate_parser.set_defaults(
            _handler=MigrateCommand.handle_migrate,
        )

        # 2. `swiftpy migrate:rollback`
        rollback_parser = subparsers.add_parser(
            "migrate:rollback",
            help="Rollback the last database migration batch",
        )
        MigrateCommand._add_common_arguments(rollback_parser)
        rollback_parser.add_argument(
            "--step",
            dest="step",
            type=int,
            default=1,
            help="The number of migration batches to rollback",
        )
        rollback_parser.add_argument(
            "--force",
            dest="force",
            action="store_true",
            default=False,
            help="Force the operation to run when in production",
        )
        rollback_parser.set_defaults(
            _handler=MigrateCommand.handle_rollback,
        )

        # 3. `swiftpy migrate:reset`
        reset_parser = subparsers.add_parser(
            "migrate:reset",
            help="Rollback all database migrations",
        )
        MigrateCommand._add_common_arguments(reset_parser)
        reset_parser.add_argument(
            "--force",
            dest="force",
            action="store_true",
            default=False,
            help="Force the operation to run when in production",
        )
        reset_parser.set_defaults(
            _handler=MigrateCommand.handle_reset,
        )

        # 4. `swiftpy migrate:status`
        status_parser = subparsers.add_parser(
            "migrate:status",
            help="Show the status of each migration",
        )
        MigrateCommand._add_common_arguments(status_parser)
        status_parser.set_defaults(
            _handler=MigrateCommand.handle_status,
        )

    @staticmethod
    def _validate_step(step: int | None) -> None:
        if step is not None and step < 1:
            print("Error: --step must be an integer >= 1.", file=sys.stderr)
            raise SystemExit(1)

    @staticmethod
    def _enforce_production_guard(
        args: argparse.Namespace, app: Any | None = None
    ) -> None:
        if getattr(args, "force", False):
            return
        env_val = (
            env("SWIFTPY_APP__ENV")
            or env("APP_ENV")
            or (
                str(app.config.get("app.env") or app.config.get("env") or "")
                if app is not None and hasattr(app, "config")
                else ""
            )
        )
        if str(env_val).strip().lower() in ("production", "prod"):
            print(
                "Error: Application is in production. Pass --force to run migrations in production.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    @classmethod
    async def _init_migrator(
        cls,
        args: argparse.Namespace,
        *,
        require_guard: bool = False,
    ) -> tuple[DatabasePool, Migrator]:
        """Load application target, resolve DatabasePool, and return initialized Migrator."""
        if require_guard:
            cls._enforce_production_guard(args)
        app = load_application(args.app)
        if require_guard:
            cls._enforce_production_guard(args, app=app)
        pool = app.container.resolve(DatabasePool)
        await pool.init()

        mig_dir = (
            Path(args.path) if args.path else Path.cwd() / "database" / "migrations"
        )
        return pool, Migrator(pool=pool, migrations_dir=mig_dir)

    @staticmethod
    def handle_migrate(args: argparse.Namespace) -> None:
        MigrateCommand._validate_step(getattr(args, "step", None))
        MigrateCommand._enforce_production_guard(args)
        _run_coroutine(MigrateCommand._execute_migrate(args))

    @classmethod
    async def _execute_migrate(cls, args: argparse.Namespace) -> None:
        pool, migrator = await cls._init_migrator(args, require_guard=True)
        try:
            print(_paint("Running migrations...", "1"))
            applied = await migrator.run(
                step=args.step,
                on_applied=lambda name, ms: print(
                    f"  {_paint('Migrated:', '32')} {name} ({ms:.1f}ms)"
                ),
            )

            if not applied:
                print(_paint("Nothing to migrate.", "33"))
        finally:
            await pool.close()

    @staticmethod
    def handle_rollback(args: argparse.Namespace) -> None:
        MigrateCommand._validate_step(getattr(args, "step", 1))
        MigrateCommand._enforce_production_guard(args)
        _run_coroutine(MigrateCommand._execute_rollback(args))

    @classmethod
    async def _execute_rollback(cls, args: argparse.Namespace) -> None:
        pool, migrator = await cls._init_migrator(args, require_guard=True)
        try:
            print(_paint("Rolling back migrations...", "1"))
            rolled_back = await migrator.rollback(
                steps=args.step,
                on_rolled_back=lambda name, ms: print(
                    f"  {_paint('Rolled back:', '33')} {name} ({ms:.1f}ms)"
                ),
            )

            if not rolled_back:
                print(_paint("Nothing to rollback.", "33"))
        finally:
            await pool.close()

    @staticmethod
    def handle_reset(args: argparse.Namespace) -> None:
        MigrateCommand._enforce_production_guard(args)
        _run_coroutine(MigrateCommand._execute_reset(args))

    @classmethod
    async def _execute_reset(cls, args: argparse.Namespace) -> None:
        pool, migrator = await cls._init_migrator(args, require_guard=True)
        try:
            print(_paint("Resetting all migrations...", "1"))
            rolled_back = await migrator.reset()
            for name in rolled_back:
                print(f"  {_paint('Rolled back:', '33')} {name}")

            if not rolled_back:
                print(_paint("Nothing to reset.", "33"))
        finally:
            await pool.close()

    @staticmethod
    def handle_status(args: argparse.Namespace) -> None:
        _run_coroutine(MigrateCommand._execute_status(args))

    @classmethod
    async def _execute_status(cls, args: argparse.Namespace) -> None:
        pool, migrator = await cls._init_migrator(args)
        try:
            statuses: list[dict[str, Any]] = await migrator.status()

            if not statuses:
                print(_paint("No migrations found.", "33"))
                return

            print(
                "\n+------+----------------------------------------------------------+-------+"
            )
            print(
                "| Ran? | Migration                                                | Batch |"
            )
            print(
                "+------+----------------------------------------------------------+-------+"
            )

            for s in statuses:
                ran_str = (
                    f"{_paint('Yes', '32')} " if s["ran"] else f"{_paint('No', '31')}  "
                )
                name = str(s["migration"])[:56].ljust(56)
                batch_str = str(s["batch"] if s["batch"] is not None else "").center(5)
                print(f"| {ran_str} | {name} | {batch_str} |")

            print(
                "+------+----------------------------------------------------------+-------+\n"
            )
        finally:
            await pool.close()
