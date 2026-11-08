from __future__ import annotations

import argparse
import sys
from typing import Any

import uvicorn

from swiftpy.core.config import Config


class StartCommand:
    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        parser = subparsers.add_parser(
            "start",
            help="Boot the application server",
        )

        parser.add_argument(
            "target",
            nargs="?",
            default=None,
            help="Application ASGI target (default: from config or 'main:app')",
        )

        parser.add_argument(
            "--host",
            dest="host",
            default=None,
            help="Bind socket to this host (default: from config or '127.0.0.1')",
        )

        parser.add_argument(
            "--port",
            dest="port",
            type=int,
            default=None,
            help="Bind socket to this port (default: from config or 8000)",
        )

        parser.add_argument(
            "--workers",
            dest="workers",
            type=int,
            default=None,
            help="Number of worker processes (default: 1)",
        )

        parser.add_argument(
            "--reload",
            dest="reload",
            action="store_true",
            default=False,
            help="Enable auto-reload on code change (default: False)",
        )

        parser.add_argument(
            "--env-file",
            dest="env_file",
            default=None,
            help="Environment configuration file path (e.g. '.env')",
        )

        parser.add_argument(
            "--access-log",
            dest="access_log",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Enable or disable uvicorn access log",
        )

        parser.add_argument(
            "--log-level",
            dest="log_level",
            default=None,
            help="Logging level (critical, error, warning, info, debug, trace)",
        )

        parser.set_defaults(
            _handler=StartCommand.handle,
        )

    @staticmethod
    def handle(args: argparse.Namespace) -> None:
        try:
            config = Config.load()
        except Exception:  # noqa: BLE001
            config = Config()

        target = (
            args.target
            or config.get("server.target")
            or config.get("app.target")
            or "main:app"
        )
        host = (
            args.host
            or config.get("server.host")
            or config.get("app.host")
            or "127.0.0.1"
        )
        raw_port = (
            args.port
            or config.get("server.port")
            or config.get("app.port")
            or 8000
        )
        port = int(raw_port)
        raw_workers = args.workers or config.get("server.workers") or 1
        workers = int(raw_workers)
        reload = bool(args.reload)
        env_file = args.env_file or config.get("server.env_file")

        if reload and workers > 1:
            sys.stderr.write(
                "Error: --reload cannot be used with multiple workers (--workers > 1).\n"
            )
            raise SystemExit(1)

        uvicorn_kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "reload": reload,
        }
        if workers > 1:
            uvicorn_kwargs["workers"] = workers
        if env_file:
            uvicorn_kwargs["env_file"] = env_file

        access_log_opt = (
            args.access_log
            if getattr(args, "access_log", None) is not None
            else config.get("server.access_log")
        )
        if access_log_opt is not None:
            uvicorn_kwargs["access_log"] = bool(access_log_opt)

        log_level_opt = getattr(args, "log_level", None) or config.get("server.log_level")
        if log_level_opt:
            uvicorn_kwargs["log_level"] = str(log_level_opt).lower()

        uvicorn.run(target, **uvicorn_kwargs)


__all__ = ["StartCommand"]
