from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path


def _pascal_case(snake_str: str) -> str:
    """Convert snake_case string to PascalCase class name."""
    components = re.split(r"[^a-zA-Z0-9]+", snake_str)
    return "".join(x.capitalize() for x in components if x)


_CREATE_PATTERNS = (
    re.compile(r"^create_(\w+?)_table$"),
    re.compile(r"^create_(\w+)$"),
)
_CHANGE_PATTERNS = (
    re.compile(r".+_(?:to|from|in)_(\w+?)_table$"),
    re.compile(r".+_(?:to|from|in)_(\w+)$"),
)


def _guess_table(name: str) -> tuple[str | None, bool]:
    """Infer table name and whether migration creates a table from its name."""
    for pattern in _CREATE_PATTERNS:
        if match := pattern.match(name):
            return match.group(1), True
    for pattern in _CHANGE_PATTERNS:
        if match := pattern.match(name):
            return match.group(1), False
    return None, False


_CREATE_STUB = """from __future__ import annotations

from swiftpy.database.migrations import Blueprint, Migration, Schema


class {class_name}(Migration):
    async def up(self) -> None:
        def define(table: Blueprint) -> None:
            table.id()
            table.timestamps()

        await Schema.create("{table_name}", define)

    async def down(self) -> None:
        await Schema.drop_if_exists("{table_name}")
"""

_ALTER_STUB = """from __future__ import annotations

from swiftpy.database.migrations import Blueprint, Migration, Schema


class {class_name}(Migration):
    async def up(self) -> None:
        def define(table: Blueprint) -> None:
            pass  # table.string("column_name").nullable()

        await Schema.table("{table_name}", define)

    async def down(self) -> None:
        def define(table: Blueprint) -> None:
            pass  # table.drop_column("column_name")

        await Schema.table("{table_name}", define)
"""

_BLANK_STUB = """from __future__ import annotations

from swiftpy.database.migrations import Migration


class {class_name}(Migration):
    async def up(self) -> None:
        pass

    async def down(self) -> None:
        pass
"""


class MakeCommand:
    """Command dispatcher for code generation scaffolding."""

    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        # Register `make:migration`
        parser = subparsers.add_parser(
            "make:migration",
            help="Create a new migration file",
        )
        parser.add_argument(
            "name",
            help="The name of the migration (e.g., create_users_table)",
        )
        parser.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the migration should be saved",
        )
        parser.add_argument(
            "--create",
            dest="create",
            default=None,
            help="The table to be created",
        )
        parser.add_argument(
            "--table",
            dest="table",
            default=None,
            help="The table to be altered",
        )
        parser.set_defaults(
            _handler=MakeCommand.handle_migration,
        )

    @staticmethod
    def handle_migration(args: argparse.Namespace) -> None:
        """Handle make:migration command execution."""
        raw_name: str = args.name.strip()
        snake_name = re.sub(r"[^a-zA-Z0-9_]+", "_", raw_name.lower()).strip("_")
        if not snake_name or snake_name[0].isdigit():
            raise SystemExit(f"Invalid migration name: {raw_name!r}")

        class_name = _pascal_case(snake_name)

        target_dir = (
            Path(args.path) if args.path else Path.cwd() / "database" / "migrations"
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(tz=UTC).strftime("%Y_%m_%d_%H%M%S")
        filename = f"{timestamp}_{snake_name}.py"
        filepath = target_dir / filename

        table_name = args.create or args.table
        is_create = bool(args.create)
        if not table_name:
            table_name, is_create = _guess_table(snake_name)

        if table_name is None:
            content = _BLANK_STUB.format(class_name=class_name)
        elif is_create:
            content = _CREATE_STUB.format(class_name=class_name, table_name=table_name)
        else:
            content = _ALTER_STUB.format(class_name=class_name, table_name=table_name)

        filepath.write_text(content, encoding="utf-8")
        if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
            print(f"\033[32mCreated Migration:\033[0m {filename}")
        else:
            print(f"Created Migration: {filename}")
