from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from swiftpy.core.config import Config


def _paint(text: str, code: str) -> str:
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        return f"\033[{code}m{text}\033[0m"
    return text


class StorageCommand:
    """Console commands for filesystem and storage management."""

    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        link_parser = subparsers.add_parser(
            "storage:link",
            help="Create the symbolic links configured for the application",
        )
        link_parser.add_argument(
            "--force",
            action="store_true",
            help="Recreate existing symbolic links",
        )
        link_parser.set_defaults(_handler=StorageCommand.handle_link)

        storage_group = subparsers.add_parser(
            "storage",
            help="Manage application storage links and operations",
        )
        storage_sub = storage_group.add_subparsers(
            dest="storage_subcommand",
            required=False,
        )
        sub_link = storage_sub.add_parser(
            "link",
            help="Create the symbolic links configured for the application",
        )
        sub_link.add_argument(
            "--force",
            action="store_true",
            help="Recreate existing symbolic links",
        )
        sub_link.set_defaults(_handler=StorageCommand.handle_link)

    @staticmethod
    def handle_link(args: argparse.Namespace) -> None:
        force = getattr(args, "force", False)
        try:
            cfg = Config.get_instance()
            target_path = Path(
                str(cfg.get("filesystems.disks.public.root", "storage/app/public"))
            ).resolve()
        except (RuntimeError, ValueError, KeyError):
            target_path = Path("storage/app/public").resolve()

        link_path = Path("public/storage").resolve()

        target_path.mkdir(parents=True, exist_ok=True)
        link_path.parent.mkdir(parents=True, exist_ok=True)

        if link_path.is_symlink() or link_path.exists():
            if force:
                if link_path.is_symlink():
                    link_path.unlink()
                elif link_path.is_dir():
                    shutil.rmtree(link_path)
                else:
                    link_path.unlink()
            else:
                msg = _paint(f"The [{link_path.relative_to(Path.cwd())}] link already exists.", "33")
                print(msg)
                return

        try:
            os.symlink(target_path, link_path, target_is_directory=True)
            rel_link = (
                link_path.relative_to(Path.cwd())
                if link_path.is_relative_to(Path.cwd())
                else link_path
            )
            rel_target = (
                target_path.relative_to(Path.cwd())
                if target_path.is_relative_to(Path.cwd())
                else target_path
            )
            msg = _paint(
                f"The [{rel_link}] link has been connected to [{rel_target}].",
                "32",
            )
            print(msg)
        except OSError as exc:
            err = _paint(f"Failed to create storage link: {exc}", "31")
            print(err, file=sys.stderr)
            sys.exit(1)
