from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from swiftpy.auth.api_key_guard import (
    generate_api_key,
    get_server_pepper,
    hash_api_key,
)
from swiftpy.console.stubs import get_stub
from swiftpy.core.config import ConfigurationError
from swiftpy.database.identifiers import validate_identifier
from swiftpy.support.strings import pluralize, snake_case

_SEGMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _pascal_case(snake_str: str) -> str:
    """Convert snake_case string to PascalCase class name."""
    components = re.split(r"[^a-zA-Z0-9]+", snake_str)
    return "".join(x.capitalize() for x in components if x)


def _validate_and_split_name(
    raw_name: str,
    item_type: str = "File",
    *,
    allow_nested: bool = True,
) -> tuple[list[str], str, str]:
    """
    Validate a generator name (optionally with nested '/' path segments) and return
    (dir_segments, snake_name, class_name).
    """
    trimmed = raw_name.strip()
    if not trimmed:
        raise SystemExit(f"Invalid {item_type.lower()} name: {raw_name!r}")

    if (
        "\\" in trimmed
        or trimmed.startswith("/")
        or trimmed.endswith("/")
        or "//" in trimmed
        or Path(trimmed).is_absolute()
    ):
        raise SystemExit(
            f"Invalid {item_type.lower()} path {raw_name!r}: absolute or malformed paths are not allowed."
        )

    segments = trimmed.split("/")
    if not allow_nested and len(segments) > 1:
        raise SystemExit(
            f"Invalid {item_type.lower()} name {raw_name!r}: nested paths are not allowed."
        )

    for seg in segments:
        if seg in ("", ".", ".."):
            raise SystemExit(
                f"Invalid {item_type.lower()} path {raw_name!r}: path traversal is not allowed."
            )
        seg_snake = snake_case(seg)
        if not _SEGMENT_PATTERN.fullmatch(seg) or not _SEGMENT_PATTERN.fullmatch(
            seg_snake
        ):
            raise SystemExit(
                f"Invalid {item_type.lower()} name segment {seg!r} in {raw_name!r}. "
                "Each segment must be a valid Python identifier starting with a letter or underscore."
            )

    base_name = segments[-1]
    snake_name = snake_case(base_name)
    class_name = _pascal_case(snake_name)
    if not class_name or not _SEGMENT_PATTERN.fullmatch(class_name):
        raise SystemExit(
            f"Invalid {item_type.lower()} class name {class_name!r} derived from {raw_name!r}."
        )

    return segments[:-1], snake_name, class_name


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
    Resolves class names, filenames, validates path containment, creates target directories,
    and writes stub templates.
    """
    dir_segments, snake_name, class_name = _validate_and_split_name(
        raw_name,
        item_type=item_type,
        allow_nested=True,
    )

    base_dir = Path(override_path) if override_path else Path.cwd() / default_dir_rel
    resolved_base = base_dir.resolve()
    target_dir = base_dir.joinpath(*dir_segments) if dir_segments else base_dir
    filename = f"{snake_name}.py"
    filepath = target_dir / filename

    resolved_target_dir = target_dir.resolve()
    resolved_filepath = filepath.resolve()
    if not resolved_target_dir.is_relative_to(
        resolved_base
    ) or not resolved_filepath.is_relative_to(resolved_base):
        raise SystemExit(
            f"Invalid {item_type.lower()} path {raw_name!r}: target escapes base directory '{resolved_base}'."
        )

    target_dir.mkdir(parents=True, exist_ok=True)

    # Re-verify containment after directory creation
    if not filepath.resolve().is_relative_to(resolved_base) or filepath.is_symlink():
        raise SystemExit(
            f"Invalid {item_type.lower()} path {raw_name!r}: target escapes base directory '{resolved_base}'."
        )

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

        # 8. make:mail
        parser_mail = subparsers.add_parser(
            "make:mail",
            help="Create a new Mailable class",
        )
        parser_mail.add_argument(
            "name",
            help="The name of the mailable (e.g., WelcomeEmail)",
        )
        parser_mail.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the mailable should be saved (defaults to app/mail)",
        )
        parser_mail.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the mailable if it already exists",
        )
        parser_mail.set_defaults(
            _handler=MakeCommand.handle_mail,
        )

        # 9. make:api-key
        parser_api_key = subparsers.add_parser(
            "make:api-key",
            help="Generate a new prefix.secret API key and its peppered HMAC-SHA256 hash",
        )
        parser_api_key.add_argument(
            "--prefix",
            dest="prefix",
            default="sk_live_",
            help="Prefix for the API key (default: sk_live_)",
        )
        parser_api_key.add_argument(
            "--pepper",
            dest="pepper",
            default=None,
            help="Server pepper for HMAC-SHA256 hashing (defaults to AUTH_KEY_PEPPER config)",
        )
        parser_api_key.add_argument(
            "-m",
            "--migration",
            action="store_true",
            help="Also create the api_keys table migration file",
        )
        parser_api_key.add_argument(
            "--path",
            dest="path",
            default=None,
            help="Migration target directory when --migration is specified",
        )
        parser_api_key.set_defaults(
            _handler=MakeCommand.handle_api_key,
        )

        # 10. make:controller
        parser_controller = subparsers.add_parser(
            "make:controller",
            help="Create a new Controller class",
        )
        parser_controller.add_argument(
            "name",
            help="The name of the controller (e.g., UserController)",
        )
        parser_controller.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the controller should be saved (defaults to app/http/controllers)",
        )
        parser_controller.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the controller if it already exists",
        )
        parser_controller.set_defaults(
            _handler=MakeCommand.handle_controller,
        )

        # 11. make:middleware
        parser_middleware = subparsers.add_parser(
            "make:middleware",
            help="Create a new ASGI Middleware class",
        )
        parser_middleware.add_argument(
            "name",
            help="The name of the middleware (e.g., AuthMiddleware)",
        )
        parser_middleware.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the middleware should be saved (defaults to app/http/middleware)",
        )
        parser_middleware.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the middleware if it already exists",
        )
        parser_middleware.set_defaults(
            _handler=MakeCommand.handle_middleware,
        )

        # 12. make:job
        parser_job = subparsers.add_parser(
            "make:job",
            help="Create a new queued background Job class",
        )
        parser_job.add_argument(
            "name",
            help="The name of the job (e.g., SendNotificationJob)",
        )
        parser_job.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the job should be saved (defaults to app/jobs)",
        )
        parser_job.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the job if it already exists",
        )
        parser_job.set_defaults(
            _handler=MakeCommand.handle_job,
        )

        # 13. make:event
        parser_event = subparsers.add_parser(
            "make:event",
            help="Create a new domain Event dataclass",
        )
        parser_event.add_argument(
            "name",
            help="The name of the event (e.g., UserRegistered)",
        )
        parser_event.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the event should be saved (defaults to app/events)",
        )
        parser_event.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the event if it already exists",
        )
        parser_event.set_defaults(
            _handler=MakeCommand.handle_event,
        )

        # 14. make:listener
        parser_listener = subparsers.add_parser(
            "make:listener",
            help="Create a new event Listener class",
        )
        parser_listener.add_argument(
            "name",
            help="The name of the listener (e.g., SendWelcomeNotification)",
        )
        parser_listener.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the listener should be saved (defaults to app/listeners)",
        )
        parser_listener.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the listener if it already exists",
        )
        parser_listener.set_defaults(
            _handler=MakeCommand.handle_listener,
        )

        # 15. make:provider
        parser_provider = subparsers.add_parser(
            "make:provider",
            help="Create a new ServiceProvider class",
        )
        parser_provider.add_argument(
            "name",
            help="The name of the service provider (e.g., AppServiceProvider)",
        )
        parser_provider.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the provider should be saved (defaults to app/providers)",
        )
        parser_provider.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the provider if it already exists",
        )
        parser_provider.set_defaults(
            _handler=MakeCommand.handle_provider,
        )

        # 16. make:seeder
        parser_seeder = subparsers.add_parser(
            "make:seeder",
            help="Create a new database Seeder class",
        )
        parser_seeder.add_argument(
            "name",
            help="The name of the seeder (e.g., UserSeeder)",
        )
        parser_seeder.add_argument(
            "--path",
            dest="path",
            default=None,
            help="The directory where the seeder should be saved (defaults to database/seeders)",
        )
        parser_seeder.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the seeder if it already exists",
        )
        parser_seeder.set_defaults(
            _handler=MakeCommand.handle_seeder,
        )

    @staticmethod
    def handle_migration(args: argparse.Namespace) -> None:
        """Handle make:migration command execution."""
        raw_name: str = args.name.strip()
        if not raw_name or "/" in raw_name or "\\" in raw_name or ".." in raw_name:
            raise SystemExit(f"Invalid migration name: {raw_name!r}")

        snake_name = re.sub(r"[^a-zA-Z0-9_]+", "_", raw_name.lower()).strip("_")
        if not snake_name or not _SEGMENT_PATTERN.fullmatch(snake_name):
            raise SystemExit(f"Invalid migration name: {raw_name!r}")

        class_name = _pascal_case(snake_name)
        if not class_name or not _SEGMENT_PATTERN.fullmatch(class_name):
            raise SystemExit(f"Invalid migration class name: {class_name!r}")

        raw_table = getattr(args, "create", None) or getattr(args, "table", None)
        is_create = bool(getattr(args, "create", None))
        if raw_table is not None:
            try:
                table_name: str | None = validate_identifier(
                    str(raw_table), param_name="table"
                )
            except ValueError as exc:
                raise SystemExit(f"Invalid table identifier: {exc}") from exc
        else:
            guessed_table, is_create = _guess_table(snake_name)
            if guessed_table is not None:
                try:
                    table_name = validate_identifier(
                        guessed_table, param_name="table"
                    )
                except ValueError as exc:
                    raise SystemExit(f"Invalid table identifier: {exc}") from exc
            else:
                table_name = None

        target_dir = (
            Path(args.path) if args.path else Path.cwd() / "database" / "migrations"
        )
        resolved_base = target_dir.resolve()
        timestamp = datetime.now(tz=UTC).strftime("%Y_%m_%d_%H%M%S")
        filename = f"{timestamp}_{snake_name}.py"
        filepath = target_dir / filename

        if not filepath.resolve().is_relative_to(resolved_base):
            raise SystemExit(
                f"Invalid migration path {raw_name!r}: target escapes base directory '{resolved_base}'."
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        if not filepath.resolve().is_relative_to(resolved_base) or filepath.is_symlink():
            raise SystemExit(
                f"Invalid migration path {raw_name!r}: target escapes base directory '{resolved_base}'."
            )

        if filepath.exists() and not getattr(args, "force", False):
            raise SystemExit(
                f"Migration already exists: {filepath}. Use --force to overwrite."
            )

        stub_override = getattr(args, "stub", None)
        if stub_override:
            content = (
                get_stub(stub_override)
                .replace("{class_name}", class_name)
                .replace("{table_name}", table_name or "api_keys")
            )
        elif table_name is None:
            content = get_stub("migration.blank").replace("{class_name}", class_name)
        elif is_create and table_name == "api_keys":
            content = (
                get_stub("migration.api_keys")
                .replace("{class_name}", class_name)
                .replace("{table_name}", table_name)
            )
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
        _, snake_name, _ = _validate_and_split_name(
            raw_name,
            item_type="Model",
            allow_nested=True,
        )

        raw_table = getattr(args, "table", None)
        if raw_table is not None:
            try:
                table_name = validate_identifier(str(raw_table), param_name="table")
            except ValueError as exc:
                raise SystemExit(f"Invalid table identifier: {exc}") from exc
        else:
            try:
                table_name = validate_identifier(
                    pluralize(snake_name), param_name="table"
                )
            except ValueError as exc:
                raise SystemExit(f"Invalid table identifier: {exc}") from exc

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
    def handle_api_key(args: argparse.Namespace) -> None:
        """Handle make:api-key command execution (P2-A2 requirement 5)."""
        prefix_arg: str = getattr(args, "prefix", "sk_live_") or "sk_live_"
        pepper_arg: str | None = getattr(args, "pepper", None)

        try:
            active_pepper: str | bytes = (
                pepper_arg if pepper_arg is not None else get_server_pepper()
            )
        except ConfigurationError as exc:
            raise SystemExit(str(exc)) from exc

        full_key, key_prefix, secret = generate_api_key(prefix=prefix_arg)
        key_hash = hash_api_key(secret, pepper=active_pepper)

        print("Generated API Key (save this secret now; it will not be shown again):")
        print(f"  API Key:    {full_key}")
        print(f"  key_prefix: {key_prefix}")
        print(f"  key_hash:   {key_hash}")

        if getattr(args, "migration", False):
            mig_args = argparse.Namespace(
                name="create_api_keys_table",
                path=getattr(args, "path", None),
                create="api_keys",
                table=None,
                stub="migration.api_keys",
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

    @staticmethod
    def handle_mail(args: argparse.Namespace) -> None:
        """Handle make:mail command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/mail",
            stub_name="mail",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Mailable",
        )

    @staticmethod
    def handle_controller(args: argparse.Namespace) -> None:
        """Handle make:controller command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/http/controllers",
            stub_name="controller",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Controller",
        )

    @staticmethod
    def handle_middleware(args: argparse.Namespace) -> None:
        """Handle make:middleware command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/http/middleware",
            stub_name="middleware",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Middleware",
        )

    @staticmethod
    def handle_job(args: argparse.Namespace) -> None:
        """Handle make:job command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/jobs",
            stub_name="job",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Job",
        )

    @staticmethod
    def handle_event(args: argparse.Namespace) -> None:
        """Handle make:event command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/events",
            stub_name="event",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Event",
        )

    @staticmethod
    def handle_listener(args: argparse.Namespace) -> None:
        """Handle make:listener command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/listeners",
            stub_name="listener",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Listener",
        )

    @staticmethod
    def handle_provider(args: argparse.Namespace) -> None:
        """Handle make:provider command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="app/providers",
            stub_name="provider",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Provider",
        )

    @staticmethod
    def handle_seeder(args: argparse.Namespace) -> None:
        """Handle make:seeder command execution."""
        _scaffold_file(
            raw_name=args.name,
            default_dir_rel="database/seeders",
            stub_name="seeder",
            override_path=getattr(args, "path", None),
            force=bool(getattr(args, "force", False)),
            item_type="Seeder",
        )
