from __future__ import annotations

import argparse

import uvicorn


class StartCommand:
    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        parser = subparsers.add_parser(
            "start",
            help="Boot the application",
        )

        parser.add_argument(
            "target",
            nargs="?",
            default="main:app",
            help="Application target",
        )

        parser.set_defaults(
            _handler=StartCommand.handle,
        )

    @staticmethod
    def handle(args: argparse.Namespace) -> None:
        uvicorn.run(
            args.target,
            host="127.0.0.1",
            port=8888,
            reload=True,
        )
