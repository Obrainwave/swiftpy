from __future__ import annotations

import base64
import importlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from swiftpy.ai.prompts import PromptLoader
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.auth.token_blacklist import is_token_blacklist_enabled
from swiftpy.cache.cache import Cache
from swiftpy.console.cli import CLI
from swiftpy.core.application import Application
from swiftpy.database.pool import DatabasePool
from swiftpy.support.redis_config import resolve_redis_params
from swiftpy.testing.http_client import TestClient


def test_new_command_scaffolds_complete_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    cli.run(["new", "my_api_app"])

    app_dir = tmp_path / "my_api_app"
    assert app_dir.is_dir()

    # 1. Verify starter controllers, resources, AI folders, and prompts exist
    assert (app_dir / "app" / "http" / "controllers" / "user_controller.py").is_file()
    assert (app_dir / "app" / "http" / "resources" / "user_resource.py").is_file()
    assert (app_dir / "app" / "ai" / "agents" / "assistant_agent.py").is_file()
    assert (app_dir / "app" / "ai" / "tools" / "system_status_tool.py").is_file()
    assert (app_dir / "app" / "ai" / "pipelines" / ".gitkeep").is_file()
    assert (app_dir / "app" / "ai" / "providers" / ".gitkeep").is_file()
    assert (app_dir / "app" / "ai" / "vectorstores" / ".gitkeep").is_file()
    assert (app_dir / "app" / "ai" / "observers" / ".gitkeep").is_file()
    assert (app_dir / "prompts" / "summarise.py").is_file()
    assert (app_dir / "prompts" / "classify.py").is_file()

    # 2. Verify all 11 config files exist
    expected_configs = [
        "ai.py",
        "app.py",
        "auth.py",
        "cache.py",
        "cors.py",
        "database.py",
        "events.py",
        "filesystems.py",
        "logging.py",
        "mail.py",
        "queue.py",
    ]
    for cfg in expected_configs:
        cfg_file = app_dir / "config" / cfg
        assert cfg_file.is_file(), f"Missing config file: {cfg}"
        content = cfg_file.read_text(encoding="utf-8")
        assert "CONFIG =" in content or "CONFIG:" in content

    # 3. Verify 8 service providers
    expected_providers = [
        "app_service_provider.py",
        "auth_service_provider.py",
        "cache_service_provider.py",
        "database_service_provider.py",
        "event_service_provider.py",
        "filesystem_service_provider.py",
        "mail_service_provider.py",
        "queue_service_provider.py",
    ]
    for prov in expected_providers:
        prov_file = app_dir / "app" / "providers" / prov
        assert prov_file.is_file(), f"Missing provider: {prov}"

    # 4. Verify app/http structure
    assert (app_dir / "app" / "http" / "handler" / "exceptions.py").is_file()
    assert (app_dir / "app" / "http" / "middleware" / "auth_middleware.py").is_file()
    assert (app_dir / "app" / "http" / "middleware" / "admin_middleware.py").is_file()
    assert (app_dir / "app" / "http" / "requests" / "create_user_request.py").is_file()

    # 5. Verify sample domain files
    user_model_file = app_dir / "app" / "models" / "user.py"
    assert user_model_file.is_file()
    user_model_src = user_model_file.read_text(encoding="utf-8")
    assert '__fillable__ = ["name", "email", "password", "is_admin"]' in user_model_src
    assert '__hidden__ = ["password"]' in user_model_src
    assert "ClassVar" not in user_model_src
    assert (app_dir / "app" / "schemas" / "user_schema.py").is_file()
    assert (app_dir / "app" / "services" / "user_service.py").is_file()
    assert (app_dir / "app" / "repositories" / "user_repository.py").is_file()
    assert (app_dir / "app" / "events" / "user_registered.py").is_file()
    assert (app_dir / "app" / "listeners" / "send_welcome_notification.py").is_file()
    assert (app_dir / "app" / "jobs" / "send_notification_job.py").is_file()
    assert (app_dir / "app" / "mail" / "welcome_email.py").is_file()

    # 6. Verify database migrations and seeders
    assert (
        app_dir
        / "database"
        / "migrations"
        / "2026_01_01_000000_create_users_table.py"
    ).is_file()
    assert (app_dir / "database" / "seeders" / "database_seeder.py").is_file()

    # 7. Verify dedicated views (admin views removed)
    assert (app_dir / "views" / "mail" / "template.html").is_file()
    assert not (app_dir / "views" / "admin").exists()

    # 8. Verify root files
    assert (app_dir / "routes" / "api.py").is_file()
    assert (app_dir / "main.py").is_file()
    assert (app_dir / ".dockerignore").is_file()
    assert (app_dir / "Dockerfile").is_file()
    assert (app_dir / "docker-compose.yml").is_file()
    assert (app_dir / "pyproject.toml").is_file()
    assert (app_dir / "README.md").is_file()

    # 9. Verify .env and .env.example with secure 32-byte APP_KEY and JWT_SECRET
    env_example = (app_dir / ".env.example").read_text(encoding="utf-8")
    assert "APP_KEY=\n" in env_example or env_example.endswith("APP_KEY=")
    assert "JWT_SECRET=\n" in env_example or env_example.endswith("JWT_SECRET=")

    env_active = (app_dir / ".env").read_text(encoding="utf-8")
    assert "APP_KEY=" in env_active
    lines = [ln.strip() for ln in env_active.splitlines() if ln.startswith("APP_KEY=")]
    assert len(lines) == 1
    key_val = lines[0].split("=", 1)[1]
    assert key_val.startswith("base64:")
    decoded_key = base64.b64decode(
        key_val.removeprefix("base64:"),
        validate=True,
    )
    assert len(decoded_key) == 32
    jwt_lines = [ln.strip() for ln in env_active.splitlines() if ln.startswith("JWT_SECRET=")]
    assert len(jwt_lines) == 1
    assert len(jwt_lines[0].split("=", 1)[1]) >= 32

    # 10. Verify .env is excluded from git and docker build context
    dockerignore = (app_dir / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in dockerignore
    gitignore = (app_dir / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore

    # 11. Two independent projects receive different keys
    cli.run(["new", "second_project"])
    second_env = (tmp_path / "second_project" / ".env").read_text(encoding="utf-8")
    second_key = next(
        ln.strip() for ln in second_env.splitlines() if ln.startswith("APP_KEY=")
    ).split("=", 1)[1]
    assert second_key != key_val
    second_decoded = base64.b64decode(
        second_key.removeprefix("base64:"), validate=True
    )
    assert len(second_decoded) == 32


def test_new_command_validation_and_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    # 1. Invalid names
    for bad_name in ("../escape", "invalid name with spaces", "nested/project", "/tmp/abs"):
        with pytest.raises(SystemExit, match="Invalid project name"):
            cli.run(["new", bad_name])

    # 2. Existing directory rejection without force
    project_dir = tmp_path / "existing_dir"
    project_dir.mkdir()
    (project_dir / "some_file.txt").write_text("content", encoding="utf-8")

    with pytest.raises(SystemExit, match="already exists and is not empty"):
        cli.run(["new", "existing_dir"])

    # 3. Overwrite with --force preserves existing .env, secrets, and custom user files
    custom_secret_key = "base64:" + base64.b64encode(b"A" * 32).decode("ascii")
    (project_dir / ".env").write_text(f"APP_KEY={custom_secret_key}\nCUSTOM_VAR=preserved\n", encoding="utf-8")
    custom_file = project_dir / "user_notes.txt"
    custom_file.write_text("User owned custom content", encoding="utf-8")

    cli.run(["new", "existing_dir", "--force"])

    assert (project_dir / "main.py").is_file()
    preserved_env = (project_dir / ".env").read_text(encoding="utf-8")
    assert f"APP_KEY={custom_secret_key}" in preserved_env
    assert "CUSTOM_VAR=preserved" in preserved_env
    assert custom_file.read_text(encoding="utf-8") == "User owned custom content"


@pytest.mark.asyncio
async def test_scaffolded_app_e2e_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Genuine fresh-project end-to-end test:
    Scaffolds a project, loads its generated configuration, initializes providers,
    mounts generated modular routes, executes normal startup and shutdown lifecycle hooks,
    and makes live HTTP requests against the generated application.
    """
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    cli.run(["new", "e2e_app"])
    app_dir = tmp_path / "e2e_app"

    # Prepend project to sys.path so generated modules can be imported
    monkeypatch.syspath_prepend(str(app_dir))

    # Ensure JWT_SECRET is present in env when Config.load runs without dotenv auto-loading
    if not (monkeypatch and os.getenv("JWT_SECRET")):
        monkeypatch.setenv("JWT_SECRET", "e2e-scaffold-jwt-secret-at-least-32-bytes!!")

    prefixes = ("main", "app", "routes")
    saved_modules = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name in prefixes or name.startswith(("app.", "routes."))
    }
    for name in saved_modules:
        sys.modules.pop(name, None)

    try:
        # Initialize application with generated project root
        app = Application(project_root=app_dir)

        # 1. Verify generated configuration loaded
        assert app.config.get("app.name") == "SwiftPY"
        assert app.config.get("app.cipher") == "AES-256-GCM"

        # 2. Verify all 8 providers were auto-discovered and registered
        provider_names = {type(p).__name__ for p in app._providers}
        assert "AppServiceProvider" in provider_names
        assert "AuthServiceProvider" in provider_names
        assert "CacheServiceProvider" in provider_names
        assert "DatabaseServiceProvider" in provider_names
        assert "EventServiceProvider" in provider_names
        assert "FilesystemServiceProvider" in provider_names
        assert "MailServiceProvider" in provider_names
        assert "QueueServiceProvider" in provider_names

        # 3. Mount generated routes/api.py into the application
        api_module = importlib.import_module("routes.api")
        app.include_router(api_module.router, prefix="/api")

        # 4. Verify route registration
        route_paths = [r.path for r in app.router.routes]
        assert "/api/health" in route_paths
        assert "/api/users" in route_paths

        # 5. Mock database pool connection to test full lifespan startup and shutdown without external postgres
        if app.container.has(DatabasePool):
            pool = app.container.resolve(DatabasePool)
            monkeypatch.setattr(pool, "init", AsyncMock())
            monkeypatch.setattr(pool, "close", AsyncMock())

        # 6. Execute normal application lifespan startup and shutdown
        await app.startup()
        assert app._started is True

        # 7. Execute live HTTP requests via TestClient
        client = TestClient(app)
        res_health = await client.get("/api/health")
        assert res_health.status_code == 200
        assert res_health.json() == {"status": "ok", "app": "SwiftPY"}

        res_users = await client.get("/api/users")
        assert res_users.status_code == 200
        data = res_users.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["email"] == "alice@example.com"
        assert data[0]["name"] == "Alice Doe"

        # Verify starter AI agent, tool, and named prompt integration
        assistant_mod = importlib.import_module("app.ai.agents.assistant_agent")
        agent = assistant_mod.AssistantAgent(config=app.config)
        assert agent.name == "AssistantAgent"

        prompt_loader = PromptLoader(base_dir=app_dir / "prompts", config=app.config)
        rendered_summarise = prompt_loader.load(
            "summarise",
            style="bullet-point",
            text="SwiftPY 0.3.0 ships prompt versioning.",
        )
        assert rendered_summarise == [
            {
                "role": "system",
                "content": (
                    "You are a precise technical editor. Summarise the provided content "
                    "clearly in bullet-point style."
                ),
            },
            {
                "role": "user",
                "content": "SwiftPY 0.3.0 ships prompt versioning.",
            },
        ]

        await app.shutdown()
    finally:
        for name in list(sys.modules):
            if name in prefixes or name.startswith(("app.", "routes.")):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


def test_environment_overrides_in_scaffolded_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that environment variable overrides produce deliberate behavior."""
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    cli.run(["new", "override_app"])
    app_dir = tmp_path / "override_app"

    monkeypatch.setenv("APP_NAME", "CustomProductionPlatform")
    monkeypatch.setenv("APP_ENV", "staging")

    app = Application(project_root=app_dir)
    assert app.config.get("app.name") == "CustomProductionPlatform"
    assert app.config.get("app.env") == "staging"


def test_generated_project_passes_ruff_and_mypy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generated project source must pass ruff and strict mypy (framework imports followed silently)."""
    monkeypatch.chdir(tmp_path)
    CLI().run(["new", "quality_app"])
    app_dir = tmp_path / "quality_app"
    bin_dir = Path(sys.executable).parent
    repo_root = Path(__file__).resolve().parents[2]

    # Generated CLI models and AI/HTTP artifacts inside the project must pass ruff and strict mypy
    monkeypatch.chdir(app_dir)
    CLI().run(["make:model", "Article", "-m"])
    CLI().run(["make:model", "Billing/Invoice"])
    CLI().run(["make:controller", "PostController"])
    CLI().run(["make:resource", "PostResource"])
    CLI().run(["make:service", "PostService"])
    CLI().run(["make:agent", "SupportAgent"])
    CLI().run(["make:tool", "WeatherLookupTool"])
    CLI().run(["make:prompt", "summarise.v2"])

    ruff = subprocess.run(
        [str(bin_dir / "ruff"), "check", "."],
        cwd=app_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ruff.returncode == 0, ruff.stdout + ruff.stderr

    env = {**os.environ, "MYPYPATH": str(repo_root)}
    mypy = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--follow-imports=silent",
            "--explicit-package-bases",
            "--ignore-missing-imports",
            ".",
        ],
        cwd=app_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert mypy.returncode == 0, mypy.stdout + mypy.stderr


def test_scaffolded_dotenv_overrides_and_framework_config_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generated config/*.py must honor .env without os.environ mutation and match framework config keys."""
    monkeypatch.chdir(tmp_path)
    CLI().run(["new", "dotenv_app"])
    app_dir = tmp_path / "dotenv_app"

    for key in (
        "APP_NAME",
        "APP_DEBUG",
        "JWT_SECRET",
        "JWT_ACCESS_TTL",
        "JWT_REFRESH_TTL",
        "AUTH_BLACKLIST_ENABLED",
        "CACHE_DEFAULT_TTL",
        "EVENTS_PREFIX",
        "REDIS_CACHE_DB",
        "REDIS_QUEUE_DB",
        "REDIS_EVENTS_DB",
        "CORS_ALLOWED_ORIGINS",
        "CORS_ALLOW_CREDENTIALS",
    ):
        monkeypatch.delenv(key, raising=False)

    (app_dir / ".env").write_text(
        "\n".join(
            [
                "APP_NAME=DotenvCustomApp",
                "APP_DEBUG=true",
                "APP_KEY=base64:" + base64.b64encode(b"k" * 32).decode("ascii"),
                "JWT_SECRET=dotenv-jwt-secret-value-at-least-32-bytes",
                "JWT_ACCESS_TTL=321",
                "JWT_REFRESH_TTL=12345",
                "AUTH_BLACKLIST_ENABLED=false",
                "CACHE_DEFAULT_TTL=900",
                "EVENTS_PREFIX=custom_events:",
                "REDIS_CACHE_DB=4",
                "REDIS_QUEUE_DB=5",
                "REDIS_EVENTS_DB=6",
                "CORS_ALLOWED_ORIGINS=https://app.example.com",
                "CORS_ALLOW_CREDENTIALS=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    app = Application(project_root=app_dir)

    assert app.config.get("app.name") == "DotenvCustomApp"
    assert app.config.get("app.debug") is True

    issuer = JWTIssuer.from_config(app.config)
    assert issuer.access_token_ttl == 321
    assert issuer.refresh_token_ttl == 12345
    assert is_token_blacklist_enabled(app.config) is False

    assert app.config.get("cache.default_ttl") == 900
    cache_mgr = Cache(config=app.config)
    redis_store = app.config.get("cache.stores.redis")
    assert isinstance(redis_store, dict)
    assert cache_mgr._resolve_store_ttl(redis_store) == 900.0
    assert resolve_redis_params(app.config, section="cache")["database"] == 4
    assert resolve_redis_params(app.config, section="queue")["database"] == 5
    events_redis = resolve_redis_params(app.config, section="events")
    assert events_redis["database"] == 6
    assert app.config.get("events.redis.prefix") == "custom_events:"

    assert app.config.get("cors.allowed_origins") == ["https://app.example.com"]
    assert app.config.get("cors.expose_headers") == ["X-Request-ID"]
    assert app.config.get("cors.allow_credentials") is True


def test_scaffolded_packaging_and_plain_mypy_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generated pyproject.toml configures package discovery and strict mypy defaults."""
    monkeypatch.chdir(tmp_path)
    CLI().run(["new", "pkg_app"])
    app_dir = tmp_path / "pkg_app"

    pyproject = (app_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "[build-system]" in pyproject
    assert "[tool.setuptools.packages.find]" in pyproject
    assert "[tool.mypy]" in pyproject
    assert "[tool.ruff.lint.per-file-ignores]" in pyproject
    assert '"app/models/**/*.py" = ["RUF012"]' in pyproject

    compose = (app_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.startswith("name: pkg_app\n")
    assert "image: pgvector/pgvector:pg16" in compose
    assert "DB_HOST: postgres" in compose
    assert "REDIS_HOST: redis" in compose
    assert "POSTGRES_DB: ${DB_DATABASE}" in compose
    assert "POSTGRES_USER: ${DB_USERNAME}" in compose
    assert "POSTGRES_PASSWORD: ${DB_PASSWORD}" in compose
    assert "pg_isready -U ${DB_USERNAME} -d ${DB_DATABASE}" in compose
    assert ":-" not in compose

    env_active = (app_dir / ".env").read_text(encoding="utf-8")
    assert "\nDB_DATABASE=pkg_app\n" in env_active
    assert "\nJWT_ACCESS_TTL=3600\n" in env_active
    assert "\nJWT_REFRESH_TTL=604800\n" in env_active
    env_example = (app_dir / ".env.example").read_text(encoding="utf-8")
    assert "\nDB_DATABASE=pkg_app\n" in env_example
    assert "\nJWT_ACCESS_TTL=3600\n" in env_example
    assert "\nJWT_REFRESH_TTL=604800\n" in env_example
    auth_cfg = (app_dir / "config" / "auth.py").read_text(encoding="utf-8")
    assert 'env("JWT_ACCESS_TTL", "3600")' in auth_cfg
    assert 'env("JWT_REFRESH_TTL", "604800")' in auth_cfg
    db_cfg = (app_dir / "config" / "database.py").read_text(encoding="utf-8")
    assert '"database": env("DB_DATABASE", "pkg_app")' in db_cfg

    readme = (app_dir / "README.md").read_text(encoding="utf-8")
    assert "uv sync" in readme
    assert "pip install -e ." in readme

    repo_root = Path(__file__).resolve().parents[2]
    mypy = subprocess.run(
        [sys.executable, "-m", "mypy", "."],
        cwd=app_dir,
        env={**os.environ, "MYPYPATH": str(repo_root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert mypy.returncode == 0, mypy.stdout + mypy.stderr


async def test_scaffolded_main_wires_exception_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generated main.py registers app.http.handler.exceptions onto Application."""
    monkeypatch.chdir(tmp_path)
    CLI().run(["new", "main_app"])
    app_dir = tmp_path / "main_app"

    monkeypatch.chdir(app_dir)
    monkeypatch.syspath_prepend(str(app_dir))
    monkeypatch.setenv("JWT_SECRET", "main-test-jwt-secret-at-least-32-bytes!!")

    prefixes = ("main", "app", "routes")
    saved_modules = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name in prefixes or name.startswith(("app.", "routes."))
    }
    for name in saved_modules:
        sys.modules.pop(name, None)

    try:
        main_module = importlib.import_module("main")
        exc_module = importlib.import_module("app.http.handler.exceptions")
        loaded_app = main_module.app
        assert isinstance(loaded_app, Application)
        conflict_cls = exc_module.ResourceConflictException

        @loaded_app.get("/conflict-test")
        async def conflict_endpoint() -> dict[str, str]:
            raise conflict_cls("duplicate entry")

        @loaded_app.get("/conflict-default")
        async def conflict_default_endpoint() -> dict[str, str]:
            raise conflict_cls()

        if loaded_app.container.has(DatabasePool):
            pool = loaded_app.container.resolve(DatabasePool)
            monkeypatch.setattr(pool, "init", AsyncMock())
            monkeypatch.setattr(pool, "close", AsyncMock())

        async with TestClient(loaded_app) as client:
            home_resp = await client.get("/")
            assert home_resp.status_code == 200
            assert home_resp.json() == {
                "status": "ok",
                "message": "Hello from SwiftPY",
                "app": "SwiftPY",
            }

            resp = await client.get("/conflict-test")
            assert resp.status_code == 409
            assert resp.json() == {"detail": "duplicate entry"}

            resp_default = await client.get("/conflict-default")
            assert resp_default.status_code == 409
            assert resp_default.json() == {"detail": "Resource conflict"}
    finally:
        for name in list(sys.modules):
            if name in prefixes or name.startswith(("app.", "routes.")):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
