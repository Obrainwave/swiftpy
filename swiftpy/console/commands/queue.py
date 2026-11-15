from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from swiftpy.console.loader import load_application
from swiftpy.core.config import ConfigurationError
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.worker import QueueWorker

if TYPE_CHECKING:
    from swiftpy.core.application import Application

logger = logging.getLogger("swiftpy.console")


def _load_optional_app(app_target: str | None) -> Application | None:
    """Load the optional `module:attribute` application target or exit on failure."""
    if not app_target:
        return None
    try:
        return load_application(app_target)
    except (
        ImportError,
        AttributeError,
        TypeError,
        ConfigurationError,
        OSError,
        ValueError,
        RuntimeError,
        SyntaxError,
    ) as exc:
        logger.error("Could not boot application target '%s': %s", app_target, exc)
        raise SystemExit(1) from exc


def _run_queue_action(
    action: Callable[[], Awaitable[None]],
    *,
    app: Application | None = None,
    temporary_dispatcher: QueueDispatcher | None = None,
) -> None:
    """Execute an async queue CLI action within optional app lifespan and dispatcher cleanup."""

    async def _execute() -> None:
        if app is not None:
            await app.startup()
        try:
            await action()
        finally:
            if temporary_dispatcher is not None:
                await temporary_dispatcher.close()
            if app is not None:
                await app.shutdown()

    _run_coroutine(_execute())


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
            "--concurrency",
            type=int,
            default=1,
            help="Number of concurrent worker tasks to process jobs simultaneously",
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

        # queue:failed
        failed_parser = subparsers.add_parser(
            "queue:failed",
            help="List failed jobs recorded in the Dead Letter Queue",
        )
        failed_parser.add_argument(
            "--queue",
            default="default",
            help="The queue to inspect",
        )
        failed_parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum number of failed records to list",
        )
        failed_parser.add_argument(
            "--prefix",
            default=None,
            help="The Redis key prefix for queues",
        )
        failed_parser.add_argument(
            "--app",
            default=None,
            help="Application target (module:attribute) to boot before running command",
        )
        failed_parser.set_defaults(
            _handler=QueueCommand.handle_failed,
        )

        # queue:retry
        retry_parser = subparsers.add_parser(
            "queue:retry",
            help="Retry failed jobs by pushing them back to the ready queue",
        )
        retry_parser.add_argument(
            "id",
            help="The ID of the failed job to retry, or 'all' to retry all failed jobs",
        )
        retry_parser.add_argument(
            "--queue",
            default="default",
            help="The queue to retry failed jobs on",
        )
        retry_parser.add_argument(
            "--prefix",
            default=None,
            help="The Redis key prefix for queues",
        )
        retry_parser.add_argument(
            "--app",
            default=None,
            help="Application target (module:attribute) to boot before running command",
        )
        retry_parser.set_defaults(
            _handler=QueueCommand.handle_retry,
        )

        # queue:flush
        flush_parser = subparsers.add_parser(
            "queue:flush",
            help="Flush all failed jobs from the Dead Letter Queue",
        )
        flush_parser.add_argument(
            "--queue",
            default="default",
            help="The queue to flush failed jobs from",
        )
        flush_parser.add_argument(
            "--prefix",
            default=None,
            help="The Redis key prefix for queues",
        )
        flush_parser.add_argument(
            "--app",
            default=None,
            help="Application target (module:attribute) to boot before running command",
        )
        flush_parser.set_defaults(
            _handler=QueueCommand.handle_flush,
        )

    @staticmethod
    def handle_work(args: argparse.Namespace) -> None:
        """Run the queue worker process."""
        app = _load_optional_app(getattr(args, "app", None))
        temporary_dispatcher = QueueDispatcher(prefix=args.prefix) if args.prefix else None
        worker = QueueWorker(
            queues=args.queue,
            dispatcher=temporary_dispatcher,
            sleep=args.sleep,
            max_jobs=args.max_jobs,
            stop_when_empty=args.stop_when_empty,
            timeout=args.timeout,
            concurrency=getattr(args, "concurrency", 1),
        )
        _run_queue_action(
            worker.work,
            app=app,
            temporary_dispatcher=temporary_dispatcher,
        )

    @staticmethod
    def handle_failed(args: argparse.Namespace) -> None:
        """List failed jobs from the Dead Letter Queue."""
        if args.limit < 1 or args.limit > 1000:
            raise SystemExit(
                f"Invalid --limit {args.limit}: must be between 1 and 1000."
            )

        app = _load_optional_app(getattr(args, "app", None))
        temporary_dispatcher = QueueDispatcher(prefix=args.prefix) if args.prefix else None
        dispatcher = temporary_dispatcher or QueueDispatcher.get_instance()

        async def _list() -> None:
            records = await dispatcher.get_failed_jobs(
                queue=args.queue, start=0, stop=args.limit - 1
            )
            if not records:
                print(f"No failed jobs found for queue '{args.queue}'.")
                return

            print(f"Failed jobs for queue '{args.queue}' ({len(records)}):")
            for rec in records:
                job_id = rec.get("job_id", "unknown")
                job_class = rec.get("job_class", "unknown")
                exc_info = rec.get("exception", {})
                exc_msg = exc_info.get("message", "unknown error")
                failed_at = rec.get("failed_at", 0)
                print(f"  [{job_id}] {job_class} - {exc_msg} (failed at {failed_at})")

        _run_queue_action(
            _list,
            app=app,
            temporary_dispatcher=temporary_dispatcher,
        )

    @staticmethod
    def handle_retry(args: argparse.Namespace) -> None:
        """Retry one or all failed jobs."""
        app = _load_optional_app(getattr(args, "app", None))
        temporary_dispatcher = QueueDispatcher(prefix=args.prefix) if args.prefix else None
        dispatcher = temporary_dispatcher or QueueDispatcher.get_instance()

        async def _retry() -> None:
            target_id = None if args.id == "all" else args.id
            retried = await dispatcher.retry_failed(
                queue=args.queue, job_id=target_id
            )
            print(f"Retried {retried} failed job(s) on queue '{args.queue}'.")

        _run_queue_action(
            _retry,
            app=app,
            temporary_dispatcher=temporary_dispatcher,
        )

    @staticmethod
    def handle_flush(args: argparse.Namespace) -> None:
        """Flush failed jobs from the Dead Letter Queue."""
        app = _load_optional_app(getattr(args, "app", None))
        temporary_dispatcher = QueueDispatcher(prefix=args.prefix) if args.prefix else None
        dispatcher = temporary_dispatcher or QueueDispatcher.get_instance()

        async def _flush() -> None:
            cleared = await dispatcher.flush_failed(queue=args.queue)
            print(f"Flushed {cleared} failed job(s) from queue '{args.queue}'.")

        _run_queue_action(
            _flush,
            app=app,
            temporary_dispatcher=temporary_dispatcher,
        )
