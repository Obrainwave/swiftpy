from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.scheduling.models import ScheduleOccurrence, ScheduleRecord


@dataclass(frozen=True, slots=True)
class ScheduleRegistered:
    """Dispatched when a schedule definition is created or updated."""

    schedule: ScheduleRecord
    upserted: bool = False


@dataclass(frozen=True, slots=True)
class ScheduledTaskDue:
    """Dispatched when a schedule reaches its due window for claiming."""

    schedule: ScheduleRecord
    scheduled_for: datetime


@dataclass(frozen=True, slots=True)
class ScheduledTaskDispatched:
    """Dispatched after a claimed occurrence is confirmed on the queue."""

    schedule: ScheduleRecord
    occurrence: ScheduleOccurrence


@dataclass(frozen=True, slots=True)
class ScheduledTaskSkipped:
    """Dispatched when an occurrence is skipped due to overlap or misfire policy."""

    schedule: ScheduleRecord
    reason: str
    occurrence: ScheduleOccurrence | None = None


@dataclass(frozen=True, slots=True)
class ScheduledTaskCompleted:
    """Dispatched when a scheduled job completes execution."""

    occurrence: ScheduleOccurrence


@dataclass(frozen=True, slots=True)
class ScheduledTaskFailed:
    """Dispatched when scheduling dispatch fails or a worker job exhausts retries."""

    occurrence: ScheduleOccurrence
    error: str


_EVENT_NAME_MAP: dict[type[Any], str] = {
    ScheduleRegistered: "scheduler.registered",
    ScheduledTaskDue: "scheduler.due",
    ScheduledTaskDispatched: "scheduler.dispatched",
    ScheduledTaskSkipped: "scheduler.skipped",
    ScheduledTaskCompleted: "scheduler.completed",
    ScheduledTaskFailed: "scheduler.failed",
}


async def dispatch_scheduler_event(event: Any) -> None:
    """Dispatch both the typed scheduler event and its ``scheduler.<name>`` alias."""
    dispatcher = EventDispatcher.get_instance()
    await dispatcher.dispatch(event)
    alias = _EVENT_NAME_MAP.get(type(event))
    if alias is not None and dispatcher.has_listeners(alias):
        await dispatcher.dispatch(alias, event)


__all__ = [
    "ScheduleRegistered",
    "ScheduledTaskCompleted",
    "ScheduledTaskDispatched",
    "ScheduledTaskDue",
    "ScheduledTaskFailed",
    "ScheduledTaskSkipped",
    "dispatch_scheduler_event",
]
