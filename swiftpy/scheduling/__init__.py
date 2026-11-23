from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swiftpy.scheduling.cron import (
        CronExpression,
        normalize_schedule_datetime,
        parse_cron,
        resolve_timezone,
    )
    from swiftpy.scheduling.events import (
        ScheduledTaskCompleted,
        ScheduledTaskDispatched,
        ScheduledTaskDue,
        ScheduledTaskFailed,
        ScheduledTaskSkipped,
        ScheduleRegistered,
        dispatch_scheduler_event,
    )
    from swiftpy.scheduling.exceptions import (
        DuplicateScheduleError,
        InvalidCronError,
        InvalidScheduleError,
        InvalidTimezoneError,
        ScheduleError,
        ScheduleNotFoundError,
        ScheduleSignatureError,
    )
    from swiftpy.scheduling.jobs import ScheduledCallableJob
    from swiftpy.scheduling.models import (
        TERMINAL_OCCURRENCE_STATUSES,
        MisfirePolicy,
        OccurrenceStatus,
        OverlapPolicy,
        ScheduleOccurrence,
        ScheduleRecord,
        TriggerType,
    )
    from swiftpy.scheduling.scheduler import (
        IntervalScheduleBuilder,
        Schedule,
        ScheduleBuilder,
        Scheduler,
        scheduler,
    )
    from swiftpy.scheduling.store import (
        ClaimOutcome,
        MemoryScheduleStore,
        RedisScheduleStore,
        ScheduleStore,
    )

_EXPORT_MAP: dict[str, str] = {
    "ClaimOutcome": "swiftpy.scheduling.store",
    "CronExpression": "swiftpy.scheduling.cron",
    "DuplicateScheduleError": "swiftpy.scheduling.exceptions",
    "IntervalScheduleBuilder": "swiftpy.scheduling.scheduler",
    "InvalidCronError": "swiftpy.scheduling.exceptions",
    "InvalidScheduleError": "swiftpy.scheduling.exceptions",
    "InvalidTimezoneError": "swiftpy.scheduling.exceptions",
    "MemoryScheduleStore": "swiftpy.scheduling.store",
    "MisfirePolicy": "swiftpy.scheduling.models",
    "OccurrenceStatus": "swiftpy.scheduling.models",
    "OverlapPolicy": "swiftpy.scheduling.models",
    "RedisScheduleStore": "swiftpy.scheduling.store",
    "Schedule": "swiftpy.scheduling.scheduler",
    "ScheduleBuilder": "swiftpy.scheduling.scheduler",
    "ScheduleError": "swiftpy.scheduling.exceptions",
    "ScheduleNotFoundError": "swiftpy.scheduling.exceptions",
    "ScheduleSignatureError": "swiftpy.scheduling.exceptions",
    "ScheduleOccurrence": "swiftpy.scheduling.models",
    "ScheduleRecord": "swiftpy.scheduling.models",
    "ScheduleRegistered": "swiftpy.scheduling.events",
    "ScheduleStore": "swiftpy.scheduling.store",
    "ScheduledCallableJob": "swiftpy.scheduling.jobs",
    "ScheduledTaskCompleted": "swiftpy.scheduling.events",
    "ScheduledTaskDispatched": "swiftpy.scheduling.events",
    "ScheduledTaskDue": "swiftpy.scheduling.events",
    "ScheduledTaskFailed": "swiftpy.scheduling.events",
    "ScheduledTaskSkipped": "swiftpy.scheduling.events",
    "Scheduler": "swiftpy.scheduling.scheduler",
    "TERMINAL_OCCURRENCE_STATUSES": "swiftpy.scheduling.models",
    "TriggerType": "swiftpy.scheduling.models",
    "dispatch_scheduler_event": "swiftpy.scheduling.events",
    "normalize_schedule_datetime": "swiftpy.scheduling.cron",
    "parse_cron": "swiftpy.scheduling.cron",
    "resolve_timezone": "swiftpy.scheduling.cron",
    "scheduler": "swiftpy.scheduling.scheduler",
}


def __getattr__(name: str) -> object:
    """Lazily resolve scheduling exports to avoid import-order cycles with queue."""
    module_name = _EXPORT_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name)
    value: object = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "TERMINAL_OCCURRENCE_STATUSES",
    "ClaimOutcome",
    "CronExpression",
    "DuplicateScheduleError",
    "IntervalScheduleBuilder",
    "InvalidCronError",
    "InvalidScheduleError",
    "InvalidTimezoneError",
    "MemoryScheduleStore",
    "MisfirePolicy",
    "OccurrenceStatus",
    "OverlapPolicy",
    "RedisScheduleStore",
    "Schedule",
    "ScheduleBuilder",
    "ScheduleError",
    "ScheduleNotFoundError",
    "ScheduleOccurrence",
    "ScheduleRecord",
    "ScheduleRegistered",
    "ScheduleSignatureError",
    "ScheduleStore",
    "ScheduledCallableJob",
    "ScheduledTaskCompleted",
    "ScheduledTaskDispatched",
    "ScheduledTaskDue",
    "ScheduledTaskFailed",
    "ScheduledTaskSkipped",
    "Scheduler",
    "TriggerType",
    "dispatch_scheduler_event",
    "normalize_schedule_datetime",
    "parse_cron",
    "resolve_timezone",
    "scheduler",
]
