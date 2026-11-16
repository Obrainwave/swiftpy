from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from swiftpy.console.cli import CLI
from swiftpy.core.config import Config
from swiftpy.core.container import Container, bind_container


@contextmanager
def _bound_config(cfg: Config) -> Iterator[None]:
    container = Container()
    container.instance(Config, cfg)
    with bind_container(container):
        yield


def test_storage_link_creates_portable_relative_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = Config({"filesystems": {"disks": {"public": {"root": "storage/app/public"}}}})

    with _bound_config(cfg):
        CLI().run(["storage:link"])

    link_path = tmp_path / "public" / "storage"
    target_path = tmp_path / "storage" / "app" / "public"

    assert link_path.is_symlink()
    assert target_path.is_dir()
    assert os.readlink(link_path) == str(Path("..") / "storage" / "app" / "public")
    assert link_path.resolve() == target_path.resolve()

    out = capsys.readouterr().out
    assert "public/storage" in out
    assert "storage/app/public" in out


def test_storage_link_existing_and_force_preserves_target_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = Config({"filesystems": {"disks": {"public": {"root": "storage/app/public"}}}})

    with _bound_config(cfg):
        CLI().run(["storage:link"])

        uploaded_file = tmp_path / "storage" / "app" / "public" / "avatar.txt"
        uploaded_file.write_text("keep-me", encoding="utf-8")

        # Without --force: preserves symlink and warns cleanly
        CLI().run(["storage:link"])
        assert "already exists" in capsys.readouterr().out
        assert uploaded_file.read_text(encoding="utf-8") == "keep-me"

        # With --force: unlinks ONLY public/storage symlink, never deleting storage/app/public files
        CLI().run(["storage:link", "--force"])
        assert "has been connected" in capsys.readouterr().out

    link_path = tmp_path / "public" / "storage"
    assert link_path.is_symlink()
    assert uploaded_file.read_text(encoding="utf-8") == "keep-me"
    assert (link_path / "avatar.txt").read_text(encoding="utf-8") == "keep-me"


def test_storage_link_falls_back_to_absolute_on_cross_drive_relpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = Config({"filesystems": {"disks": {"public": {"root": "storage/app/public"}}}})
    expected_target = (tmp_path / "storage" / "app" / "public").resolve()

    def raise_cross_drive(_target: object, start: object = None) -> str:
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", raise_cross_drive)

    with _bound_config(cfg):
        CLI().run(["storage:link"])

    link_path = tmp_path / "public" / "storage"
    assert link_path.is_symlink()
    assert os.readlink(link_path) == str(expected_target)
