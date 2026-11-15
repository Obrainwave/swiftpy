from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from swiftpy.console.commands.storage import StorageCommand
from swiftpy.core.config import Config


def test_storage_link_command() -> None:
    with tempfile.TemporaryDirectory() as base_dir:
        cwd = Path(base_dir).resolve()
        with patch("pathlib.Path.cwd", return_value=cwd), patch("os.getcwd", return_value=str(cwd)):
            cfg = Config({
                "filesystems": {
                    "disks": {
                        "public": {
                            "root": str(cwd / "storage" / "app" / "public"),
                        }
                    }
                }
            })
            with patch.object(Config, "get_instance", return_value=cfg):
                args = argparse.Namespace(force=False)
                StorageCommand.handle_link(args)

                link_path = cwd / "public" / "storage"
                target_path = cwd / "storage" / "app" / "public"

                assert link_path.is_symlink()
                assert target_path.exists()
                assert os.path.samefile(link_path, target_path)

                # Running again without force reports already exists without raising
                StorageCommand.handle_link(args)

                # Running with --force re-links cleanly
                args_force = argparse.Namespace(force=True)
                StorageCommand.handle_link(args_force)
                assert link_path.is_symlink()

