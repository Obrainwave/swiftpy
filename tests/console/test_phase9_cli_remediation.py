from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from swiftpy.admin.registry import AdminSite, humanize_name
from swiftpy.admin.views import create_admin_router
from swiftpy.console.cli import CLI
from swiftpy.core.application import Application
from swiftpy.http.router import Router
from swiftpy.mail.templates.renderer import MailTemplateRenderer


def test_new_command_templates_hardened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INF-2 / CLI-1: Verify generated Dockerfile, compose, pyproject, CORS, storage, and email template."""
    monkeypatch.chdir(tmp_path)
    CLI().run(["new", "sample_app"])
    app_dir = tmp_path / "sample_app"

    dockerfile = (app_dir / "Dockerfile").read_text(encoding="utf-8")
    assert "<(" not in dockerfile
    assert "pyproject.toml" in dockerfile

    pyproject = (app_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert '"swiftpy-framework>=0.2.2"' in pyproject

    dockerignore = (app_dir / ".dockerignore").read_text(encoding="utf-8")
    assert "public/storage" in dockerignore
    gitignore = (app_dir / ".gitignore").read_text(encoding="utf-8")
    assert "public/storage" in gitignore

    compose = (app_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.startswith("name: sample_app\n")
    assert '"127.0.0.1:5432:5432"' in compose
    assert '"127.0.0.1:6379:6379"' in compose
    assert '"127.0.0.1:8000:8000"' in compose
    assert "POSTGRES_PASSWORD: ${DB_PASSWORD}" in compose
    assert ":-" not in compose

    assert not (app_dir / "public" / "storage" / ".gitkeep").exists()
    assert not (app_dir / "public" / "storage").exists()

    env_example = (app_dir / ".env.example").read_text(encoding="utf-8")
    assert "\nCORS_ALLOWED_ORIGINS=\n" in env_example

    welcome_src = (app_dir / "app" / "mail" / "welcome_email.py").read_text(encoding="utf-8")
    assert 'template="mail/template"' in welcome_src
    renderer = MailTemplateRenderer(template_dir=app_dir / "views")
    rendered = asyncio.run(
        renderer.render(
            "mail/template",
            {"title": "Welcome", "content": "Hello", "app_name": "SwiftPY"},
        )
    )
    assert rendered.html is not None
    assert "Welcome" in rendered.html


def test_new_force_rejects_symlink_target_and_symlink_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI-1: swiftpy new --force must reject symlinked target dir and symlinked ancestor dirs."""
    monkeypatch.chdir(tmp_path)
    cli = CLI()

    outside = tmp_path / "outside"
    outside.mkdir()

    # 1. Symlinked project root directory
    link_project = tmp_path / "linked_proj"
    link_project.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit, match=r"(?i)symlink|escapes"):
        cli.run(["new", "linked_proj", "--force"])
    assert list(outside.iterdir()) == []

    # 2. Pre-existing project containing a symlinked 'app' subdirectory pointing outside
    proj = tmp_path / "partial_proj"
    proj.mkdir()
    (proj / "app").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit, match=r"(?i)symlink|escapes"):
        cli.run(["new", "partial_proj", "--force"])
    assert not (outside / "models" / "user.py").exists()


def test_make_command_flags_force_and_migration_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI-2: make:migration --force, make:api-key --force, and make:model -m --migration-path --force."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_KEY_PEPPER", "pepper-secret-value-at-least-32-bytes!!")
    cli = CLI()

    cli.run(["make:migration", "create_items_table", "--path", str(tmp_path), "--force"])
    assert len(list(tmp_path.glob("*_create_items_table.py"))) == 1

    cli.run(["make:api-key", "-m", "--path", str(tmp_path), "--force"])
    assert len(list(tmp_path.glob("*_create_api_keys_table.py"))) == 1

    custom_mig_dir = tmp_path / "custom_migrations"
    cli.run(
        [
            "make:model",
            "Widget",
            "-m",
            "--path",
            str(tmp_path / "custom_models"),
            "--migration-path",
            str(custom_mig_dir),
            "--force",
        ]
    )
    assert (tmp_path / "custom_models" / "widget.py").is_file()
    assert len(list(custom_mig_dir.glob("*_create_widgets_table.py"))) == 1


def test_start_command_preserves_port_zero() -> None:
    """CLI-2: swiftpy start --port 0 must pass port=0 rather than falling back to 8000."""
    cli = CLI()
    with patch("swiftpy.console.commands.start.uvicorn.run") as mock_run:
        cli.run(["start", "--port", "0"])
        mock_run.assert_called_once_with(
            "main:app",
            host="127.0.0.1",
            port=0,
            reload=False,
        )


def test_queue_commands_app_lifecycle_and_bounded_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI-2: queue commands validate --limit bounds and run app startup/shutdown with --app."""
    cli = CLI()

    # 1. --limit <= 0 or > 1000 rejected cleanly before touching Redis
    for bad_limit in ("0", "-5", "1001"):
        with pytest.raises(SystemExit, match="Invalid --limit"):
            cli.run(["queue:failed", "--limit", bad_limit])

    # 2. queue:work --app boots and shuts down Application lifespan
    fake_app = Application(auto_discover_providers=False)
    startup_mock = AsyncMock()
    shutdown_mock = AsyncMock()
    work_mock = AsyncMock()
    monkeypatch.setattr(fake_app, "startup", startup_mock)
    monkeypatch.setattr(fake_app, "shutdown", shutdown_mock)
    monkeypatch.setattr(
        "swiftpy.console.commands.queue.load_application", lambda _target: fake_app
    )
    monkeypatch.setattr("swiftpy.console.commands.queue.QueueWorker.work", work_mock)

    cli.run(["queue:work", "--app", "dummy:app", "--stop-when-empty"])
    startup_mock.assert_awaited_once()
    work_mock.assert_awaited_once()
    shutdown_mock.assert_awaited_once()

    # 3. queue:failed --app also boots and shuts down Application lifespan
    startup_mock.reset_mock()
    shutdown_mock.reset_mock()
    monkeypatch.setattr(
        "swiftpy.console.commands.queue.QueueDispatcher.get_failed_jobs",
        AsyncMock(return_value=[]),
    )
    cli.run(["queue:failed", "--app", "dummy:app", "--limit", "10"])
    startup_mock.assert_awaited_once()
    shutdown_mock.assert_awaited_once()


def test_admin_humanize_name_and_router_factory_typing() -> None:
    """CLI-3 / ADMIN-5: humanize_name shared cleanly and get_router returns real Router without cast."""
    assert humanize_name("user_id") == "User"
    assert humanize_name("id") == "ID"
    site = AdminSite()
    _ = create_admin_router  # ensures route compiler wired
    assert isinstance(site.get_router(), Router)
