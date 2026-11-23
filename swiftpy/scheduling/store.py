from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import ClassVar, Literal, Protocol, Self

import redis.asyncio as aioredis
from redis.exceptions import NoScriptError, RedisError

from swiftpy.core.config import Config, ConfigurationError
from swiftpy.scheduling._redis_lua import (
    CLAIM_DUE_LUA,
    CONFIRM_DISPATCHED_LUA,
    DELETE_SCHEDULE_LUA,
    EXTEND_OVERLAP_LOCK_LUA,
    GET_SCHEDULE_LUA,
    MARK_COMPLETED_LUA,
    MARK_DISPATCH_FAILED_LUA,
    MARK_FAILED_LUA,
    MARK_RUNNING_LUA,
    RECLAIM_PENDING_LUA,
    RELEASE_OVERLAP_LOCK_LUA,
    SAVE_SCHEDULE_LUA,
    SET_ENABLED_LUA,
    UPDATE_SCHEDULE_LUA,
    flat_list_to_dict,
    hash_to_occurrence,
    hash_to_schedule,
    schedule_to_lua_payload,
)
from swiftpy.scheduling.exceptions import (
    DuplicateScheduleError,
    InvalidScheduleError,
    ScheduleNotFoundError,
)
from swiftpy.scheduling.models import (
    TERMINAL_OCCURRENCE_STATUSES,
    MisfirePolicy,
    OccurrenceStatus,
    OverlapPolicy,
    ScheduleOccurrence,
    ScheduleRecord,
    TriggerType,
    format_utc_datetime,
    utc_now,
)
from swiftpy.support.redis_config import resolve_redis_params

logger = logging.getLogger("swiftpy.scheduling")


def _default_schedule_prefix() -> str:
    try:
        cfg = Config.get_instance()
        val = cfg.get("schedule.prefix")
        if val and isinstance(val, str):
            return val
    except ConfigurationError:
        logger.debug("Falling back to default schedule prefix", exc_info=True)
    return "swiftpy_schedule:"


def _default_schedule_database() -> int:
    try:
        cfg = Config.get_instance()
        for key in (
            "schedule.database",
            "queue.connections.redis.database",
            "redis_queue_db",
            "redis.queue_db",
        ):
            val = cfg.get(key)
            if val is not None and val != "":
                return int(val)
    except (ConfigurationError, TypeError, ValueError):
        logger.debug("Falling back to default REDIS_QUEUE_DB (1)", exc_info=True)
    return 1


def _require_utc_iso(dt: datetime) -> str:
    iso = format_utc_datetime(dt)
    if iso is None:
        raise InvalidScheduleError("Expected non-null datetime value.")
    return iso


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    """Result emitted for each schedule inspected by ``ScheduleStore.claim_due()``."""

    kind: Literal["claimed", "skipped"]
    schedule: ScheduleRecord
    occurrence: ScheduleOccurrence
    reason: str | None = None


NextRunCalculator = Callable[[ScheduleRecord, datetime], datetime | None]


class ScheduleStore(Protocol):
    """Persistence backend contract for SwiftPY schedule records and occurrence claims."""

    async def save_schedule(
        self,
        record: ScheduleRecord,
        *,
        upsert: bool = False,
    ) -> tuple[ScheduleRecord, bool]:
        """Persist a new schedule or state-preserving upsert. Returns ``(record, upserted)``."""
        ...

    async def get_schedule(self, schedule_or_name: str) -> ScheduleRecord | None:
        """Retrieve a schedule record by ID or unique name."""
        ...

    async def list_schedules(self) -> list[ScheduleRecord]:
        """Return all persisted schedule records."""
        ...

    async def update_schedule(self, record: ScheduleRecord) -> ScheduleRecord:
        """Persist updated configuration fields on an existing schedule record."""
        ...

    async def delete_schedule(self, schedule_or_name: str) -> bool:
        """Delete a schedule and cancel any un-dispatched pending claims for it."""
        ...

    async def set_enabled(
        self,
        schedule_or_name: str,
        enabled: bool,
        *,
        next_run_at: datetime | None = None,
    ) -> ScheduleRecord:
        """Enable or disable a schedule without interrupting already-running jobs."""
        ...

    async def claim_due(
        self,
        *,
        now: datetime,
        claimed_by: str,
        default_grace_seconds: float,
        default_lock_ttl: int,
        dispatch_lease_seconds: float,
        next_run_calculator: NextRunCalculator,
        limit: int = 50,
    ) -> list[ClaimOutcome]:
        """Atomically claim due schedules into ``dispatch_pending`` or ``skipped`` occurrences."""
        ...

    async def confirm_dispatched(
        self,
        occurrence_id: str,
        *,
        job_id: str,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        """Confirm that a claimed occurrence has been accepted onto QueueDispatcher."""
        ...

    async def reclaim_pending_dispatches(
        self,
        *,
        now: datetime,
        reclaimed_by: str,
        dispatch_lease_seconds: float,
        max_dispatch_attempts: int,
        limit: int = 50,
    ) -> tuple[list[ScheduleOccurrence], list[ScheduleOccurrence]]:
        """
        Reclaim expired ``dispatch_pending`` occurrences.
        Returns ``(to_retry, exhausted_failed)``.
        """
        ...

    async def mark_dispatch_attempt_failed(
        self,
        occurrence_id: str,
        *,
        error: str,
        now: datetime,
        max_dispatch_attempts: int,
    ) -> ScheduleOccurrence | None:
        """Record a queue push failure; mark permanently failed once max attempts exhaust."""
        ...

    async def mark_running(
        self,
        occurrence_id: str,
        *,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        """Mark an occurrence as actively running inside QueueWorker."""
        ...

    async def mark_completed(
        self,
        occurrence_id: str,
        *,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        """Mark an occurrence completed and release any held overlap lock."""
        ...

    async def mark_failed(
        self,
        occurrence_id: str,
        *,
        error: str,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        """Mark an occurrence failed after exhausting retries and release any held overlap lock."""
        ...

    async def release_overlap_lock(self, lock_key: str, lock_token: str) -> bool:
        """Release an overlap lock strictly if the stored token matches ``lock_token``."""
        ...

    async def extend_overlap_lock(
        self, lock_key: str, lock_token: str, ttl_seconds: float
    ) -> bool:
        """Re-arm an overlap lock's expiry strictly if the stored token matches ``lock_token``."""
        ...

    async def get_occurrence(self, occurrence_id: str) -> ScheduleOccurrence | None:
        """Fetch an occurrence record by its ID."""
        ...

    async def next_due_at(self) -> datetime | None:
        """Return the earliest ``next_run_at`` among enabled schedules, or None."""
        ...

    async def close(self) -> None:
        """Release store resources cleanly."""
        ...


class MemoryScheduleStore:
    """
    In-memory, process-local schedule store intended for deterministic tests and
    single-process local development.

    NOT durable across process restarts and NOT safe across distributed replicas.
    """

    is_durable: ClassVar[bool] = False

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utc_now,
        prefix: str = "swiftpy_schedule:",
    ) -> None:
        self._clock = clock
        self.prefix = prefix
        self._lock = asyncio.Lock()
        self._records: dict[str, ScheduleRecord] = {}
        self._name_to_id: dict[str, str] = {}
        self._occurrences: dict[str, ScheduleOccurrence] = {}
        self._overlap_locks: dict[str, tuple[str, float]] = {}
        try:
            cfg = Config.get_instance()
            env_name = str(cfg.get("app.env") or cfg.get("env") or "").strip().lower()
            if env_name in ("production", "prod"):
                logger.warning(
                    "MemoryScheduleStore initialized while APP_ENV=%r: schedules, occurrences, "
                    "and overlap locks are process-local and NOT durable across restarts or replicas. "
                    "Use RedisScheduleStore in production.",
                    env_name,
                )
        except (ConfigurationError, RuntimeError):
            pass

    def _resolve_id(self, schedule_or_name: str) -> str | None:
        if schedule_or_name in self._records:
            return schedule_or_name
        return self._name_to_id.get(schedule_or_name)

    def _purge_expired_locks(self, now_ts: float) -> None:
        expired = [
            key
            for key, (_tok, expires_at) in self._overlap_locks.items()
            if expires_at <= now_ts
        ]
        for key in expired:
            del self._overlap_locks[key]

    def _release_lock_unlocked(self, lock_key: str | None, lock_token: str | None) -> bool:
        if not lock_key or not lock_token:
            return False
        current = self._overlap_locks.get(lock_key)
        if current is None or current[0] != lock_token:
            return False
        del self._overlap_locks[lock_key]
        return True

    def _extend_lock_unlocked(
        self, lock_key: str | None, lock_token: str | None, ttl_seconds: float
    ) -> bool:
        if not lock_key or not lock_token:
            return False
        current = self._overlap_locks.get(lock_key)
        if current is None or current[0] != lock_token:
            return False
        self._overlap_locks[lock_key] = (
            lock_token,
            self._clock().timestamp() + float(ttl_seconds),
        )
        return True

    async def save_schedule(
        self,
        record: ScheduleRecord,
        *,
        upsert: bool = False,
    ) -> tuple[ScheduleRecord, bool]:
        async with self._lock:
            existing_id = self._resolve_id(record.id)
            if existing_id is None and record.name is not None:
                existing_id = self._resolve_id(record.name)

            if existing_id is not None:
                if not upsert:
                    conflict_label = record.name or record.id
                    raise DuplicateScheduleError(
                        f"Schedule '{conflict_label}' is already registered."
                    )

                existing = self._records[existing_id]
                timing_changed = (
                    existing.trigger != record.trigger
                    or existing.expression != record.expression
                    or existing.interval_seconds != record.interval_seconds
                    or existing.run_at != record.run_at
                    or existing.timezone != record.timezone
                )

                updated = copy.deepcopy(record)
                updated.id = existing.id
                updated.created_at = existing.created_at
                updated.updated_at = record.updated_at
                # Preserve aggregate operational state across restarts/boot sync
                updated.enabled = existing.enabled
                updated.last_run_at = existing.last_run_at
                updated.last_claimed_occurrence = existing.last_claimed_occurrence
                updated.last_occurrence_id = existing.last_occurrence_id
                updated.last_status = existing.last_status
                updated.last_error = existing.last_error
                if not timing_changed:
                    updated.next_run_at = existing.next_run_at

                if existing.name and existing.name != updated.name:
                    self._name_to_id.pop(existing.name, None)
                if updated.name:
                    self._name_to_id[updated.name] = updated.id
                self._records[updated.id] = updated
                return copy.deepcopy(updated), True

            stored = copy.deepcopy(record)
            self._records[stored.id] = stored
            if stored.name:
                self._name_to_id[stored.name] = stored.id
            return copy.deepcopy(stored), False

    async def get_schedule(self, schedule_or_name: str) -> ScheduleRecord | None:
        async with self._lock:
            sid = self._resolve_id(schedule_or_name)
            if sid is None:
                return None
            rec = self._records.get(sid)
            return copy.deepcopy(rec) if rec is not None else None

    async def list_schedules(self) -> list[ScheduleRecord]:
        async with self._lock:
            return [
                copy.deepcopy(rec)
                for rec in sorted(
                    self._records.values(),
                    key=lambda r: (r.next_run_at is None, r.next_run_at or r.created_at, r.id),
                )
            ]

    async def next_due_at(self) -> datetime | None:
        async with self._lock:
            candidates = [
                rec.next_run_at
                for rec in self._records.values()
                if rec.enabled and rec.next_run_at is not None
            ]
            return min(candidates) if candidates else None

    async def update_schedule(self, record: ScheduleRecord) -> ScheduleRecord:
        async with self._lock:
            if record.id not in self._records:
                raise ScheduleNotFoundError(f"Schedule '{record.id}' not found.")
            existing = self._records[record.id]
            stored = copy.deepcopy(record)
            if existing.name and existing.name != stored.name:
                self._name_to_id.pop(existing.name, None)
            if stored.name:
                self._name_to_id[stored.name] = stored.id
            self._records[stored.id] = stored
            return copy.deepcopy(stored)

    async def delete_schedule(self, schedule_or_name: str) -> bool:
        async with self._lock:
            sid = self._resolve_id(schedule_or_name)
            if sid is None:
                return False
            rec = self._records.pop(sid)
            if rec.name:
                self._name_to_id.pop(rec.name, None)

            now = self._clock()
            for occ in self._occurrences.values():
                if occ.schedule_id == sid and occ.status in (
                    OccurrenceStatus.PENDING,
                    OccurrenceStatus.CLAIMED,
                    OccurrenceStatus.DISPATCH_PENDING,
                ):
                    occ.status = OccurrenceStatus.CANCELLED
                    occ.finished_at = now
                    occ.lease_until = None
                    self._release_lock_unlocked(occ.lock_key, occ.lock_token)
            return True

    async def set_enabled(
        self,
        schedule_or_name: str,
        enabled: bool,
        *,
        next_run_at: datetime | None = None,
    ) -> ScheduleRecord:
        async with self._lock:
            sid = self._resolve_id(schedule_or_name)
            if sid is None:
                raise ScheduleNotFoundError(f"Schedule '{schedule_or_name}' not found.")
            rec = self._records[sid]
            rec.enabled = enabled
            if enabled and next_run_at is not None:
                rec.next_run_at = next_run_at
            rec.updated_at = self._clock()
            return copy.deepcopy(rec)

    async def claim_due(
        self,
        *,
        now: datetime,
        claimed_by: str,
        default_grace_seconds: float,
        default_lock_ttl: int,
        dispatch_lease_seconds: float,
        next_run_calculator: NextRunCalculator,
        limit: int = 50,
    ) -> list[ClaimOutcome]:
        async with self._lock:
            now_ts = now.timestamp()
            self._purge_expired_locks(now_ts)

            due_candidates = [
                rec
                for rec in self._records.values()
                if rec.enabled and rec.next_run_at is not None and rec.next_run_at <= now
            ]
            due_candidates.sort(key=lambda r: (r.next_run_at or now, r.id))

            outcomes: list[ClaimOutcome] = []
            for rec in due_candidates:
                if len(outcomes) >= limit:
                    break
                scheduled_for = rec.next_run_at
                if scheduled_for is None:
                    continue

                occ_id = f"{rec.id}:{int(scheduled_for.timestamp() * 1000)}"
                if rec.last_claimed_occurrence == occ_id:
                    continue

                grace = (
                    rec.grace_seconds
                    if rec.grace_seconds is not None
                    else default_grace_seconds
                )
                lag = now_ts - scheduled_for.timestamp()
                if lag > grace and rec.misfire_policy == MisfirePolicy.SKIP:
                    occ = ScheduleOccurrence(
                        occurrence_id=occ_id,
                        schedule_id=rec.id,
                        scheduled_for=scheduled_for,
                        status=OccurrenceStatus.SKIPPED,
                        task_name=rec.task_name,
                        task_kind=rec.task_kind,
                        args=list(rec.args),
                        kwargs=dict(rec.kwargs),
                        queue=rec.queue,
                        claimed_by=claimed_by,
                        claimed_at=now,
                        finished_at=now,
                        error="Skipped due to misfire grace window exceeded",
                    )
                    self._occurrences[occ_id] = occ
                    if rec.trigger == TriggerType.ONCE:
                        rec.enabled = False
                        rec.next_run_at = None
                    else:
                        rec.next_run_at = next_run_calculator(rec, now)
                    rec.last_claimed_occurrence = occ_id
                    rec.last_occurrence_id = occ_id
                    rec.last_status = OccurrenceStatus.SKIPPED
                    rec.updated_at = now
                    outcomes.append(
                        ClaimOutcome(
                            kind="skipped",
                            schedule=copy.deepcopy(rec),
                            occurrence=copy.deepcopy(occ),
                            reason="misfire",
                        )
                    )
                    continue

                lock_key: str | None = None
                lock_token: str | None = None
                lock_ttl: int | None = None
                if rec.overlap_policy == OverlapPolicy.SKIP:
                    lock_key = f"{self.prefix}overlap:{rec.id}"
                    existing_lock = self._overlap_locks.get(lock_key)
                    if existing_lock is not None and existing_lock[1] > now_ts:
                        occ = ScheduleOccurrence(
                            occurrence_id=occ_id,
                            schedule_id=rec.id,
                            scheduled_for=scheduled_for,
                            status=OccurrenceStatus.SKIPPED,
                            task_name=rec.task_name,
                            task_kind=rec.task_kind,
                            args=list(rec.args),
                            kwargs=dict(rec.kwargs),
                            queue=rec.queue,
                            claimed_by=claimed_by,
                            claimed_at=now,
                            finished_at=now,
                            error="Skipped due to overlap lock held by active occurrence",
                        )
                        self._occurrences[occ_id] = occ
                        if rec.trigger == TriggerType.ONCE:
                            rec.enabled = False
                            rec.next_run_at = None
                        else:
                            ref_dt = max(scheduled_for, now)
                            rec.next_run_at = next_run_calculator(rec, ref_dt)
                        rec.last_claimed_occurrence = occ_id
                        rec.last_occurrence_id = occ_id
                        rec.last_status = OccurrenceStatus.SKIPPED
                        rec.updated_at = now
                        outcomes.append(
                            ClaimOutcome(
                                kind="skipped",
                                schedule=copy.deepcopy(rec),
                                occurrence=copy.deepcopy(occ),
                                reason="overlap",
                            )
                        )
                        continue

                    lock_ttl = rec.lock_ttl if rec.lock_ttl is not None else default_lock_ttl
                    lock_token = uuid.uuid4().hex
                    self._overlap_locks[lock_key] = (
                        lock_token,
                        now_ts + float(lock_ttl),
                    )

                occ = ScheduleOccurrence(
                    occurrence_id=occ_id,
                    schedule_id=rec.id,
                    scheduled_for=scheduled_for,
                    status=OccurrenceStatus.DISPATCH_PENDING,
                    task_name=rec.task_name,
                    task_kind=rec.task_kind,
                    args=list(rec.args),
                    kwargs=dict(rec.kwargs),
                    queue=rec.queue,
                    claimed_by=claimed_by,
                    claimed_at=now,
                    lease_until=now_ts + float(dispatch_lease_seconds),
                    dispatch_attempts=1,
                    lock_key=lock_key,
                    lock_token=lock_token,
                    lock_ttl=lock_ttl,
                    signature=rec.signature,
                )
                self._occurrences[occ_id] = occ

                if rec.trigger == TriggerType.ONCE:
                    rec.enabled = False
                    rec.next_run_at = None
                else:
                    ref_dt = max(scheduled_for, now)
                    rec.next_run_at = next_run_calculator(rec, ref_dt)

                rec.last_claimed_occurrence = occ_id
                rec.last_occurrence_id = occ_id
                rec.last_status = OccurrenceStatus.DISPATCH_PENDING
                rec.updated_at = now

                outcomes.append(
                    ClaimOutcome(
                        kind="claimed",
                        schedule=copy.deepcopy(rec),
                        occurrence=copy.deepcopy(occ),
                    )
                )

            return outcomes

    async def confirm_dispatched(
        self,
        occurrence_id: str,
        *,
        job_id: str,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        async with self._lock:
            occ = self._occurrences.get(occurrence_id)
            if occ is None:
                return None
            if occ.status == OccurrenceStatus.CANCELLED:
                self._release_lock_unlocked(occ.lock_key, occ.lock_token)
                return copy.deepcopy(occ)
            if occ.status not in (
                OccurrenceStatus.PENDING,
                OccurrenceStatus.CLAIMED,
                OccurrenceStatus.DISPATCH_PENDING,
            ):
                if occ.status in (OccurrenceStatus.FAILED, OccurrenceStatus.SKIPPED):
                    self._release_lock_unlocked(occ.lock_key, occ.lock_token)
                return copy.deepcopy(occ)

            occ.status = OccurrenceStatus.DISPATCHED
            occ.job_id = job_id
            occ.dispatched_at = now
            occ.lease_until = None
            occ.error = None

            rec = self._records.get(occ.schedule_id)
            if rec is not None:
                rec.last_run_at = occ.scheduled_for
                rec.last_occurrence_id = occ.occurrence_id
                rec.last_status = OccurrenceStatus.DISPATCHED
                rec.last_error = None
                rec.updated_at = now

            return copy.deepcopy(occ)

    async def reclaim_pending_dispatches(
        self,
        *,
        now: datetime,
        reclaimed_by: str,
        dispatch_lease_seconds: float,
        max_dispatch_attempts: int,
        limit: int = 50,
    ) -> tuple[list[ScheduleOccurrence], list[ScheduleOccurrence]]:
        async with self._lock:
            now_ts = now.timestamp()
            expired = [
                occ
                for occ in self._occurrences.values()
                if occ.status == OccurrenceStatus.DISPATCH_PENDING
                and occ.lease_until is not None
                and occ.lease_until <= now_ts
            ]
            expired.sort(key=lambda o: (o.lease_until or 0.0, o.occurrence_id))

            to_retry: list[ScheduleOccurrence] = []
            exhausted: list[ScheduleOccurrence] = []
            for occ in expired[:limit]:
                if occ.dispatch_attempts >= max_dispatch_attempts:
                    occ.status = OccurrenceStatus.FAILED
                    occ.lease_until = None
                    occ.finished_at = now
                    if not occ.error:
                        occ.error = (
                            f"Dispatch failed after {occ.dispatch_attempts} pending attempts"
                        )
                    self._release_lock_unlocked(occ.lock_key, occ.lock_token)
                    rec = self._records.get(occ.schedule_id)
                    if rec is not None:
                        rec.last_status = OccurrenceStatus.FAILED
                        rec.last_error = occ.error
                        rec.updated_at = now
                    exhausted.append(copy.deepcopy(occ))
                else:
                    occ.dispatch_attempts += 1
                    occ.claimed_by = reclaimed_by
                    occ.lease_until = now_ts + float(dispatch_lease_seconds)
                    to_retry.append(copy.deepcopy(occ))

            return to_retry, exhausted

    async def mark_dispatch_attempt_failed(
        self,
        occurrence_id: str,
        *,
        error: str,
        now: datetime,
        max_dispatch_attempts: int,
    ) -> ScheduleOccurrence | None:
        async with self._lock:
            occ = self._occurrences.get(occurrence_id)
            if occ is None:
                return None
            if occ.status not in (
                OccurrenceStatus.PENDING,
                OccurrenceStatus.CLAIMED,
                OccurrenceStatus.DISPATCH_PENDING,
            ):
                return copy.deepcopy(occ)
            occ.error = error
            if occ.dispatch_attempts >= max_dispatch_attempts:
                occ.status = OccurrenceStatus.FAILED
                occ.lease_until = None
                occ.finished_at = now
                self._release_lock_unlocked(occ.lock_key, occ.lock_token)
                rec = self._records.get(occ.schedule_id)
                if rec is not None:
                    rec.last_status = OccurrenceStatus.FAILED
                    rec.last_error = error
                    rec.updated_at = now
            else:
                # Allow immediate recovery on the next tick when a synchronous push error occurs
                occ.lease_until = now.timestamp()
            return copy.deepcopy(occ)

    async def mark_running(
        self,
        occurrence_id: str,
        *,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        async with self._lock:
            occ = self._occurrences.get(occurrence_id)
            if occ is None:
                return None
            if occ.status in TERMINAL_OCCURRENCE_STATUSES:
                return copy.deepcopy(occ)
            occ.status = OccurrenceStatus.RUNNING
            occ.started_at = occ.started_at or now
            occ.lease_until = None
            if occ.lock_key and occ.lock_token and occ.lock_ttl:
                self._extend_lock_unlocked(
                    occ.lock_key, occ.lock_token, float(occ.lock_ttl)
                )
            rec = self._records.get(occ.schedule_id)
            if rec is not None:
                rec.last_status = OccurrenceStatus.RUNNING
                rec.updated_at = now
            return copy.deepcopy(occ)

    async def mark_completed(
        self,
        occurrence_id: str,
        *,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        async with self._lock:
            occ = self._occurrences.get(occurrence_id)
            if occ is None:
                return None
            if occ.status in (
                OccurrenceStatus.FAILED,
                OccurrenceStatus.CANCELLED,
                OccurrenceStatus.SKIPPED,
            ):
                return copy.deepcopy(occ)
            if occ.status == OccurrenceStatus.COMPLETED:
                self._release_lock_unlocked(occ.lock_key, occ.lock_token)
                return copy.deepcopy(occ)
            occ.status = OccurrenceStatus.COMPLETED
            occ.finished_at = now
            occ.lease_until = None
            occ.error = None
            self._release_lock_unlocked(occ.lock_key, occ.lock_token)
            rec = self._records.get(occ.schedule_id)
            if rec is not None:
                rec.last_run_at = rec.last_run_at or occ.scheduled_for
                rec.last_occurrence_id = occ.occurrence_id
                rec.last_status = OccurrenceStatus.COMPLETED
                rec.last_error = None
                rec.updated_at = now
            return copy.deepcopy(occ)

    async def mark_failed(
        self,
        occurrence_id: str,
        *,
        error: str,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        async with self._lock:
            occ = self._occurrences.get(occurrence_id)
            if occ is None:
                return None
            if occ.status in (
                OccurrenceStatus.COMPLETED,
                OccurrenceStatus.CANCELLED,
                OccurrenceStatus.SKIPPED,
            ):
                return copy.deepcopy(occ)
            if occ.status == OccurrenceStatus.FAILED:
                self._release_lock_unlocked(occ.lock_key, occ.lock_token)
                return copy.deepcopy(occ)
            occ.status = OccurrenceStatus.FAILED
            occ.finished_at = now
            occ.lease_until = None
            occ.error = error
            self._release_lock_unlocked(occ.lock_key, occ.lock_token)
            rec = self._records.get(occ.schedule_id)
            if rec is not None:
                rec.last_occurrence_id = occ.occurrence_id
                rec.last_status = OccurrenceStatus.FAILED
                rec.last_error = error
                rec.updated_at = now
            return copy.deepcopy(occ)

    async def release_overlap_lock(self, lock_key: str, lock_token: str) -> bool:
        async with self._lock:
            return self._release_lock_unlocked(lock_key, lock_token)

    async def extend_overlap_lock(
        self, lock_key: str, lock_token: str, ttl_seconds: float
    ) -> bool:
        async with self._lock:
            return self._extend_lock_unlocked(lock_key, lock_token, ttl_seconds)

    async def get_occurrence(self, occurrence_id: str) -> ScheduleOccurrence | None:
        async with self._lock:
            occ = self._occurrences.get(occurrence_id)
            return copy.deepcopy(occ) if occ is not None else None

    async def close(self) -> None:
        return None


class RedisScheduleStore:
    """
    Redis-backed durable ScheduleStore on ``REDIS_QUEUE_DB`` with atomic Lua claiming,
    state-preserving upsert, compare-and-del overlap locking, and lease-based
    ``dispatch_pending`` crash recovery.
    """

    is_durable: ClassVar[bool] = True

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        connection_pool: aioredis.ConnectionPool | None = None,
        redis_url: str | None = None,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
        database: int | None = None,
        prefix: str | None = None,
        max_connections: int | None = None,
        timeout: float = 30.0,
        *,
        clock: Callable[[], datetime] = utc_now,
        retention_seconds: int = 7 * 24 * 3600,
        max_occurrences_per_schedule: int = 200,
    ) -> None:
        self._client: aioredis.Redis | None = redis_client
        self._pool: aioredis.ConnectionPool | None = connection_pool
        self._redis_url = redis_url
        self.host = host
        self.port = port
        self.password = password
        self._explicit_database = database
        self.database = database if database is not None else _default_schedule_database()
        self.prefix = prefix if prefix is not None else _default_schedule_prefix()
        self.max_connections = max_connections or 50
        self.timeout = timeout
        self._clock = clock
        self.retention_seconds = max(0, int(retention_seconds))
        self.max_occurrences_per_schedule = max(0, int(max_occurrences_per_schedule))
        self._owns_client = redis_client is None
        self._owns_pool = connection_pool is None and redis_client is None
        self._client_lock = asyncio.Lock()
        self._script_shas: dict[str, str] = {}

    def _schedules_key(self) -> str:
        return f"{self.prefix}schedules"

    def _names_key(self) -> str:
        return f"{self.prefix}names"

    def _due_key(self) -> str:
        return f"{self.prefix}due"

    def _pending_key(self) -> str:
        return f"{self.prefix}pending_dispatch"

    def _schedule_key(self, schedule_id: str) -> str:
        return f"{self.prefix}schedule:{schedule_id}"

    def _occurrence_key(self, occurrence_id: str) -> str:
        return f"{self.prefix}occ:{occurrence_id}"

    async def get_client(self) -> aioredis.Redis:
        """Lazily initialize and return the async Redis client."""
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            if self._redis_url:
                self._client = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    encoding_errors="replace",
                )
            else:
                if self._pool is None:
                    resolved_db = (
                        self._explicit_database
                        if self._explicit_database is not None
                        else _default_schedule_database()
                    )
                    params = resolve_redis_params(
                        section="queue",
                        host=self.host,
                        port=self.port,
                        password=self.password,
                        database=resolved_db,
                    )
                    self.database = int(params["database"])
                    self._pool = aioredis.BlockingConnectionPool(
                        host=params["host"],
                        port=params["port"],
                        db=params["database"],
                        password=params["password"] or None,
                        decode_responses=True,
                        encoding_errors="replace",
                        max_connections=self.max_connections,
                        timeout=self.timeout,
                    )
                self._client = aioredis.Redis(
                    connection_pool=self._pool,
                    decode_responses=True,
                    encoding_errors="replace",
                )
            self._owns_client = True
            return self._client

    async def _eval_lua(
        self,
        client: aioredis.Redis,
        script: str,
        numkeys: int,
        *keys_and_args: str | float,
    ) -> object:
        """Execute a Lua script with cached EVALSHA and transparent NoScriptError reload."""
        sha = self._script_shas.get(script)
        if sha is None and hasattr(client, "script_load"):
            try:
                loaded: object = await client.script_load(script)
                if isinstance(loaded, (bytes, bytearray)):
                    sha = loaded.decode("utf-8")
                elif loaded is not None:
                    sha = str(loaded)
                if sha is not None:
                    self._script_shas[script] = sha
            except (RedisError, ConnectionError, OSError):
                sha = None

        if sha is not None and hasattr(client, "evalsha"):
            try:
                res: object = await client.evalsha(sha, numkeys, *keys_and_args)
                return res
            except NoScriptError:
                loaded_retry: object = await client.script_load(script)
                sha = (
                    loaded_retry.decode("utf-8")
                    if isinstance(loaded_retry, (bytes, bytearray))
                    else str(loaded_retry)
                )
                self._script_shas[script] = sha
                res_retry: object = await client.evalsha(sha, numkeys, *keys_and_args)
                return res_retry

        eval_res: object = await client.eval(script, numkeys, *keys_and_args)
        return eval_res

    async def save_schedule(
        self,
        record: ScheduleRecord,
        *,
        upsert: bool = False,
    ) -> tuple[ScheduleRecord, bool]:
        client = await self.get_client()
        payload = schedule_to_lua_payload(record)
        res = await self._eval_lua(
            client,
            SAVE_SCHEDULE_LUA,
            3,
            self._schedules_key(),
            self._names_key(),
            self._due_key(),
            self.prefix,
            "1" if upsert else "0",
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
        )
        if not isinstance(res, list) or not res:
            raise InvalidScheduleError("Unexpected Redis response while saving schedule.")

        tag = str(res[0])
        if tag == "err":
            err_kind = str(res[1]) if len(res) > 1 else "duplicate"
            label = str(res[2]) if len(res) > 2 else (record.name or record.id)
            if err_kind == "duplicate":
                raise DuplicateScheduleError(
                    f"Schedule '{label}' is already registered."
                )
            raise InvalidScheduleError(f"Failed to save schedule '{label}': {err_kind}")

        raw_hash = res[2] if len(res) > 2 else {}
        return hash_to_schedule(flat_list_to_dict(raw_hash)), tag == "upserted"

    async def get_schedule(self, schedule_or_name: str) -> ScheduleRecord | None:
        client = await self.get_client()
        res = await self._eval_lua(
            client,
            GET_SCHEDULE_LUA,
            1,
            self._names_key(),
            self.prefix,
            schedule_or_name,
        )
        parsed = flat_list_to_dict(res)
        if not parsed:
            return None
        return hash_to_schedule(parsed)

    async def list_schedules(self) -> list[ScheduleRecord]:
        client = await self.get_client()
        raw_ids: object = await client.smembers(self._schedules_key())
        if not isinstance(raw_ids, (set, list, tuple)) or not raw_ids:
            return []

        ids = sorted(str(item) for item in raw_ids)
        async with client.pipeline(transaction=False) as pipe:
            for sid in ids:
                pipe.hgetall(self._schedule_key(sid))
            raw_hashes: object = await pipe.execute()

        records: list[ScheduleRecord] = []
        if isinstance(raw_hashes, list):
            for item in raw_hashes:
                parsed = flat_list_to_dict(item)
                if parsed:
                    records.append(hash_to_schedule(parsed))
        records.sort(
            key=lambda r: (r.next_run_at is None, r.next_run_at or r.created_at, r.id)
        )
        return records

    async def update_schedule(self, record: ScheduleRecord) -> ScheduleRecord:
        client = await self.get_client()
        payload = schedule_to_lua_payload(record)
        res = await self._eval_lua(
            client,
            UPDATE_SCHEDULE_LUA,
            2,
            self._names_key(),
            self._due_key(),
            self.prefix,
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
        )
        if not isinstance(res, list) or not res:
            raise InvalidScheduleError("Unexpected Redis response while updating schedule.")

        tag = str(res[0])
        if tag == "err":
            err_kind = str(res[1]) if len(res) > 1 else "unknown"
            label = str(res[2]) if len(res) > 2 else record.id
            if err_kind == "not_found":
                raise ScheduleNotFoundError(f"Schedule '{label}' not found.")
            if err_kind == "duplicate":
                raise DuplicateScheduleError(
                    f"Schedule '{label}' is already registered."
                )
            raise InvalidScheduleError(f"Failed to update schedule '{label}': {err_kind}")

        raw_hash = res[1] if len(res) > 1 else {}
        return hash_to_schedule(flat_list_to_dict(raw_hash))

    async def delete_schedule(self, schedule_or_name: str) -> bool:
        client = await self.get_client()
        now_iso = _require_utc_iso(self._clock())
        res = await self._eval_lua(
            client,
            DELETE_SCHEDULE_LUA,
            4,
            self._schedules_key(),
            self._names_key(),
            self._due_key(),
            self._pending_key(),
            self.prefix,
            schedule_or_name,
            now_iso,
            str(self.retention_seconds),
        )
        if isinstance(res, (int, float)):
            return int(res) > 0
        return bool(res)

    async def set_enabled(
        self,
        schedule_or_name: str,
        enabled: bool,
        *,
        next_run_at: datetime | None = None,
    ) -> ScheduleRecord:
        client = await self.get_client()
        updated_iso = _require_utc_iso(self._clock())
        has_next = enabled and next_run_at is not None
        next_iso = _require_utc_iso(next_run_at) if next_run_at is not None else ""
        next_ts = repr(next_run_at.timestamp()) if next_run_at is not None else ""

        res = await self._eval_lua(
            client,
            SET_ENABLED_LUA,
            2,
            self._names_key(),
            self._due_key(),
            self.prefix,
            schedule_or_name,
            "1" if enabled else "0",
            "1" if has_next else "0",
            next_iso,
            next_ts,
            updated_iso,
        )
        if not isinstance(res, list) or not res:
            raise InvalidScheduleError("Unexpected Redis response updating enabled state.")

        tag = str(res[0])
        if tag == "err":
            label = str(res[2]) if len(res) > 2 else schedule_or_name
            raise ScheduleNotFoundError(f"Schedule '{label}' not found.")

        raw_hash = res[1] if len(res) > 1 else {}
        return hash_to_schedule(flat_list_to_dict(raw_hash))

    async def claim_due(
        self,
        *,
        now: datetime,
        claimed_by: str,
        default_grace_seconds: float,
        default_lock_ttl: int,
        dispatch_lease_seconds: float,
        next_run_calculator: NextRunCalculator,
        limit: int = 50,
    ) -> list[ClaimOutcome]:
        if limit <= 0:
            return []

        client = await self.get_client()
        now_iso = _require_utc_iso(now)
        now_ts = now.timestamp()

        raw_due: object = await client.zrangebyscore(
            self._due_key(),
            "-inf",
            now_ts,
            start=0,
            num=int(limit),
        )
        if not isinstance(raw_due, list) or not raw_due:
            return []

        due_ids = [str(item) for item in raw_due]
        async with client.pipeline(transaction=False) as pipe:
            for sid in due_ids:
                pipe.hgetall(self._schedule_key(sid))
            raw_hashes: object = await pipe.execute()

        if not isinstance(raw_hashes, list):
            return []

        candidates: list[dict[str, object]] = []
        for item in raw_hashes:
            parsed = flat_list_to_dict(item)
            if not parsed:
                continue
            rec = hash_to_schedule(parsed)
            scheduled_for = rec.next_run_at
            if not rec.enabled or scheduled_for is None or scheduled_for > now:
                continue

            occ_id = f"{rec.id}:{int(scheduled_for.timestamp() * 1000)}"
            if rec.last_claimed_occurrence == occ_id:
                continue

            grace = (
                rec.grace_seconds
                if rec.grace_seconds is not None
                else default_grace_seconds
            )
            lag = now_ts - scheduled_for.timestamp()
            is_misfire_skip = (
                lag > grace and rec.misfire_policy == MisfirePolicy.SKIP
            )

            if rec.trigger == TriggerType.ONCE:
                next_dt: datetime | None = None
            elif is_misfire_skip:
                next_dt = next_run_calculator(rec, now)
            else:
                ref_dt = max(scheduled_for, now)
                next_dt = next_run_calculator(rec, ref_dt)

            lock_ttl_val = (
                rec.lock_ttl if rec.lock_ttl is not None else default_lock_ttl
            )
            candidates.append(
                {
                    "id": rec.id,
                    "occ_id": occ_id,
                    "expected_next_run": _require_utc_iso(scheduled_for),
                    "scheduled_for": _require_utc_iso(scheduled_for),
                    "scheduled_for_ts": repr(scheduled_for.timestamp()),
                    "is_once": rec.trigger == TriggerType.ONCE,
                    "is_misfire_skip": is_misfire_skip,
                    "overlap_skip": rec.overlap_policy == OverlapPolicy.SKIP,
                    "lock_key": f"{self.prefix}overlap:{rec.id}",
                    "lock_token": uuid.uuid4().hex,
                    "lock_ttl": int(lock_ttl_val),
                    "lease_until": repr(now_ts + float(dispatch_lease_seconds)),
                    "next_run_at": (
                        _require_utc_iso(next_dt) if next_dt is not None else ""
                    ),
                    "next_run_ts": (
                        repr(next_dt.timestamp()) if next_dt is not None else ""
                    ),
                }
            )

        if not candidates:
            return []

        res = await self._eval_lua(
            client,
            CLAIM_DUE_LUA,
            2,
            self._due_key(),
            self._pending_key(),
            self.prefix,
            now_iso,
            claimed_by,
            str(self.retention_seconds),
            str(self.max_occurrences_per_schedule),
            json.dumps(candidates, ensure_ascii=False, allow_nan=False),
        )
        if not isinstance(res, list):
            return []

        outcomes: list[ClaimOutcome] = []
        for entry in res:
            if not isinstance(entry, list) or len(entry) < 4:
                continue
            kind_raw = str(entry[0])
            reason_raw = str(entry[1]) if entry[1] else None
            sched_dict = flat_list_to_dict(entry[2])
            occ_dict = flat_list_to_dict(entry[3])
            if not sched_dict or not occ_dict:
                continue
            kind: Literal["claimed", "skipped"] = (
                "skipped" if kind_raw == "skipped" else "claimed"
            )
            outcomes.append(
                ClaimOutcome(
                    kind=kind,
                    schedule=hash_to_schedule(sched_dict),
                    occurrence=hash_to_occurrence(occ_dict),
                    reason=reason_raw,
                )
            )
        return outcomes

    async def confirm_dispatched(
        self,
        occurrence_id: str,
        *,
        job_id: str,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        client = await self.get_client()
        now_iso = _require_utc_iso(now)
        res = await self._eval_lua(
            client,
            CONFIRM_DISPATCHED_LUA,
            2,
            self._occurrence_key(occurrence_id),
            self._pending_key(),
            self.prefix,
            occurrence_id,
            job_id,
            now_iso,
        )
        parsed = flat_list_to_dict(res)
        return hash_to_occurrence(parsed) if parsed else None

    async def reclaim_pending_dispatches(
        self,
        *,
        now: datetime,
        reclaimed_by: str,
        dispatch_lease_seconds: float,
        max_dispatch_attempts: int,
        limit: int = 50,
    ) -> tuple[list[ScheduleOccurrence], list[ScheduleOccurrence]]:
        if limit <= 0:
            return [], []

        client = await self.get_client()
        now_ts = now.timestamp()
        now_iso = _require_utc_iso(now)
        next_lease_until = now_ts + float(dispatch_lease_seconds)

        res = await self._eval_lua(
            client,
            RECLAIM_PENDING_LUA,
            1,
            self._pending_key(),
            self.prefix,
            repr(now_ts),
            now_iso,
            reclaimed_by,
            repr(next_lease_until),
            str(int(max_dispatch_attempts)),
            str(int(limit)),
            str(self.retention_seconds),
        )
        to_retry: list[ScheduleOccurrence] = []
        exhausted: list[ScheduleOccurrence] = []
        if not isinstance(res, list):
            return to_retry, exhausted

        for item in res:
            if not isinstance(item, list) or len(item) < 2:
                continue
            tag = str(item[0])
            parsed = flat_list_to_dict(item[1])
            if not parsed:
                continue
            occ = hash_to_occurrence(parsed)
            if tag == "exhausted":
                exhausted.append(occ)
            else:
                to_retry.append(occ)
        return to_retry, exhausted

    async def mark_dispatch_attempt_failed(
        self,
        occurrence_id: str,
        *,
        error: str,
        now: datetime,
        max_dispatch_attempts: int,
    ) -> ScheduleOccurrence | None:
        client = await self.get_client()
        now_iso = _require_utc_iso(now)
        res = await self._eval_lua(
            client,
            MARK_DISPATCH_FAILED_LUA,
            2,
            self._occurrence_key(occurrence_id),
            self._pending_key(),
            self.prefix,
            occurrence_id,
            error,
            repr(now.timestamp()),
            now_iso,
            str(int(max_dispatch_attempts)),
            str(self.retention_seconds),
        )
        parsed = flat_list_to_dict(res)
        return hash_to_occurrence(parsed) if parsed else None

    async def mark_running(
        self,
        occurrence_id: str,
        *,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        client = await self.get_client()
        now_iso = _require_utc_iso(now)
        res = await self._eval_lua(
            client,
            MARK_RUNNING_LUA,
            2,
            self._occurrence_key(occurrence_id),
            self._pending_key(),
            self.prefix,
            occurrence_id,
            now_iso,
        )
        parsed = flat_list_to_dict(res)
        return hash_to_occurrence(parsed) if parsed else None

    async def mark_completed(
        self,
        occurrence_id: str,
        *,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        client = await self.get_client()
        now_iso = _require_utc_iso(now)
        res = await self._eval_lua(
            client,
            MARK_COMPLETED_LUA,
            2,
            self._occurrence_key(occurrence_id),
            self._pending_key(),
            self.prefix,
            occurrence_id,
            now_iso,
            str(self.retention_seconds),
        )
        parsed = flat_list_to_dict(res)
        return hash_to_occurrence(parsed) if parsed else None

    async def mark_failed(
        self,
        occurrence_id: str,
        *,
        error: str,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        client = await self.get_client()
        now_iso = _require_utc_iso(now)
        res = await self._eval_lua(
            client,
            MARK_FAILED_LUA,
            2,
            self._occurrence_key(occurrence_id),
            self._pending_key(),
            self.prefix,
            occurrence_id,
            error,
            now_iso,
            str(self.retention_seconds),
        )
        parsed = flat_list_to_dict(res)
        return hash_to_occurrence(parsed) if parsed else None

    async def release_overlap_lock(self, lock_key: str, lock_token: str) -> bool:
        if not lock_key or not lock_token:
            return False
        client = await self.get_client()
        res = await self._eval_lua(
            client,
            RELEASE_OVERLAP_LOCK_LUA,
            1,
            lock_key,
            lock_token,
        )
        if isinstance(res, (int, float)):
            return int(res) > 0
        return bool(res)

    async def extend_overlap_lock(
        self, lock_key: str, lock_token: str, ttl_seconds: float
    ) -> bool:
        if not lock_key or not lock_token or ttl_seconds <= 0:
            return False
        client = await self.get_client()
        res = await self._eval_lua(
            client,
            EXTEND_OVERLAP_LOCK_LUA,
            1,
            lock_key,
            lock_token,
            str(int(ttl_seconds * 1000)),
        )
        if isinstance(res, (int, float)):
            return int(res) > 0
        return bool(res)

    async def next_due_at(self) -> datetime | None:
        client = await self.get_client()
        raw: object = await client.zrange(self._due_key(), 0, 0, withscores=True)
        if not isinstance(raw, list) or not raw:
            return None
        first = raw[0]
        if not isinstance(first, (tuple, list)) or len(first) < 2:
            return None
        return datetime.fromtimestamp(float(first[1]), UTC)

    async def get_occurrence(self, occurrence_id: str) -> ScheduleOccurrence | None:
        client = await self.get_client()
        raw: object = await client.hgetall(self._occurrence_key(occurrence_id))
        parsed = flat_list_to_dict(raw)
        return hash_to_occurrence(parsed) if parsed else None

    async def close(self) -> None:
        """Close the Redis client and connection pool if owned by this store."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._owns_pool and self._pool is not None:
            await self._pool.aclose()
            self._pool = None
        self._script_shas.clear()

    async def aclose(self) -> None:
        """Alias for close()."""
        await self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


ActiveStoreResolver = Callable[[], "ScheduleStore | None"]
_active_store_resolver: ActiveStoreResolver | None = None


def set_active_store_resolver(resolver: ActiveStoreResolver | None) -> None:
    """Register the active container-aware ScheduleStore resolver used by QueueWorker."""
    global _active_store_resolver
    _active_store_resolver = resolver


def resolve_active_schedule_store() -> ScheduleStore | None:
    """Return the active container or singleton ScheduleStore, if initialized."""
    return _active_store_resolver() if _active_store_resolver is not None else None


__all__ = [
    "ActiveStoreResolver",
    "ClaimOutcome",
    "MemoryScheduleStore",
    "NextRunCalculator",
    "RedisScheduleStore",
    "ScheduleStore",
    "resolve_active_schedule_store",
    "set_active_store_resolver",
]
