from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

import pytest

from swiftpy.console.cli import CLI, _get_framework_version


@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_cli_version_flags(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify `swiftpy -v` and `swiftpy --version` print framework version and exit 0."""
    with patch("swiftpy.console.cli.version", return_value="9.8.7"):
        cli = CLI()
        with pytest.raises(SystemExit) as exc_info:
            cli.run([flag])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "swiftpy 9.8.7"
    assert captured.err == ""


def test_get_framework_version_fallback_to_pyproject() -> None:
    """Fallback reads pyproject.toml when package distribution metadata is absent."""
    with patch(
        "swiftpy.console.cli.version",
        side_effect=PackageNotFoundError("swiftpy-framework"),
    ):
        resolved = _get_framework_version()

    assert resolved
    assert resolved != "unknown"


def test_get_framework_version_returns_unknown_without_metadata_or_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback returns 'unknown' if neither dist metadata nor pyproject.toml exists."""
    fake_cli_file = tmp_path / "pkg" / "swiftpy" / "console" / "cli.py"
    fake_cli_file.parent.mkdir(parents=True)
    fake_cli_file.write_text("", encoding="utf-8")

    monkeypatch.setattr("swiftpy.console.cli.__file__", str(fake_cli_file))
    with patch(
        "swiftpy.console.cli.version",
        side_effect=PackageNotFoundError("swiftpy-framework"),
    ):
        assert _get_framework_version() == "unknown"
