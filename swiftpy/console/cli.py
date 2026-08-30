from __future__ import annotations

import argparse

from swiftpy.console.commands.start import StartCommand

# from swiftpy.console.commands.migrate import MigrateCommand
# from swiftpy.console.commands.make import MakeCommand
# from swiftpy.console.commands.ai import AICommand
# from swiftpy.console.commands.cache import CacheCommand


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
        """
        Register all framework commands.
        """

        StartCommand.register(self.subparsers)
        # MigrateCommand.register(self.subparsers)
        # MakeCommand.register(self.subparsers)
        # AICommand.register(self.subparsers)
        # CacheCommand.register(self.subparsers)

    def run(self) -> None:
        """
        Parse arguments and dispatch.
        """

        args = self.parser.parse_args()

        handler = getattr(args, "_handler", None)

        if handler is None:
            self.parser.print_help()
            return

        handler(args)


def main() -> None:
    CLI().run()


if __name__ == "__main__":
    main()
