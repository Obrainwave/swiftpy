from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from swiftpy.console.stubs import get_stub
from swiftpy.support.strings import pluralize, snake_case


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


_CREATE_STUB = get_stub("migration.create")
_ALTER_STUB = get_stub("migration.alter")
_BLANK_STUB = get_stub("migration.blank")


def _scaffold_file(
    raw_name: str,
    default_dir_rel: str,
    stub_name: str,
    override_path: str | None = None,
    force: bool = False,
    extra_replacements: dict[str, str] | None = None,
    item_type: str = "File",
) -> Path:
    """
    Generic scaffolding engine for SwiftPY generators.
    Resolves class names, filenames, creates target directories, and writes stub templates.
    """
    trimmed = raw_name.strip()
    if not trimmed:
        raise SystemExit(f"Invalid {item_type.lower()} name: {raw_name!r}")

    raw_path = Path(trimmed)
    subpath = raw_path.parent if str(raw_path.parent) != "." else None
    base_name = raw_path.name

    snake_name = snake_case(base_name)
    class_name = _pascal_case(snake_name)

    base_dir = Path(override_path) if override_path else Path.cwd() / default_dir_rel
    target_dir = base_dir / subpath if subpath else base_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{snake_name}.py"
    filepath = target_dir / filename

    if filepath.exists() and not force:
        raise SystemExit(f"{item_type} already exists: {filepath}. Use --force to overwrite.")

    stub = get_stub(stub_name)
    content = stub.replace("{class_name}", class_name)
    if extra_replacements:
        for k, v in extra_replacements.items():
            content = content.replace(f"{{{k}}}", v)

    filepath.write_text(content, encoding="utf-8")

    display_name = str(filepath.relative_to(Path.cwd())) if filepath.is_relative_to(Path.cwd()) else str(filepath)
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        print(f"\033[32mCreated {item_type}:\033[0m {display_name}")
    else:
        print(f"Created {item_type}: {display_name}")

    return filepath


class MakeCommand:
    """Command dispatcher for code generation scaffolding."""

    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        # 1. make:migration
        parser_mig = subparsers.add_parser(
            "make:migration",
            help="Create a new migration file",
        )
        parser_mig.add_argument(
            "name",
            help="The name of the migration (e.g., create_users_table)",
        )
        parser_mig.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the migration should be saved",
        )
        parser_mig.add_argument(
            "--create",
            dest="create",
            default=None,
            help="The table to be created",
        )
        parser_mig.add_argument(
            "--table",
            dest="table",
            default=None,
            help="The table to be altered",
        )
        parser_mig.set_defaults(
            _handler=MakeCommand.handle_migration,
        )

        # 2. make:model
        parser_model = subparsers.add_parser(
            "make:model",
            help="Create a new Model class",
        )
        parser_model.add_argument(
            "name",
            help="The name of the model (e.g., User)",
        )
        parser_model.add_argument(
            "-m",
            "--migration",
            action="store_true",
            help="Create a new migration file for the model",
        )
        parser_model.add_argument(
            "--table",
            dest="table",
            default=None,
            help="The table associated with the model",
        )
        parser_model.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the model should be saved (defaults to app/models)",
        )
        parser_model.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the model if it already exists",
        )
        parser_model.set_defaults(
            _handler=MakeCommand.handle_model,
        )

        # 3. make:service
        parser_service = subparsers.add_parser(
            "make:service",
            help="Create a new Service class",
        )
        parser_service.add_argument(
            "name",
            help="The name of the service (e.g., PaymentService)",
        )
        parser_service.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the service should be saved (defaults to app/services)",
        )
        parser_service.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the service if it already exists",
        )
        parser_service.set_defaults(
            _handler=MakeCommand.handle_service,
        )

        # 4. make:request
        parser_request = subparsers.add_parser(
            "make:request",
            help="Create a new FormRequest validation class",
        )
        parser_request.add_argument(
            "name",
            help="The name of the request (e.g., CreateUserRequest)",
        )
        parser_request.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the request should be saved (defaults to app/http/requests)",
        )
        parser_request.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the request if it already exists",
        )
        parser_request.set_defaults(
            _handler=MakeCommand.handle_request,
        )

        # 5. make:resource
        parser_resource = subparsers.add_parser(
            "make:resource",
            help="Create a new Resource transformer class",
        )
        parser_resource.add_argument(
            "name",
            help="The name of the resource (e.g., UserResource)",
        )
        parser_resource.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the resource should be saved (defaults to app/http/resources)",
        )
        parser_resource.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the resource if it already exists",
        )
        parser_resource.set_defaults(
            _handler=MakeCommand.handle_resource,
        )

        # 6. make:repository
        parser_repo = subparsers.add_parser(
            "make:repository",
            help="Create a new Repository class",
        )
        parser_repo.add_argument(
            "name",
            help="The name of the repository (e.g., UserRepository)",
        )
        parser_repo.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the repository should be saved (defaults to app/repositories)",
        )
        parser_repo.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the repository if it already exists",
        )
        parser_repo.set_defaults(
            _handler=MakeCommand.handle_repository,
        )

        # 7. make:schema
        parser_schema = subparsers.add_parser(
            "make:schema",
            help="Create a new Schema class",
        )
        parser_schema.add_argument(
            "name",
            help="The name of the schema (e.g., OrderSchema)",
        )
        parser_schema.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the schema should be saved (defaults to app/schemas)",
        )
        parser_schema.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the schema if it already exists",
        )
        parser_schema.set_defaults(
            _handler=MakeCommand.handle_schema,
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
            content = get_stub("migration.blank").replace("{class_name}", class_name)
        elif is_create:
            content = (
                get_stub("migration.create")
                .replace("{class_name}", class_name)
                .replace("{table_name}", table_name)
            )
        else:
            content = (
                get_stub("migration.alter")
                .replace("{class_name}", class_name)
                .replace("{table_name}", table_name)
            )

        filepath.write_text(content, encoding="utf-8")
        if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
            print(f"\033[32mCreated Migration:\033[0m {filename}")
        else:
            print(f"Created Migration: {filename}")

    @staticmethod
    def handle_model(args: argparse.Namespace) -> None:
        """Handle make:model command execution."""
        raw_name: str = args.name.strip()
        raw_path = Path(raw_name)
        base_name = raw_path.name
        snake_name = snake_case(base_name)

        table_name = getattr(args, "table", None) or pluralize(snake_name)

        _scaffold_file(
            raw_name=raw_name,
            default_dir_rel="app/models",
            stub_name="model",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            extra_replacements={"table_name": table_name},
            item_type="Model",
        )

        if getattr(args, "migration", False):
            # If -m is added, create migration file for that model
            mig_name = f"create_{table_name}_table"
            mig_args = argparse.Namespace(
                name=mig_name,
                path=None,
                create=table_name,
                table=None,
            )
            MakeCommand.handle_migration(mig_args)

    @staticmethod
    def handle_service(args: argparse.Namespace) -> None:
        """Handle make:service command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/services",
            stub_name="service",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Service",
        )

    @staticmethod
    def handle_request(args: argparse.Namespace) -> None:
        """Handle make:request command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/http/requests",
            stub_name="request",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Request",
        )

    @staticmethod
    def handle_resource(args: argparse.Namespace) -> None:
        """Handle make:resource command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/http/resources",
            stub_name="resource",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Resource",
        )

    @staticmethod
    def handle_repository(args: argparse.Namespace) -> None:
        """Handle make:repository command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/repositories",
            stub_name="repository",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Repository",
        )

    @staticmethod
    def handle_schema(args: argparse.Namespace) -> None:
        """Handle make:schema command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/schemas",
            stub_name="schema",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Schema",
        )
