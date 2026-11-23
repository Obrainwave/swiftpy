from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from swiftpy.scheduling.exceptions import InvalidScheduleError


class TriggerType(StrEnum):
    """Supported schedule trigger mechanisms."""

    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"


class OverlapPolicy(StrEnum):
    """Concurrency policy when a previous occurrence is still queued or running."""

    ALLOW = "allow"
    SKIP = "skip"


class MisfirePolicy(StrEnum):
    """Catch-up policy when a schedule's due time elapsed while offline."""

    SKIP = "skip"
    FIRE_ONCE = "fire_once"


class OccurrenceStatus(StrEnum):
    """Lifecycle states for an individual scheduled task occurrence."""

    PENDING = "pending"
    CLAIMED = "claimed"
    DISPATCH_PENDING = "dispatch_pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


TERMINAL_OCCURRENCE_STATUSES: frozenset[OccurrenceStatus] = frozenset(
    {
        OccurrenceStatus.COMPLETED,
        OccurrenceStatus.FAILED,
        OccurrenceStatus.SKIPPED,
        OccurrenceStatus.CANCELLED,
    }
)


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(UTC)


def format_utc_datetime(dt: datetime | None) -> str | None:
    """Serialize a datetime to canonical UTC ISO-8601 string."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise InvalidScheduleError(
            "Naive datetime cannot be serialized without explicit timezone resolution."
        )
    return dt.astimezone(UTC).isoformat()


def parse_utc_datetime(raw: Any) -> datetime | None:
    """Deserialize an ISO-8601 string or datetime into a UTC-aware datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raise InvalidScheduleError("Expected timezone-aware datetime.")
        return raw.astimezone(UTC)
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidScheduleError(f"Invalid datetime value: {raw!r}")
    parsed = datetime.fromisoformat(raw.strip())
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def validate_json_arguments(args: list[Any], kwargs: dict[str, Any]) -> None:
    """Ensure task args and kwargs are strictly JSON-serializable at registration time."""
    try:
        json.dumps({"args": args, "kwargs": kwargs}, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidScheduleError(
            f"Scheduled task arguments must be JSON-serializable: {exc}"
        ) from exc


@dataclass(slots=True)
class ScheduleRecord:
    """
    Persistent definition and aggregate runtime metadata for a scheduled task.

    A recurring ScheduleRecord stays enabled across executions; each triggered
    execution is tracked independently by a ``ScheduleOccurrence``.
    """

    id: str
    trigger: TriggerType
    task_name: str
    task_kind: Literal["job", "callable"] = "job"
    name: str | None = None
    expression: str | None = None
    interval_seconds: float | None = None
    run_at: datetime | None = None
    timezone: str = "UTC"
    args: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)
    queue: str = "default"
    enabled: bool = True
    overlap_policy: OverlapPolicy = OverlapPolicy.ALLOW
    misfire_policy: MisfirePolicy = MisfirePolicy.SKIP
    lock_ttl: int | None = None
    grace_seconds: float | None = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_claimed_occurrence: str | None = None
    last_occurrence_id: str | None = None
    last_status: OccurrenceStatus | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this schedule record into a JSON-compatible dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "trigger": self.trigger.value,
            "expression": self.expression,
            "interval_seconds": self.interval_seconds,
            "run_at": format_utc_datetime(self.run_at),
            "timezone": self.timezone,
            "task_name": self.task_name,
            "task_kind": self.task_kind,
            "args": list(self.args),
            "kwargs": dict(self.kwargs),
            "queue": self.queue,
            "enabled": bool(self.enabled),
            "overlap_policy": self.overlap_policy.value,
            "misfire_policy": self.misfire_policy.value,
            "lock_ttl": self.lock_ttl,
            "grace_seconds": self.grace_seconds,
            "next_run_at": format_utc_datetime(self.next_run_at),
            "last_run_at": format_utc_datetime(self.last_run_at),
            "last_claimed_occurrence": self.last_claimed_occurrence,
            "last_occurrence_id": self.last_occurrence_id,
            "last_status": self.last_status.value if self.last_status is not None else None,
            "last_error": self.last_error,
            "created_at": format_utc_datetime(self.created_at),
            "updated_at": format_utc_datetime(self.updated_at),
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Deserialize a ScheduleRecord from a mapping."""
        raw_kind = str(data.get("task_kind", "job"))
        task_kind: Literal["job", "callable"] = (
            "callable" if raw_kind == "callable" else "job"
        )
        raw_status = data.get("last_status")
        raw_args = data.get("args")
        raw_kwargs = data.get("kwargs")
        created = parse_utc_datetime(data.get("created_at")) or utc_now()
        updated = parse_utc_datetime(data.get("updated_at")) or created
        return cls(
            id=str(data["id"]),
            name=str(data["name"]) if data.get("name") is not None else None,
            trigger=TriggerType(str(data["trigger"])),
            expression=(
                str(data["expression"]) if data.get("expression") is not None else None
            ),
            interval_seconds=(
                float(data["interval_seconds"])
                if data.get("interval_seconds") is not None
                else None
            ),
            run_at=parse_utc_datetime(data.get("run_at")),
            timezone=str(data.get("timezone", "UTC")),
            task_name=str(data["task_name"]),
            task_kind=task_kind,
            args=list(raw_args) if isinstance(raw_args, list) else [],
            kwargs=dict(raw_kwargs) if isinstance(raw_kwargs, Mapping) else {},
            queue=str(data.get("queue", "default")),
            enabled=bool(data.get("enabled", True)),
            overlap_policy=OverlapPolicy(str(data.get("overlap_policy", "allow"))),
            misfire_policy=MisfirePolicy(str(data.get("misfire_policy", "skip"))),
            lock_ttl=int(data["lock_ttl"]) if data.get("lock_ttl") is not None else None,
            grace_seconds=(
                float(data["grace_seconds"])
                if data.get("grace_seconds") is not None
                else None
            ),
            next_run_at=parse_utc_datetime(data.get("next_run_at")),
            last_run_at=parse_utc_datetime(data.get("last_run_at")),
            last_claimed_occurrence=(
                str(data["last_claimed_occurrence"])
                if data.get("last_claimed_occurrence") is not None
                else None
            ),
            last_occurrence_id=(
                str(data["last_occurrence_id"])
                if data.get("last_occurrence_id") is not None
                else None
            ),
            last_status=(
                OccurrenceStatus(str(raw_status)) if raw_status is not None else None
            ),
            last_error=(
                str(data["last_error"]) if data.get("last_error") is not None else None
            ),
            created_at=created,
            updated_at=updated,
            signature=str(data["signature"]) if data.get("signature") else None,
        )


@dataclass(slots=True)
class ScheduleOccurrence:
    """
    Individual execution occurrence generated when a schedule becomes due.

    Tracks the crash-recoverable boundary between claiming a due slot and confirming
    submission onto `QueueDispatcher`, as well as worker execution completion.
    """

    occurrence_id: str
    schedule_id: str
    scheduled_for: datetime
    status: OccurrenceStatus
    task_name: str
    task_kind: Literal["job", "callable"] = "job"
    args: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)
    queue: str = "default"
    job_id: str | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    dispatched_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    lease_until: float | None = None
    dispatch_attempts: int = 0
    lock_key: str | None = None
    lock_token: str | None = None
    lock_ttl: int | None = None
    error: str | None = None
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this occurrence record into a JSON-compatible dictionary."""
        return {
            "occurrence_id": self.occurrence_id,
            "schedule_id": self.schedule_id,
            "scheduled_for": format_utc_datetime(self.scheduled_for),
            "status": self.status.value,
            "task_name": self.task_name,
            "task_kind": self.task_kind,
            "args": list(self.args),
            "kwargs": dict(self.kwargs),
            "queue": self.queue,
            "job_id": self.job_id,
            "claimed_by": self.claimed_by,
            "claimed_at": format_utc_datetime(self.claimed_at),
            "dispatched_at": format_utc_datetime(self.dispatched_at),
            "started_at": format_utc_datetime(self.started_at),
            "finished_at": format_utc_datetime(self.finished_at),
            "lease_until": self.lease_until,
            "dispatch_attempts": int(self.dispatch_attempts),
            "lock_key": self.lock_key,
            "lock_token": self.lock_token,
            "lock_ttl": self.lock_ttl,
            "error": self.error,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Deserialize a ScheduleOccurrence from a mapping."""
        scheduled_for = parse_utc_datetime(data.get("scheduled_for"))
        if scheduled_for is None:
            raise InvalidScheduleError("ScheduleOccurrence missing 'scheduled_for'")
        raw_kind = str(data.get("task_kind", "job"))
        task_kind: Literal["job", "callable"] = (
            "callable" if raw_kind == "callable" else "job"
        )
        raw_args = data.get("args")
        raw_kwargs = data.get("kwargs")
        return cls(
            occurrence_id=str(data["occurrence_id"]),
            schedule_id=str(data["schedule_id"]),
            scheduled_for=scheduled_for,
            status=OccurrenceStatus(str(data["status"])),
            task_name=str(data["task_name"]),
            task_kind=task_kind,
            args=list(raw_args) if isinstance(raw_args, list) else [],
            kwargs=dict(raw_kwargs) if isinstance(raw_kwargs, Mapping) else {},
            queue=str(data.get("queue", "default")),
            job_id=str(data["job_id"]) if data.get("job_id") is not None else None,
            claimed_by=(
                str(data["claimed_by"]) if data.get("claimed_by") is not None else None
            ),
            claimed_at=parse_utc_datetime(data.get("claimed_at")),
            dispatched_at=parse_utc_datetime(data.get("dispatched_at")),
            started_at=parse_utc_datetime(data.get("started_at")),
            finished_at=parse_utc_datetime(data.get("finished_at")),
            lease_until=(
                float(data["lease_until"])
                if data.get("lease_until") is not None
                else None
            ),
            dispatch_attempts=int(data.get("dispatch_attempts", 0)),
            lock_key=(
                str(data["lock_key"]) if data.get("lock_key") is not None else None
            ),
            lock_token=(
                str(data["lock_token"]) if data.get("lock_token") is not None else None
            ),
            lock_ttl=(
                int(data["lock_ttl"]) if data.get("lock_ttl") is not None else None
            ),
            error=str(data["error"]) if data.get("error") is not None else None,
            signature=str(data["signature"]) if data.get("signature") else None,
        )


__all__ = [
    "TERMINAL_OCCURRENCE_STATUSES",
    "MisfirePolicy",
    "OccurrenceStatus",
    "OverlapPolicy",
    "ScheduleOccurrence",
    "ScheduleRecord",
    "TriggerType",
    "format_utc_datetime",
    "parse_utc_datetime",
    "utc_now",
    "validate_json_arguments",
]
