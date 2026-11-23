from __future__ import annotations

import argparse
import json
import math
from typing import TYPE_CHECKING

from swiftpy.console.loader import load_optional_app, run_app_action
from swiftpy.core.config import Config
from swiftpy.scheduling.models import OccurrenceStatus, ScheduleRecord, TriggerType
from swiftpy.scheduling.scheduler import Scheduler
from swiftpy.scheduling.store import RedisScheduleStore

if TYPE_CHECKING:
    from swiftpy.core.application import Application


def _format_cadence(record: ScheduleRecord) -> str:
    if record.trigger == TriggerType.CRON:
        return record.expression or "<cron>"
    if record.trigger == TriggerType.INTERVAL:
        return f"every {record.interval_seconds}s"
    if record.run_at is not None:
        return f"once @{record.run_at.isoformat()}"
    return "once"


def _resolve_cli_scheduler(
    app: Application | None,
    prefix: str | None,
) -> tuple[Scheduler, bool]:
    cfg = app.config if app is not None else Config.get_instance()
    if prefix:
        temp_store = RedisScheduleStore(prefix=prefix)
        return Scheduler.from_config(cfg, store=temp_store), True
    if app is not None:
        return Scheduler.get_instance(), False
    return Scheduler.from_config(cfg), True


class ScheduleCommand:
    """Console commands for inspecting and running SwiftPY scheduled tasks."""

    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        list_parser = subparsers.add_parser(
            "schedule:list",
            help="List registered schedules and their next/last execution state",
        )
        list_parser.add_argument(
            "--app",
            default=None,
            help="Application target (module:attribute) to boot before listing schedules",
        )
        list_parser.add_argument(
            "--json",
            action="store_true",
            help="Emit JSON output instead of a human-readable table",
        )
        list_parser.add_argument(
            "--prefix",
            default=None,
            help="The Redis key prefix for schedules",
        )
        list_parser.set_defaults(_handler=ScheduleCommand.handle_list)

        run_parser = subparsers.add_parser(
            "schedule:run",
            help="Recover pending dispatches and run one due scheduler tick",
        )
        run_parser.add_argument(
            "--app",
            default=None,
            help="Application target (module:attribute) to boot before ticking",
        )
        run_parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum number of due schedules to process in one tick (1..1000)",
        )
        run_parser.add_argument(
            "--prefix",
            default=None,
            help="The Redis key prefix for schedules",
        )
        run_parser.set_defaults(_handler=ScheduleCommand.handle_run)

        work_parser = subparsers.add_parser(
            "schedule:work",
            help="Start the foreground scheduler loop",
        )
        work_parser.add_argument(
            "--app",
            default=None,
            help="Application target (module:attribute) to boot before running loop",
        )
        work_parser.add_argument(
            "--sleep",
            type=float,
            default=1.0,
            help="Maximum interval in seconds between scheduler wakeups",
        )
        work_parser.add_argument(
            "--max-ticks",
            type=int,
            default=None,
            help="Maximum number of ticks to execute before exiting",
        )
        work_parser.add_argument(
            "--prefix",
            default=None,
            help="The Redis key prefix for schedules",
        )
        work_parser.set_defaults(_handler=ScheduleCommand.handle_work)

    @staticmethod
    def handle_list(args: argparse.Namespace) -> None:
        """List all persisted schedules."""
        app = load_optional_app(getattr(args, "app", None))
        prefix = getattr(args, "prefix", None)
        emit_json = bool(getattr(args, "json", False))

        temp_sched: Scheduler | None = None

        async def _list() -> None:
            nonlocal temp_sched
            sched, owns_sched = _resolve_cli_scheduler(app, prefix)
            if owns_sched:
                temp_sched = sched
            records = await sched.list_schedules()
            if emit_json:
                print(json.dumps([r.to_dict() for r in records], indent=2))
                return
            if not records:
                print("No scheduled tasks registered.")
                return
            print(f"Registered schedules ({len(records)}):")
            for rec in records:
                label = rec.name or rec.id
                cadence = _format_cadence(rec)
                state = "enabled" if rec.enabled else "disabled"
                next_run = (
                    rec.next_run_at.isoformat() if rec.next_run_at is not None else "-"
                )
                last_status = (
                    rec.last_status.value if rec.last_status is not None else "-"
                )
                print(
                    f"  [{label}] {cadence} tz={rec.timezone} state={state} "
                    f"queue={rec.queue} next={next_run} last_status={last_status}"
                )

        async def _cleanup() -> None:
            if temp_sched is not None:
                await temp_sched.close()

        run_app_action(_list, app=app, cleanup=_cleanup)

    @staticmethod
    def handle_run(args: argparse.Namespace) -> None:
        """Execute pending dispatch recovery and a single due evaluation tick."""
        limit = int(getattr(args, "limit", 50))
        if limit < 1 or limit > 1000:
            raise SystemExit(f"Invalid --limit {limit}: must be between 1 and 1000.")

        app = load_optional_app(getattr(args, "app", None))
        prefix = getattr(args, "prefix", None)
        temp_sched: Scheduler | None = None

        async def _run() -> None:
            nonlocal temp_sched
            sched, owns_sched = _resolve_cli_scheduler(app, prefix)
            if owns_sched:
                temp_sched = sched
            outcomes = await sched.tick(limit=limit)
            dispatched = sum(
                1 for o in outcomes if o.status == OccurrenceStatus.DISPATCHED
            )
            skipped = sum(1 for o in outcomes if o.status == OccurrenceStatus.SKIPPED)
            failed = sum(1 for o in outcomes if o.status == OccurrenceStatus.FAILED)
            print(
                f"Scheduler tick processed {len(outcomes)} occurrence(s): "
                f"dispatched={dispatched}, skipped={skipped}, failed={failed}."
            )

        async def _cleanup() -> None:
            if temp_sched is not None:
                await temp_sched.close()

        run_app_action(_run, app=app, cleanup=_cleanup)

    @staticmethod
    def handle_work(args: argparse.Namespace) -> None:
        """Run the foreground scheduler daemon loop until stopped or --max-ticks reached."""
        sleep_val = float(getattr(args, "sleep", 1.0))
        if not math.isfinite(sleep_val) or sleep_val <= 0:
            raise SystemExit(f"Invalid --sleep {sleep_val}: must be > 0 and finite.")

        max_ticks = getattr(args, "max_ticks", None)
        if max_ticks is not None and max_ticks < 0:
            raise SystemExit(
                f"Invalid --max-ticks {max_ticks}: must be a non-negative integer."
            )

        app = load_optional_app(getattr(args, "app", None))
        prefix = getattr(args, "prefix", None)
        temp_sched: Scheduler | None = None

        async def _work() -> None:
            nonlocal temp_sched
            sched, owns_sched = _resolve_cli_scheduler(app, prefix)
            if owns_sched:
                temp_sched = sched
            executed = await sched.work(sleep=sleep_val, max_ticks=max_ticks)
            print(f"Scheduler worker stopped after {executed} tick(s).")

        async def _cleanup() -> None:
            if temp_sched is not None:
                await temp_sched.close()

        run_app_action(_work, app=app, cleanup=_cleanup)


__all__ = ["ScheduleCommand"]
