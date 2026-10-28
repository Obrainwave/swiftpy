from __future__ import annotations

from pathlib import Path

import pytest

from swiftpy.console.cli import CLI
from swiftpy.console.stubs import STUBS_DIR, get_stub


def test_stubs_directory_and_files_exist() -> None:
    assert STUBS_DIR.is_dir()

    expected_stubs = [
        "migration.create.stub",
        "migration.api_keys.stub",
        "migration.alter.stub",
        "migration.update.stub",
        "migration.blank.stub",
        "migration.stub",
        "model.stub",
        "service.stub",
        "request.stub",
        "resource.stub",
        "repository.stub",
        "schema.stub",
    ]
    for filename in expected_stubs:
        stub_file = STUBS_DIR / filename
        assert stub_file.is_file(), f"Expected stub file {filename} does not exist"
        content = stub_file.read_text(encoding="utf-8")
        assert len(content.strip()) > 0


def test_get_stub_loading() -> None:
    # 1. Dotted format
    create_stub = get_stub("migration.create")
    assert "class {class_name}(Migration):" in create_stub
    assert 'await Schema.create("{table_name}", define)' in create_stub

    alter_stub = get_stub("migration.alter")
    assert 'await Schema.table("{table_name}", define)' in alter_stub

    blank_stub = get_stub("migration.blank")
    assert "async def up(self) -> None:" in blank_stub

    model_stub = get_stub("model")
    assert "class {class_name}(Model):" in model_stub

    # 2. Underscore format
    assert get_stub("migration_create") == create_stub
    assert get_stub("migration_alter") == alter_stub
    assert get_stub("migration_blank") == blank_stub

    # 3. Direct with extension
    assert get_stub("migration.create.stub") == create_stub

    # 4. Non-existent stub
    with pytest.raises(FileNotFoundError, match="Stub file not found"):
        get_stub("nonexistent_unknown_template")


def test_make_migration_command_generates_from_stubs(tmp_path: Path) -> None:
    cli = CLI()

    # 1. Create table migration
    cli.run(["make:migration", "create_users_table", "--path", str(tmp_path)])
    create_files = list(tmp_path.glob("*_create_users_table.py"))
    assert len(create_files) == 1
    content_create = create_files[0].read_text(encoding="utf-8")
    assert "class CreateUsersTable(Migration):" in content_create
    assert 'await Schema.create("users", define)' in content_create
    assert "table.id()" in content_create

    # 2. Alter table migration
    cli.run(["make:migration", "add_role_to_users_table", "--path", str(tmp_path)])
    alter_files = list(tmp_path.glob("*_add_role_to_users_table.py"))
    assert len(alter_files) == 1
    content_alter = alter_files[0].read_text(encoding="utf-8")
    assert "class AddRoleToUsersTable(Migration):" in content_alter
    assert 'await Schema.table("users", define)' in content_alter

    # 3. Blank migration
    cli.run(["make:migration", "do_custom_data_backfill", "--path", str(tmp_path)])
    blank_files = list(tmp_path.glob("*_do_custom_data_backfill.py"))
    assert len(blank_files) == 1
    content_blank = blank_files[0].read_text(encoding="utf-8")
    assert "class DoCustomDataBackfill(Migration):" in content_blank
    assert "Schema" not in content_blank


def test_make_model_scaffolding_and_migration_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    # 1. Simple model without migration
    cli.run(["make:model", "Post"])
    model_path = tmp_path / "app" / "models" / "post.py"
    assert model_path.is_file()
    content = model_path.read_text(encoding="utf-8")
    assert "class Post(Model):" in content
    assert '__table__ = "posts"' in content

    # 2. Model with -m flag creates model AND migration
    cli.run(["make:model", "User", "-m"])
    user_model = tmp_path / "app" / "models" / "user.py"
    assert user_model.is_file()
    user_content = user_model.read_text(encoding="utf-8")
    assert "class User(Model):" in user_content
    assert '__table__ = "users"' in user_content

    migration_files = list((tmp_path / "database" / "migrations").glob("*_create_users_table.py"))
    assert len(migration_files) == 1
    mig_content = migration_files[0].read_text(encoding="utf-8")
    assert "class CreateUsersTable(Migration):" in mig_content
    assert 'await Schema.create("users", define)' in mig_content

    # 3. Model with custom --table and -m flag
    cli.run(["make:model", "Account", "--table", "custom_accounts", "-m"])
    account_model = tmp_path / "app" / "models" / "account.py"
    assert account_model.is_file()
    account_content = account_model.read_text(encoding="utf-8")
    assert '__table__ = "custom_accounts"' in account_content

    acc_migrations = list((tmp_path / "database" / "migrations").glob("*_create_custom_accounts_table.py"))
    assert len(acc_migrations) == 1
    acc_mig_content = acc_migrations[0].read_text(encoding="utf-8")
    assert 'await Schema.create("custom_accounts", define)' in acc_mig_content


def test_make_service_request_resource_repository_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    # Service
    cli.run(["make:service", "PaymentService"])
    svc_path = tmp_path / "app" / "services" / "payment_service.py"
    assert svc_path.is_file()
    assert "class PaymentService:" in svc_path.read_text(encoding="utf-8")

    # Request
    cli.run(["make:request", "CreateUserRequest"])
    req_path = tmp_path / "app" / "http" / "requests" / "create_user_request.py"
    assert req_path.is_file()
    req_content = req_path.read_text(encoding="utf-8")
    assert "class CreateUserRequest(FormRequest):" in req_content
    assert "def rules(self) -> dict[str, Any]:" in req_content

    # Resource
    cli.run(["make:resource", "UserResource"])
    res_path = tmp_path / "app" / "http" / "resources" / "user_resource.py"
    assert res_path.is_file()
    res_content = res_path.read_text(encoding="utf-8")
    assert "class UserResource:" in res_content
    assert "def to_dict(self) -> dict[str, Any]:" in res_content

    # Repository
    cli.run(["make:repository", "UserRepository"])
    repo_path = tmp_path / "app" / "repositories" / "user_repository.py"
    assert repo_path.is_file()
    assert "class UserRepository:" in repo_path.read_text(encoding="utf-8")

    # Schema
    cli.run(["make:schema", "OrderSchema"])
    schema_path = tmp_path / "app" / "schemas" / "order_schema.py"
    assert schema_path.is_file()
    assert "class OrderSchema:" in schema_path.read_text(encoding="utf-8")


def test_make_nested_namespaces_and_subdirectories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    # Nested Request: Auth/LoginRequest
    cli.run(["make:request", "Auth/LoginRequest"])
    nested_req = tmp_path / "app" / "http" / "requests" / "Auth" / "login_request.py"
    assert nested_req.is_file()
    assert "class LoginRequest(FormRequest):" in nested_req.read_text(encoding="utf-8")

    # Nested Model: Admin/SuperUser
    cli.run(["make:model", "Admin/SuperUser"])
    nested_model = tmp_path / "app" / "models" / "Admin" / "super_user.py"
    assert nested_model.is_file()
    assert "class SuperUser(Model):" in nested_model.read_text(encoding="utf-8")


def test_make_collision_protection_and_force_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    cli.run(["make:service", "NotificationService"])
    svc_path = tmp_path / "app" / "services" / "notification_service.py"
    assert svc_path.is_file()

    # Running again without --force exits with error
    with pytest.raises(SystemExit, match="already exists"):
        cli.run(["make:service", "NotificationService"])

    # Running with --force overwrites cleanly
    svc_path.write_text("# modified content", encoding="utf-8")
    cli.run(["make:service", "NotificationService", "--force"])
    assert "class NotificationService:" in svc_path.read_text(encoding="utf-8")


def test_make_path_containment_and_validation_p2_cli1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    P2-CLI1: Verifies that path traversal, absolute paths, symlink escapes,
    invalid identifier characters, leading digits, and SQL injection in --table
    are strictly rejected with SystemExit.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(project_dir)

    cli = CLI()

    # 1. Path traversal attempts
    for traversal_name in ("../../x", "/tmp/x/Foo", "a/../../b", "../Escape", "Admin/../../Evil"):
        with pytest.raises(SystemExit):
            cli.run(["make:model", traversal_name])
        with pytest.raises(SystemExit):
            cli.run(["make:service", traversal_name])
        with pytest.raises(SystemExit):
            cli.run(["make:migration", traversal_name])

    assert list(outside_dir.glob("*")) == []

    # 2. Symlinked directory pointing outside base_dir
    models_dir = project_dir / "app" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    symlink_dir = models_dir / "SymlinkEscape"
    symlink_dir.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(SystemExit, match="escapes base directory"):
        cli.run(["make:model", "SymlinkEscape/User"])
    assert not (outside_dir / "user.py").exists()

    # 3. Invalid characters (spaces, hyphens) and leading digits
    for bad_name in ("Foo Bar", "1Foo", "Admin/1User", "Foo-Bar", "User;Drop"):
        with pytest.raises(SystemExit):
            cli.run(["make:model", bad_name])
        with pytest.raises(SystemExit):
            cli.run(["make:request", bad_name])

    # 4. --table and --create validation via P1-F1 identifier validator
    for bad_table in ('x; DROP TABLE', 'users\n', '123table', 'a' * 64, 'users --'):
        with pytest.raises(SystemExit, match="Invalid table identifier"):
            cli.run(["make:model", "ValidModel", "--table", bad_table])
        with pytest.raises(SystemExit, match="Invalid table identifier"):
            cli.run(["make:migration", "add_col_migration", "--table", bad_table])
        with pytest.raises(SystemExit, match="Invalid table identifier"):
            cli.run(["make:migration", "create_tbl_migration", "--create", bad_table])

    # 5. Valid nested path still works
    cli.run(["make:model", "Admin/User"])
    nested_model = project_dir / "app" / "models" / "Admin" / "user.py"
    assert nested_model.is_file()
    assert "class User(Model):" in nested_model.read_text(encoding="utf-8")


def test_make_api_key_command_and_migration_stub_p2a2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P2-A2: Verifies make:api-key outputs prefix.secret key once and scaffolds api_keys migration."""
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    cli.run(
        [
            "make:api-key",
            "--prefix",
            "sk_live_",
            "--pepper",
            "cli-pepper-secret-at-least-32-bytes!",
            "-m",
            "--path",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr().out
    assert "API Key:" in captured
    assert "sk_live_" in captured
    assert "key_prefix:" in captured
    assert "key_hash:" in captured

    mig_files = list(tmp_path.glob("*_create_api_keys_table.py"))
    assert len(mig_files) == 1
    mig_content = mig_files[0].read_text(encoding="utf-8")
    assert 'table.string("key_prefix", length=64).unique().index()' in mig_content
    assert 'table.string("key_hash", length=128)' in mig_content

