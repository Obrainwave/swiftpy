from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
from typing import Any

from swiftpy.console.loader import load_application
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.worker import QueueWorker

logger = logging.getLogger("swiftpy.console")


def _run_coroutine(coro: Any) -> Any:
    """Run an async coroutine synchronously, safely handling existing active event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


class QueueCommand:
    """Console commands for managing and executing queue workers."""

    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        parser = subparsers.add_parser(
            "queue:work",
            help="Start processing jobs on the queue as a worker process",
        )
        parser.add_argument(
            "--queue",
            default="default",
            help="The names of the queues to work (comma-separated)",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=1.0,
            help="Number of seconds to sleep when no job is available",
        )
        parser.add_argument(
            "--max-jobs",
            type=int,
            default=None,
            help="The maximum number of jobs that the worker may process before exiting",
        )
        parser.add_argument(
            "--stop-when-empty",
            action="store_true",
            help="Stop processing jobs when all queues are empty",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=None,
            help="The number of seconds a child job can run before timing out",
        )
        parser.add_argument(
            "--prefix",
            default=None,
            help="The Redis key prefix for queues",
        )
        parser.add_argument(
            "--app",
            default=None,
            help="Application target (module:attribute) to boot before processing",
        )

        parser.set_defaults(
            _handler=QueueCommand.handle_work,
        )

    @staticmethod
    def handle_work(args: argparse.Namespace) -> None:
        """Run the queue worker process."""
        if args.app:
            try:
                load_application(args.app)
            except (ImportError, AttributeError, TypeError, ValueError) as exc:
                logger.debug(
                    "Could not boot application target '%s': %s", args.app, exc
                )

        dispatcher = QueueDispatcher(prefix=args.prefix) if args.prefix else None
        worker = QueueWorker(
            queues=args.queue,
            dispatcher=dispatcher,
            sleep=args.sleep,
            max_jobs=args.max_jobs,
            stop_when_empty=args.stop_when_empty,
            timeout=args.timeout,
        )
        _run_coroutine(worker.work())
