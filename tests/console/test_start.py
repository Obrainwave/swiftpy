from __future__ import annotations

from unittest.mock import patch

import pytest

from swiftpy.console.cli import CLI
from swiftpy.core.config import Config


def test_start_command_defaults() -> None:
    """Verify swiftpy start uses default target, host, port, and reload=False."""
    cli = CLI()

    with patch("swiftpy.console.commands.start.uvicorn.run") as mock_run:
        cli.run(["start"])
        mock_run.assert_called_once_with(
            "main:app",
            host="127.0.0.1",
            port=8000,
            reload=False,
        )


def test_start_command_custom_arguments() -> None:
    """Verify swiftpy start parses custom target, host, port, and reload flag."""
    cli = CLI()

    with patch("swiftpy.console.commands.start.uvicorn.run") as mock_run:
        cli.run([
            "start",
            "app.server:app",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--reload",
        ])
        mock_run.assert_called_once_with(
            "app.server:app",
            host="0.0.0.0",
            port=9000,
            reload=True,
        )


def test_start_command_workers_support() -> None:
    """Verify --workers passes worker count to uvicorn when greater than 1."""
    cli = CLI()

    with patch("swiftpy.console.commands.start.uvicorn.run") as mock_run:
        cli.run(["start", "--workers", "4"])
        mock_run.assert_called_once_with(
            "main:app",
            host="127.0.0.1",
            port=8000,
            reload=False,
            workers=4,
        )


def test_start_command_refuses_reload_with_multiple_workers() -> None:
    """Verify swiftpy start refuses --reload when --workers > 1."""
    cli = CLI()

    with patch("swiftpy.console.commands.start.uvicorn.run") as mock_run:
        with pytest.raises(SystemExit) as exc_info:
            cli.run(["start", "--reload", "--workers", "2"])
        assert exc_info.value.code == 1
        mock_run.assert_not_called()


def test_start_command_env_file_option() -> None:
    """Verify --env-file passes path to uvicorn."""
    cli = CLI()

    with patch("swiftpy.console.commands.start.uvicorn.run") as mock_run:
        cli.run(["start", "--env-file", ".env.local"])
        mock_run.assert_called_once_with(
            "main:app",
            host="127.0.0.1",
            port=8000,
            reload=False,
            env_file=".env.local",
        )


def test_start_command_config_defaults() -> None:
    """Verify defaults are loaded from Config when CLI arguments are omitted."""
    cli = CLI()
    cfg = Config({
        "server": {
            "target": "custom:asgi",
            "host": "192.168.1.10",
            "port": 7777,
        }
    })

    with (
        patch("swiftpy.console.commands.start.Config.load", return_value=cfg),
        patch("swiftpy.console.commands.start.uvicorn.run") as mock_run,
    ):
        cli.run(["start"])
        mock_run.assert_called_once_with(
            "custom:asgi",
            host="192.168.1.10",
            port=7777,
            reload=False,
        )


def test_cli_help_snapshot_no_placeholder_commands(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify swiftpy --help displays only real commands and does not expose placeholder ai commands."""
    cli = CLI()
    with pytest.raises(SystemExit) as exc_info:
        cli.run(["--help"])
    assert exc_info.value.code == 0

    captured = capsys.readouterr().out
    assert "start" in captured
    assert "migrate" in captured
    assert "make:model" in captured
    assert "queue:work" in captured

    # Decision D-11: ai placeholders must not be exposed as broken commands
    assert " ai " not in captured
    assert "make:ai" not in captured

