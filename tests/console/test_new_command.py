from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from swiftpy.console.cli import CLI
from swiftpy.core.application import Application
from swiftpy.database.pool import DatabasePool
from swiftpy.testing.http_client import TestClient


def test_new_command_scaffolds_complete_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    cli.run(["new", "my_api_app"])

    app_dir = tmp_path / "my_api_app"
    assert app_dir.is_dir()

    # 1. Verify on-demand folders are NOT present initially
    assert not (app_dir / "app" / "http" / "controllers").exists()
    assert not (app_dir / "app" / "http" / "resources").exists()

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

    # 3. Verify 7 service providers
    expected_providers = [
        "app_service_provider.py",
        "auth_service_provider.py",
        "cache_service_provider.py",
        "database_service_provider.py",
        "event_service_provider.py",
        "filesystem_service_provider.py",
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
    assert (app_dir / "app" / "models" / "user.py").is_file()
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

    # 7. Verify dedicated views
    assert (app_dir / "views" / "mail" / "template.html").is_file()
    assert (app_dir / "views" / "admin" / "login.html").is_file()
    assert (app_dir / "views" / "admin" / "dashboard.html").is_file()

    # 8. Verify root files
    assert (app_dir / "routes" / "api.py").is_file()
    assert (app_dir / "main.py").is_file()
    assert (app_dir / ".dockerignore").is_file()
    assert (app_dir / "Dockerfile").is_file()
    assert (app_dir / "docker-compose.yml").is_file()
    assert (app_dir / "pyproject.toml").is_file()
    assert (app_dir / "README.md").is_file()

    # 9. Verify .env and .env.example with secure 32-byte APP_KEY
    env_example = (app_dir / ".env.example").read_text(encoding="utf-8")
    assert "APP_KEY=\n" in env_example or env_example.endswith("APP_KEY=")

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

    # Initialize application with generated project root
    app = Application(project_root=app_dir)

    # 1. Verify generated configuration loaded
    assert app.config.get("app.name") == "SwiftPY"
    assert app.config.get("app.cipher") == "AES-256-GCM"

    # 2. Verify all 7 providers were auto-discovered and registered
    provider_names = {type(p).__name__ for p in app._providers}
    assert "AppServiceProvider" in provider_names
    assert "AuthServiceProvider" in provider_names
    assert "CacheServiceProvider" in provider_names
    assert "DatabaseServiceProvider" in provider_names
    assert "EventServiceProvider" in provider_names
    assert "FilesystemServiceProvider" in provider_names
    assert "QueueServiceProvider" in provider_names

    # 3. Mount generated routes/api.py into the application
    from routes.api import router as api_router
    app.include_router(api_router, prefix="/api")

    # 4. Verify route registration
    route_paths = [r.path for r in app.router.routes]
    assert "/api/health" in route_paths
    assert "/api/users" in route_paths

    # 5. Mock database pool connection to test full lifespan startup and shutdown without external postgres
    if app.container.has(DatabasePool):
        pool = app.container.resolve(DatabasePool)
        pool.init = AsyncMock()
        pool.close = AsyncMock()

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

    await app.shutdown()


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
