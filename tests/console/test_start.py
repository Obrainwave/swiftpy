from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from swiftpy.console.cli import CLI
from swiftpy.console.loader import ensure_project_in_sys_path
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


def test_ensure_project_in_sys_path_prepends_and_promotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_project_in_sys_path places cwd at sys.path[0] and removes duplicates."""
    other = tmp_path / "other"
    proj = tmp_path / "proj"
    other.mkdir()
    proj.mkdir()

    monkeypatch.chdir(proj)
    monkeypatch.setattr(sys, "path", [str(other), str(proj), str(proj)])

    resolved = ensure_project_in_sys_path()
    assert resolved == str(proj)
    assert sys.path[0] == str(proj)
    assert sys.path.count(str(proj)) == 1


def test_start_command_prepends_cwd_before_uvicorn_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """StartCommand puts the project root at sys.path[0] prior to invoking uvicorn.run."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(tmp_path)])

    observed_sys_path: list[str] = []

    def fake_run(target: str, **kwargs: object) -> None:
        observed_sys_path.extend(sys.path)

    cli = CLI()
    with patch("swiftpy.console.commands.start.uvicorn.run", side_effect=fake_run) as mock_run:
        cli.run(["start", "--reload"])

    mock_run.assert_called_once_with(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
    assert observed_sys_path[0] == str(tmp_path)


def test_start_command_invalid_args_do_not_mutate_sys_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid flag combinations exit before mutating sys.path or launching uvicorn."""
    monkeypatch.chdir(tmp_path)
    original_path = [p for p in sys.path if p != str(tmp_path)]
    monkeypatch.setattr(sys, "path", list(original_path))

    cli = CLI()
    with pytest.raises(SystemExit) as exc_info:
        cli.run(["start", "--reload", "--workers", "2"])

    assert exc_info.value.code == 1
    assert str(tmp_path) not in sys.path


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize("reload_enabled", [False, True])
def test_start_command_console_entrypoint_imports_project_main_under_wheel_layout(
    tmp_path: Path, reload_enabled: bool
) -> None:
    """
    Simulate an installed wheel console script where cwd is NOT on sys.path
    and a competing directory on sys.path has a different main.py.
    Both `swiftpy start` and `swiftpy start --reload` must import the cwd project's main:app.
    """
    repo_root = Path(__file__).resolve().parents[2]
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "swiftpy").symlink_to(repo_root / "swiftpy", target_is_directory=True)

    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()
    (shadow_dir / "main.py").write_text(
        'raise RuntimeError("Imported wrong main.py from shadow_dir!")\n',
        encoding="utf-8",
    )

    project_dir = tmp_path / "my-api"
    (project_dir / "app").mkdir(parents=True)
    (project_dir / "app" / "greeting.py").write_text(
        'MESSAGE = "from-project-app"\n',
        encoding="utf-8",
    )
    (project_dir / "main.py").write_text(
        """from __future__ import annotations

from app.greeting import MESSAGE
from swiftpy.core.application import Application
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response

app = Application(auto_discover_providers=False)


@app.get("/health")
async def health(request: Request) -> Response:
    return json_response({"status": "ok", "source": MESSAGE})
""",
        encoding="utf-8",
    )

    launcher = tmp_path / "swiftpy_console.py"
    launcher.write_text(
        f"""import sys

if __name__ == "__main__":
    sys.path = [p for p in sys.path if p not in ({str(repo_root)!r}, {str(tmp_path)!r}, "")]
    sys.path.insert(1, {str(site_dir)!r})
    sys.path.append({str(shadow_dir)!r})
    from swiftpy.console.cli import main

    sys.exit(main())
""",
        encoding="utf-8",
    )

    port = _reserve_free_port()
    cmd = [sys.executable, str(launcher), "start", "--host", "127.0.0.1", "--port", str(port)]
    if reload_enabled:
        cmd.append("--reload")

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.Popen(
        cmd,
        cwd=project_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    response_json: dict[str, object] | None = None
    deadline = time.monotonic() + 10.0
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
                if resp.status_code == 200:
                    response_json = resp.json()
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
    finally:
        if proc.poll() is None:
            if os.name == "nt":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGINT)
            try:
                output, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate(timeout=5)
        else:
            output, _ = proc.communicate(timeout=5)

    assert response_json == {"status": "ok", "source": "from-project-app"}, output
    assert 'Could not import module "main"' not in output

