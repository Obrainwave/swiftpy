from __future__ import annotations

import argparse
import sys

from swiftpy.console.commands.cache import CacheCommand
from swiftpy.console.commands.make import MakeCommand
from swiftpy.console.commands.migrate import MigrateCommand
from swiftpy.console.commands.new import NewCommand
from swiftpy.console.commands.queue import QueueCommand
from swiftpy.console.commands.seed import SeedCommand
from swiftpy.console.commands.start import StartCommand
from swiftpy.console.commands.storage import StorageCommand


class CLI:
    """
    SwiftPY command line entrypoint.

    Responsible for:
    - Parsing arguments
    - Dispatching commands
    - Delegating execution
    """

    def __init__(self) -> None:
        self.parser = argparse.ArgumentParser(
            prog="swiftpy",
            description="SwiftPY Framework CLI",
        )

        self.subparsers = self.parser.add_subparsers(
            dest="command",
            required=True,
        )

        self._register_commands()

    def _register_commands(self) -> None:
        """Register all framework commands."""
        NewCommand.register(self.subparsers)
        StartCommand.register(self.subparsers)
        MigrateCommand.register(self.subparsers)
        SeedCommand.register(self.subparsers)
        MakeCommand.register(self.subparsers)
        QueueCommand.register(self.subparsers)
        CacheCommand.register(self.subparsers)
        StorageCommand.register(self.subparsers)

    def run(self, args: list[str] | None = None) -> None:
        """Parse arguments and dispatch."""
        parsed_args = self.parser.parse_args(args)

        handler = getattr(parsed_args, "_handler", None)

        if handler is None:
            self.parser.print_help()
            return

        handler(parsed_args)


def main() -> None:
    CLI().run(sys.argv[1:])


if __name__ == "__main__":
    main()
