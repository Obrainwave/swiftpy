from __future__ import annotations

import ast
import importlib
import re
import sys
from pathlib import Path

import pytest

from swiftpy.console.cli import CLI
from swiftpy.console.stubs import STUBS_DIR, get_stub
from swiftpy.database.factories import (
    Factory,
    restore_factory_registry,
    snapshot_factory_registry,
)


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
        "mail.stub",
        "controller.stub",
        "middleware.stub",
        "job.stub",
        "event.stub",
        "listener.stub",
        "provider.stub",
        "seeder.stub",
        "factory.stub",
        "agent.stub",
        "tool.stub",
        "prompt.stub",
    ]
    for filename in expected_stubs:
        stub_file = STUBS_DIR / filename
        assert stub_file.is_file(), f"Expected stub file {filename} does not exist"
        content = stub_file.read_text(encoding="utf-8")
        assert len(content.strip()) > 0


def test_all_stubs_render_and_compile() -> None:
    """Verify that all 24 stubs render cleanly without unresolved placeholders and compile to valid Python."""
    stub_files = list(STUBS_DIR.glob("*.stub"))
    assert len(stub_files) == 24

    for stub_path in stub_files:
        content = stub_path.read_text(encoding="utf-8")
        rendered = (
            content.replace("{class_name}", "SampleTestClass")
            .replace("{table_name}", "sample_table")
            .replace("{tool_name}", "sample_tool")
            .replace("{prompt_name}", "sample_prompt")
            .replace("{model_import}", "app.models.sample")
            .replace("{model_class}", "SampleModel")
        )
        # Ensure no unreplaced template placeholders remain
        unresolved = re.findall(r"\{[a-zA-Z0-9_]+\}", rendered)
        assert not unresolved, (
            f"Stub {stub_path.name} contains unresolved placeholders: {unresolved}"
        )

        # Verify it compiles without syntax errors
        code_obj = compile(rendered, stub_path.name, "exec")
        assert code_obj is not None

        # Verify valid AST parse tree
        parsed = ast.parse(rendered)
        assert isinstance(parsed, ast.Module)


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
    assert "__fillable__ = []" in content
    assert "ClassVar" not in content

    # 2. Model with -m flag creates model AND migration
    cli.run(["make:model", "User", "-m"])
    user_model = tmp_path / "app" / "models" / "user.py"
    assert user_model.is_file()
    user_content = user_model.read_text(encoding="utf-8")
    assert "class User(Model):" in user_content
    assert '__table__ = "users"' in user_content
    assert "__fillable__ = []" in user_content
    assert "ClassVar" not in user_content

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
    assert "class OrderSchema(BaseSchema):" in schema_path.read_text(encoding="utf-8")


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
    """P2-A2: Verifies make:api-key outputs prefix.secret key once and scaffolds api_keys migration securely."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_KEY_PEPPER", "env-pepper-secret-at-least-32-bytes!")
    cli = CLI()

    cli.run(
        [
            "make:api-key",
            "--prefix",
            "sk_live_",
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


def test_scaffold_path_security_edge_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify security controls against symlink escapes, drive prefixes, and traversal forms."""
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    outside_dir = tmp_path / "outside_jail"
    outside_dir.mkdir()
    target_secret = outside_dir / "target_secret.py"
    target_secret.write_text("# Sensitive secret", encoding="utf-8")

    models_dir = tmp_path / "app" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    # 1. Existing symlink at destination file path pointing outside
    symlinked_file = models_dir / "secret_link.py"
    try:
        symlinked_file.symlink_to(target_secret)
        with pytest.raises(SystemExit, match=r"(?i)escapes base directory|already exists|refusing"):
            cli.run(["make:model", "SecretLink", "--force"])
        assert target_secret.read_text(encoding="utf-8") == "# Sensitive secret"
    finally:
        if symlinked_file.is_symlink():
            symlinked_file.unlink()

    # 2. Existing symlinked intermediate directory pointing outside
    symlinked_subdir = models_dir / "escaped_folder"
    try:
        symlinked_subdir.symlink_to(outside_dir)
        with pytest.raises(SystemExit, match=r"(?i)escapes base directory|refusing"):
            cli.run(["make:model", "escaped_folder/HackedModel"])
    finally:
        if symlinked_subdir.is_symlink():
            symlinked_subdir.unlink()

    # 3. Repeated separators / traversal patterns
    for bad_name in ("Model//DoubleSlash", "Bad/../Traversal"):
        with pytest.raises(
            SystemExit,
            match=r"(?i)Invalid .* name segment|escapes base directory|malformed paths|path traversal",
        ):
            cli.run(["make:model", bad_name])


def test_make_new_generators_and_on_demand_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verifies make:controller and make:resource create their target directories on-demand,
    and make:middleware, make:job, make:event, make:listener, make:provider, make:seeder
    scaffold their respective layers.
    """
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    # 1. Controller on-demand creation
    controllers_dir = tmp_path / "app" / "http" / "controllers"
    assert not controllers_dir.exists()
    cli.run(["make:controller", "UserController"])
    assert controllers_dir.is_dir()
    ctrl_file = controllers_dir / "user_controller.py"
    assert ctrl_file.is_file()
    assert "class UserController:" in ctrl_file.read_text(encoding="utf-8")

    # 2. Resource on-demand creation
    resources_dir = tmp_path / "app" / "http" / "resources"
    assert not resources_dir.exists()
    cli.run(["make:resource", "UserResource"])
    assert resources_dir.is_dir()
    res_file = resources_dir / "user_resource.py"
    assert res_file.is_file()
    assert "class UserResource:" in res_file.read_text(encoding="utf-8")

    # 3. Middleware
    cli.run(["make:middleware", "AuthMiddleware"])
    mw_file = tmp_path / "app" / "http" / "middleware" / "auth_middleware.py"
    assert mw_file.is_file()
    assert "class AuthMiddleware(Middleware):" in mw_file.read_text(encoding="utf-8")

    # 4. Job
    cli.run(["make:job", "SendNotificationJob"])
    job_file = tmp_path / "app" / "jobs" / "send_notification_job.py"
    assert job_file.is_file()
    assert "class SendNotificationJob(Job):" in job_file.read_text(encoding="utf-8")

    # 5. Event
    cli.run(["make:event", "UserRegistered"])
    event_file = tmp_path / "app" / "events" / "user_registered.py"
    assert event_file.is_file()
    assert "class UserRegistered:" in event_file.read_text(encoding="utf-8")

    # 6. Listener
    cli.run(["make:listener", "SendWelcomeNotification"])
    listener_file = tmp_path / "app" / "listeners" / "send_welcome_notification.py"
    assert listener_file.is_file()
    assert "class SendWelcomeNotification:" in listener_file.read_text(encoding="utf-8")

    # 7. Provider
    cli.run(["make:provider", "BillingServiceProvider"])
    provider_file = tmp_path / "app" / "providers" / "billing_service_provider.py"
    assert provider_file.is_file()
    assert "class BillingServiceProvider(ServiceProvider):" in provider_file.read_text(encoding="utf-8")

    # 8. Seeder
    cli.run(["make:seeder", "UserSeeder"])
    seeder_file = tmp_path / "database" / "seeders" / "user_seeder.py"
    assert seeder_file.is_file()
    assert "class UserSeeder(Seeder):" in seeder_file.read_text(encoding="utf-8")

    # 9. Agent
    cli.run(["make:agent", "SupportAgent"])
    agent_file = tmp_path / "app" / "ai" / "agents" / "support_agent.py"
    assert agent_file.is_file()
    agent_content = agent_file.read_text(encoding="utf-8")
    assert "class SupportAgent(BaseAgent):" in agent_content
    assert 'name="SupportAgent"' in agent_content

    # 10. Tool
    cli.run(["make:tool", "WeatherLookupTool"])
    tool_file = tmp_path / "app" / "ai" / "tools" / "weather_lookup_tool.py"
    assert tool_file.is_file()
    tool_content = tool_file.read_text(encoding="utf-8")
    assert "class WeatherLookupTool(BaseTool):" in tool_content
    assert 'return "weather_lookup"' in tool_content

    # 11. Prompt (flat and versioned)
    cli.run(["make:prompt", "summarise"])
    prompt_file = tmp_path / "prompts" / "summarise.py"
    assert prompt_file.is_file()
    prompt_content = prompt_file.read_text(encoding="utf-8")
    assert "messages: list[dict[str, str]] =" in prompt_content
    assert 'Prompt.load("summarise", ...)' in prompt_content

    cli.run(["make:prompt", "support/summarise.v2"])
    v2_file = tmp_path / "prompts" / "support" / "summarise.v2.py"
    assert v2_file.is_file()
    v2_content = v2_file.read_text(encoding="utf-8")
    assert 'Prompt.load("support/summarise.v2", ...)' in v2_content


def test_make_ai_generators_security_and_force_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    cli.run(["make:prompt", "classify.v2"])
    prompt_path = tmp_path / "prompts" / "classify.v2.py"
    assert prompt_path.is_file()

    with pytest.raises(SystemExit, match="already exists"):
        cli.run(["make:prompt", "classify.v2"])

    cli.run(["make:prompt", "classify.v2", "--force"])
    assert prompt_path.is_file()

    for bad_prompt in ("../escape", "/abs/prompt", "bad\\prompt", "summarise.py", "a..b"):
        with pytest.raises(SystemExit):
            cli.run(["make:prompt", bad_prompt])

    custom_prompts = tmp_path / "custom_prompts"
    cli.run(["make:prompt", "triage.v1", "--path", str(custom_prompts)])
    assert (custom_prompts / "triage.v1.py").is_file()


def test_make_factory_and_make_model_factory_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    # 1. Explicit Factory suffix infers model app.models.post.Post
    cli.run(["make:factory", "PostFactory"])
    post_factory = tmp_path / "database" / "factories" / "post_factory.py"
    assert post_factory.is_file()
    post_content = post_factory.read_text(encoding="utf-8")
    assert "from app.models.post import Post" in post_content
    assert "class PostFactory(Factory[Post]):" in post_content
    assert "model = Post" in post_content

    # 2. Nested factory without suffix + dotted --model import path
    cli.run(
        [
            "make:factory",
            "Billing/Invoice",
            "--model",
            "app.billing.models.Invoice",
        ]
    )
    invoice_factory = tmp_path / "database" / "factories" / "Billing" / "invoice_factory.py"
    assert invoice_factory.is_file()
    invoice_content = invoice_factory.read_text(encoding="utf-8")
    assert "from app.billing.models import Invoice" in invoice_content
    assert "class InvoiceFactory(Factory[Invoice]):" in invoice_content

    # 3. Overwrite protection and --force
    with pytest.raises(SystemExit, match="already exists"):
        cli.run(["make:factory", "PostFactory"])
    cli.run(["make:factory", "PostFactory", "--force"])
    assert post_factory.is_file()

    # 4. Path traversal rejection and custom --path
    for bad_name in ("../Escape", "/abs/Factory", "bad..name"):
        with pytest.raises(SystemExit):
            cli.run(["make:factory", bad_name])

    custom_dir = tmp_path / "custom_factories"
    cli.run(["make:factory", "Tag", "--path", str(custom_dir)])
    assert (custom_dir / "tag_factory.py").is_file()

    # 5. make:model -f / --factory and --factory-path
    cli.run(["make:model", "Article", "-f"])
    assert (tmp_path / "app" / "models" / "article.py").is_file()
    article_factory = tmp_path / "database" / "factories" / "article_factory.py"
    assert article_factory.is_file()
    article_factory_content = article_factory.read_text(encoding="utf-8")
    assert "from app.models.article import Article" in article_factory_content
    assert "class ArticleFactory(Factory[Article]):" in article_factory_content

    cli.run(["make:model", "Order", "--factory", "--factory-path", str(custom_dir)])
    assert (tmp_path / "app" / "models" / "order.py").is_file()
    assert (custom_dir / "order_factory.py").is_file()

    # 6. Exact `make:factory User`, `make:model User -f`, `make:model Author --factory`,
    #    collision guard, invalid identifiers, and importing + running User.factory()
    cli.run(["make:model", "User", "-f"])
    assert (tmp_path / "app" / "models" / "user.py").is_file()
    user_factory_file = tmp_path / "database" / "factories" / "user_factory.py"
    assert user_factory_file.is_file()

    # Running make:factory User without --force fails cleanly
    with pytest.raises(SystemExit, match="already exists"):
        cli.run(["make:factory", "User"])
    cli.run(["make:factory", "User", "--force"])
    assert user_factory_file.is_file()

    # make:model Author --factory creates model + factory
    cli.run(["make:model", "Author", "--factory"])
    assert (tmp_path / "app" / "models" / "author.py").is_file()
    author_factory_file = tmp_path / "database" / "factories" / "author_factory.py"
    assert author_factory_file.is_file()

    # Invalid factory/model identifiers rejected
    for bad_ident in ("", "../Escape", "Bad Name", "123Invalid", "foo;bar"):
        with pytest.raises(SystemExit):
            cli.run(["make:factory", bad_ident])

    # Package the generated tmp_path app and verify User.factory().make() works cleanly
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "models" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    reg_snap = snapshot_factory_registry()
    saved_mods = {
        k: v
        for k, v in list(sys.modules.items())
        if k in {"app", "database"} or k.startswith(("app.", "database.", "_swiftpy_factories"))
    }
    for k in saved_mods:
        sys.modules.pop(k, None)

    try:
        user_mod = importlib.import_module("app.models.user")
        user_cls = user_mod.User

        resolved = user_cls.factory()
        assert isinstance(resolved, Factory)
        assert type(resolved).__name__ == "UserFactory"
        assert resolved._model_cls is user_cls
        made_user = resolved.make()
        assert isinstance(made_user, user_cls)
    finally:
        restore_factory_registry(reg_snap)
        for k in list(sys.modules):
            if k in {"app", "database"} or k.startswith(
                ("app.", "database.", "_swiftpy_factories")
            ):
                sys.modules.pop(k, None)
        sys.modules.update(saved_mods)



