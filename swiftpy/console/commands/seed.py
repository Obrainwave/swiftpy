from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import importlib
import importlib.util
import inspect
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from swiftpy.console.loader import load_application
from swiftpy.database.pool import DatabasePool
from swiftpy.database.seeder import Seeder
from swiftpy.database.transaction import db
from swiftpy.support.strings import snake_case


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


class SeedCommand:
    """Console command for seeding database records."""

    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        parser = subparsers.add_parser(
            "db:seed",
            help="Seed the database with records",
        )
        parser.add_argument(
            "--class",
            dest="class_name",
            default="DatabaseSeeder",
            help="The class name of the root seeder (default: DatabaseSeeder)",
        )
        parser.add_argument(
            "--no-transaction",
            dest="no_transaction",
            action="store_true",
            default=False,
            help="Run seeders without wrapping in a transaction",
        )
        parser.add_argument(
            "--app",
            default="main:app",
            help="Application target (module:attribute)",
        )
        parser.add_argument(
            "--path",
            default=None,
            help="Path to seeders directory (defaults to database/seeders)",
        )
        parser.set_defaults(
            _handler=SeedCommand.handle,
        )

    @classmethod
    def _find_seeder_class(
        cls,
        class_name: str,
        seeders_dir: Path,
    ) -> type[Seeder]:
        """Dynamically load and resolve the Seeder class."""
        if "." in class_name:
            try:
                mod_path, cls_name = class_name.rsplit(".", 1)
                mod = importlib.import_module(mod_path)
                target = getattr(mod, cls_name, None)
                if isinstance(target, type) and issubclass(target, Seeder) and target is not Seeder:
                    if inspect.isabstract(target):
                        raise SystemExit(f"Cannot run abstract seeder class '{target.__name__}'.")
                    return target
            except (ImportError, AttributeError):
                pass

        snake_file = seeders_dir / f"{snake_case(class_name)}.py"
        direct_file = seeders_dir / f"{class_name}.py"
        candidate_files: list[Path] = []
        if snake_file.exists():
            candidate_files.append(snake_file)
        if direct_file.exists() and direct_file not in candidate_files:
            candidate_files.append(direct_file)
        if seeders_dir.is_dir():
            for f in sorted(seeders_dir.glob("*.py")):
                if f not in candidate_files and not f.name.startswith("_"):
                    candidate_files.append(f)

        for target_file in candidate_files:
            mod_name = f"_seeders.{target_file.stem}"
            loaded_mod: ModuleType | None = None
            if mod_name in sys.modules:
                loaded_mod = sys.modules[mod_name]
            else:
                spec = importlib.util.spec_from_file_location(mod_name, target_file)
                if spec and spec.loader:
                    loaded_mod = importlib.util.module_from_spec(spec)
                    sys.modules[mod_name] = loaded_mod
                    spec.loader.exec_module(loaded_mod)

            if loaded_mod is not None:
                candidate = getattr(loaded_mod, class_name, None)
                if isinstance(candidate, type) and issubclass(candidate, Seeder) and candidate is not Seeder:
                    if inspect.isabstract(candidate):
                        raise SystemExit(f"Cannot run abstract seeder class '{candidate.__name__}'.")
                    return candidate
                for attr_name in dir(loaded_mod):
                    if attr_name.lower() == class_name.lower():
                        attr = getattr(loaded_mod, attr_name)
                        if (
                            isinstance(attr, type)
                            and issubclass(attr, Seeder)
                            and attr is not Seeder
                        ):
                            if inspect.isabstract(attr):
                                raise SystemExit(f"Cannot run abstract seeder class '{attr.__name__}'.")
                            return attr

        try:
            fallback_mod = importlib.import_module(f"database.seeders.{snake_case(class_name)}")
            candidate = getattr(fallback_mod, class_name, None)
            if isinstance(candidate, type) and issubclass(candidate, Seeder) and candidate is not Seeder:
                if inspect.isabstract(candidate):
                    raise SystemExit(f"Cannot run abstract seeder class '{candidate.__name__}'.")
                return candidate
        except (ImportError, AttributeError):
            pass

        raise SystemExit(
            f"Seeder class '{class_name}' could not be found in {seeders_dir} or python path."
        )

    @classmethod
    async def _execute_seed(cls, args: argparse.Namespace) -> None:
        app = load_application(args.app)
        pool = app.container.resolve(DatabasePool)
        await pool.init()

        try:
            seeders_dir = (
                Path(args.path) if args.path else Path.cwd() / "database" / "seeders"
            )
            seeder_cls = cls._find_seeder_class(args.class_name, seeders_dir)
            if inspect.isabstract(seeder_cls):
                raise SystemExit(
                    f"Cannot run abstract seeder class '{seeder_cls.__name__}'."
                )
            seeder = seeder_cls()

            start_time = time.perf_counter()
            if getattr(args, "no_transaction", False):
                await seeder.run()
            else:
                async with db.transaction():
                    await seeder.run()
            elapsed = time.perf_counter() - start_time

            print(f"{_paint('Seeded:', '32')} {seeder_cls.__name__} ({elapsed:.2f}s)")
        finally:
            await pool.close()

    @classmethod
    def handle(cls, args: argparse.Namespace) -> None:
        """Handle db:seed command dispatch."""
        _run_coroutine(cls._execute_seed(args))
