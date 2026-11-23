from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import math
import signal
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, ClassVar, Literal, Protocol, Self

from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import get_container
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job, is_trusted_class_path, parse_class_path
from swiftpy.scheduling.cron import (
    normalize_schedule_datetime,
    parse_cron,
    resolve_timezone,
)
from swiftpy.scheduling.events import (
    ScheduledTaskDispatched,
    ScheduledTaskDue,
    ScheduledTaskFailed,
    ScheduledTaskSkipped,
    ScheduleRegistered,
    dispatch_scheduler_event,
)
from swiftpy.scheduling.exceptions import (
    InvalidCronError,
    InvalidScheduleError,
    ScheduleNotFoundError,
    ScheduleSignatureError,
)
from swiftpy.scheduling.jobs import (
    ScheduledCallableJob,
    TaskCallable,
    get_callable_resolver,
    set_callable_resolver,
)
from swiftpy.scheduling.models import (
    MisfirePolicy,
    OccurrenceStatus,
    OverlapPolicy,
    ScheduleOccurrence,
    ScheduleRecord,
    TriggerType,
    utc_now,
    validate_json_arguments,
)
from swiftpy.scheduling.signing import sign_schedule_fields, verify_schedule_signature
from swiftpy.scheduling.store import (
    ClaimOutcome,
    MemoryScheduleStore,
    RedisScheduleStore,
    ScheduleStore,
    set_active_store_resolver,
)

logger = logging.getLogger("swiftpy.scheduling")

_DOW_INPUT_MAP: dict[str, int] = {
    "sun": 0,
    "sunday": 0,
    "mon": 1,
    "monday": 1,
    "tue": 2,
    "tuesday": 2,
    "wed": 3,
    "wednesday": 3,
    "thu": 4,
    "thursday": 4,
    "fri": 5,
    "friday": 5,
    "sat": 6,
    "saturday": 6,
}


class QueuePusher(Protocol):
    """Minimal structural protocol matching ``QueueDispatcher.push()``."""

    async def push(
        self,
        job: Job,
        queue: str | None = None,
        delay: float = 0,
    ) -> str: ...


TaskTarget = type[Job] | Job | str | TaskCallable


def _validate_non_empty_str(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidScheduleError(f"{field} must be a non-empty string.")
    return value.strip()


def _parse_positive_finite(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidScheduleError(f"{field} must be a positive finite number.")
    num = float(value)
    if not math.isfinite(num) or num <= 0:
        raise InvalidScheduleError(f"{field} must be > 0 and finite, got {value!r}.")
    return num


def _parse_non_negative_finite(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidScheduleError(f"{field} must be a non-negative finite number.")
    num = float(value)
    if not math.isfinite(num) or num < 0:
        raise InvalidScheduleError(f"{field} must be >= 0 and finite, got {value!r}.")
    return num


def _parse_positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidScheduleError(f"{field} must be a positive integer.")
    return value


def _parse_hh_mm(time_str: str) -> tuple[int, int]:
    if not isinstance(time_str, str):
        raise InvalidCronError(f"Time string must be 'HH:MM', got {time_str!r}.")
    parts = time_str.strip().split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise InvalidCronError(
            f"Invalid time '{time_str}': expected strict 'HH:MM' 24-hour format."
        )
    hour, minute = int(parts[0]), int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise InvalidCronError(
            f"Time '{time_str}' out of bounds (hour 0..23, minute 0..59)."
        )
    return hour, minute


def _parse_dow_specifier(day_of_week: int | str) -> int:
    if isinstance(day_of_week, bool):
        raise InvalidCronError("Day of week cannot be a boolean.")
    if isinstance(day_of_week, int):
        if day_of_week < 0 or day_of_week > 7:
            raise InvalidCronError(
                f"Weekday integer {day_of_week} out of range (0/7=Sun, 1=Mon..6=Sat)."
            )
        return 0 if day_of_week == 7 else day_of_week
    if isinstance(day_of_week, str):
        cleaned = day_of_week.strip().lower()
        if cleaned in _DOW_INPUT_MAP:
            return _DOW_INPUT_MAP[cleaned]
        if cleaned.isdigit():
            return _parse_dow_specifier(int(cleaned))
    raise InvalidCronError(f"Invalid day_of_week specifier: {day_of_week!r}.")


def _derive_lock_ttl_from_job(job: Job, default_lock_ttl: int) -> int:
    if job.timeout is None:
        return default_lock_ttl
    tries = max(1, int(job.tries))
    backoff_budget = 0.0
    if isinstance(job.backoff, (int, float)) and not isinstance(job.backoff, bool):
        backoff_budget = max(0.0, float(job.backoff)) * max(0, tries - 1)
    elif isinstance(job.backoff, Sequence) and not isinstance(
        job.backoff, (str, bytes)
    ):
        for attempt_idx in range(max(0, tries - 1)):
            if job.backoff:
                val = job.backoff[min(attempt_idx, len(job.backoff) - 1)]
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    backoff_budget += max(0.0, float(val))
    derived = math.ceil(float(job.timeout) * tries + backoff_budget + 60.0)
    return max(60, derived)


def _instantiate_job_class(
    job_class_str: str,
    args: list[Any],
    kwargs: dict[str, Any],
) -> Job:
    module_name, class_name = parse_class_path(
        job_class_str, exc_cls=InvalidScheduleError
    )
    if not is_trusted_class_path(
        job_class_str,
        module_name,
        trusted_namespaces=Job.get_trusted_namespaces(),
        trusted_classes=Job._trusted_classes,
        class_name=class_name,
    ):
        raise InvalidScheduleError(
            f"Job class '{job_class_str}' from module '{module_name}' is not in trusted namespaces."
        )

    module = importlib.import_module(module_name)
    target_cls: Any = module
    for part in class_name.split("."):
        target_cls = getattr(target_cls, part, None)
        if target_cls is None:
            raise InvalidScheduleError(
                f"Job class '{class_name}' not found in module '{module_name}'."
            )

    if not isinstance(target_cls, type) or not issubclass(target_cls, Job):
        raise InvalidScheduleError(
            f"Resolved target '{job_class_str}' is not a subclass of Job."
        )

    job_inst = target_cls(*args, **kwargs)
    if not isinstance(job_inst, Job):
        raise InvalidScheduleError(
            f"Resolved target '{job_class_str}' did not produce a Job instance."
        )
    return job_inst


def _validate_callable_binding(
    fn: TaskCallable,
    args: list[Any],
    kwargs: dict[str, Any],
    *,
    label: str,
) -> None:
    try:
        inspect.signature(fn).bind(*args, **kwargs)
    except (TypeError, ValueError) as exc:
        raise InvalidScheduleError(
            f"Arguments do not match callable signature for '{label}': {exc}"
        ) from exc


class IntervalScheduleBuilder:
    """Intermediate interval builder enforcing explicit time units before ``.run()``."""

    def __init__(self, scheduler: Scheduler, count: float) -> None:
        self._scheduler = scheduler
        self._count = _parse_positive_finite(count, field="interval")

    def seconds(self) -> ScheduleBuilder:
        return ScheduleBuilder(
            self._scheduler,
            trigger=TriggerType.INTERVAL,
            interval_seconds=self._count,
        )

    def minutes(self) -> ScheduleBuilder:
        return ScheduleBuilder(
            self._scheduler,
            trigger=TriggerType.INTERVAL,
            interval_seconds=self._count * 60.0,
        )

    def hours(self) -> ScheduleBuilder:
        return ScheduleBuilder(
            self._scheduler,
            trigger=TriggerType.INTERVAL,
            interval_seconds=self._count * 3600.0,
        )

    def days(self) -> ScheduleBuilder:
        return ScheduleBuilder(
            self._scheduler,
            trigger=TriggerType.INTERVAL,
            interval_seconds=self._count * 86400.0,
        )


class ScheduleBuilder:
    """Fluent, strictly typed schedule builder validating options eagerly."""

    def __init__(
        self,
        scheduler: Scheduler,
        *,
        trigger: TriggerType,
        expression: str | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._trigger = trigger
        self._expression: str | None = None
        self._interval_seconds: float | None = None

        if trigger == TriggerType.CRON:
            if expression is None:
                raise InvalidCronError("Cron trigger requires a cron expression.")
            parsed = parse_cron(expression)
            self._expression = parsed.normalized
        elif trigger == TriggerType.INTERVAL:
            self._interval_seconds = _parse_positive_finite(
                interval_seconds, field="interval_seconds"
            )
        else:
            raise InvalidScheduleError(
                f"ScheduleBuilder only builds recurring triggers, got {trigger!r}."
            )

        self._timezone: str = scheduler.default_timezone
        self._name: str | None = None
        self._queue: str | None = None
        self._overlap_policy: OverlapPolicy = OverlapPolicy.ALLOW
        self._misfire_policy: MisfirePolicy = MisfirePolicy.SKIP
        self._lock_ttl: int | None = None
        self._grace_seconds: float | None = None
        self._upsert: bool = False

    def timezone(self, tz: str | tzinfo) -> Self:
        """Evaluate cron schedule times in the given IANA timezone."""
        _, tz_name = resolve_timezone(tz)
        self._timezone = tz_name
        return self

    def name(self, name: str) -> Self:
        """Assign an explicit unique identifier name to the schedule."""
        self._name = _validate_non_empty_str(name, field="name")
        return self

    def queue(self, queue: str) -> Self:
        """Set the target QueueDispatcher queue name."""
        self._queue = _validate_non_empty_str(queue, field="queue")
        return self

    def without_overlapping(self, lock_ttl: int | None = None) -> Self:
        """Skip due occurrences while an earlier occurrence lock is still held."""
        self._overlap_policy = OverlapPolicy.SKIP
        if lock_ttl is not None:
            self._lock_ttl = _parse_positive_int(lock_ttl, field="lock_ttl")
        return self

    def allow_overlapping(self) -> Self:
        """Allow overlapping execution across ticks (default)."""
        self._overlap_policy = OverlapPolicy.ALLOW
        self._lock_ttl = None
        return self

    def on_misfire(self, policy: MisfirePolicy | str) -> Self:
        """Set catch-up policy (``'skip'`` or ``'fire_once'``) after downtime."""
        try:
            self._misfire_policy = MisfirePolicy(str(policy))
        except ValueError as exc:
            raise InvalidScheduleError(f"Invalid misfire policy: {policy!r}") from exc
        return self

    def grace(self, seconds: float) -> Self:
        """Set grace window in seconds before a delayed tick is treated as misfired."""
        self._grace_seconds = _parse_non_negative_finite(
            seconds, field="grace_seconds"
        )
        return self

    def upsert(self, enabled: bool = True) -> Self:
        """Enable state-preserving idempotent updates when syncing boot schedules."""
        self._upsert = bool(enabled)
        return self

    async def run(
        self,
        task: TaskTarget,
        *args: Any,
        **kwargs: Any,
    ) -> ScheduleRecord:
        """Validate, compute initial due time, persist, and return the ScheduleRecord."""
        return await self._scheduler._register_recurring(
            trigger=self._trigger,
            expression=self._expression,
            interval_seconds=self._interval_seconds,
            timezone=self._timezone,
            name=self._name,
            queue=self._queue,
            overlap_policy=self._overlap_policy,
            misfire_policy=self._misfire_policy,
            lock_ttl=self._lock_ttl,
            grace_seconds=self._grace_seconds,
            upsert=self._upsert,
            task=task,
            args=args,
            kwargs=kwargs,
        )


def _schedule_app_env(config: Config) -> str:
    return str(config.get("app.env") or config.get("env") or "local").strip().lower()


def ensure_memory_driver_allowed(config: Config) -> None:
    """Reject the process-local memory schedule driver in production unless explicitly allowed."""
    if _schedule_app_env(config) in ("production", "prod") and not bool(
        config.get("schedule.allow_memory_in_production", False)
    ):
        raise ConfigurationError(
            "Schedule driver 'memory' is process-local and non-durable; configure "
            "schedule.driver='redis' in production or set "
            "schedule.allow_memory_in_production=True explicitly."
        )


class Scheduler:
    """
    Framework-native task scheduler that determines WHEN work is due and dispatches
    execution through SwiftPY's Queue infrastructure (``QueueDispatcher`` + ``QueueWorker``).
    """

    _instance: ClassVar[Scheduler | None] = None

    def __init__(
        self,
        *,
        store: ScheduleStore | None = None,
        dispatcher: QueuePusher | None = None,
        default_timezone: str | tzinfo = "UTC",
        grace_seconds: float = 60.0,
        dispatch_lease_seconds: float = 30.0,
        max_dispatch_attempts: int = 5,
        default_lock_ttl: int = 900,
        poll_interval: float = 1.0,
        clock: Callable[[], datetime] = utc_now,
        scheduler_id: str | None = None,
    ) -> None:
        _, tz_name = resolve_timezone(default_timezone)
        self.default_timezone: str = tz_name
        self.grace_seconds: float = _parse_non_negative_finite(
            grace_seconds, field="grace_seconds"
        )
        self.dispatch_lease_seconds: float = _parse_positive_finite(
            dispatch_lease_seconds, field="dispatch_lease_seconds"
        )
        self.max_dispatch_attempts: int = _parse_positive_int(
            max_dispatch_attempts, field="max_dispatch_attempts"
        )
        self.default_lock_ttl: int = _parse_positive_int(
            default_lock_ttl, field="default_lock_ttl"
        )
        self.poll_interval: float = _parse_positive_finite(
            poll_interval, field="poll_interval"
        )
        self._clock = clock
        self.scheduler_id: str = scheduler_id or uuid.uuid4().hex[:12]
        self.store: ScheduleStore = (
            store if store is not None else MemoryScheduleStore(clock=clock)
        )
        self._dispatcher: QueuePusher | None = dispatcher
        self._callables: dict[str, TaskCallable] = {}
        self._callable_names: dict[TaskCallable, str] = {}
        self._wake_event: asyncio.Event = asyncio.Event()
        self._tick_lock: asyncio.Lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping: bool = False

    @classmethod
    def get_instance(cls) -> Scheduler:
        """Return the active container's Scheduler singleton, or fallback instance."""
        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
            cfg = container.resolve(Config) if container.has(Config) else None
            inst = cls._from_optional_config(cfg)
            container.instance(cls, inst)
            return inst
        except RuntimeError:
            pass

        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def _get_existing_instance(cls) -> Scheduler | None:
        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
        except RuntimeError:
            pass
        return cls._instance

    @classmethod
    def set_instance(cls, scheduler_inst: Scheduler | None) -> None:
        """Override or reset the Scheduler singleton in active container and fallback."""
        cls._instance = scheduler_inst
        try:
            container = get_container()
            if container.has(cls):
                if scheduler_inst is not None:
                    container.instance(cls, scheduler_inst)
                else:
                    container.singleton(
                        cls,
                        lambda c: cls._from_optional_config(
                            c.resolve(Config) if c.has(Config) else None
                        ),
                    )
        except RuntimeError:
            pass

    @classmethod
    def from_config(
        cls,
        config: Config | None = None,
        *,
        dispatcher: QueuePusher | None = None,
        store: ScheduleStore | None = None,
    ) -> Scheduler:
        """Instantiate a ``Scheduler`` cleanly from ``Config`` and optional runtime overrides."""
        return cls._from_optional_config(
            config,
            dispatcher=dispatcher,
            store=store,
        )

    @classmethod
    def _from_optional_config(
        cls,
        config: Config | None,
        *,
        dispatcher: QueuePusher | None = None,
        store: ScheduleStore | None = None,
    ) -> Scheduler:
        if config is None:
            return cls(dispatcher=dispatcher, store=store)
        raw_tz = config.get("schedule.timezone") or config.get("app.timezone") or "UTC"
        grace = config.get("schedule.grace_seconds", 60.0)
        lease = config.get("schedule.dispatch_lease_seconds", 30.0)
        attempts = config.get("schedule.max_dispatch_attempts", 5)
        lock_ttl = config.get("schedule.default_lock_ttl", 900)
        poll_interval = config.get("schedule.poll_interval", 1.0)

        resolved_store = store
        if resolved_store is None:
            app_env = _schedule_app_env(config)
            raw_driver = config.get("schedule.driver")
            default_driver = "redis" if app_env in ("production", "prod") else "memory"
            driver = (
                str(raw_driver).strip().lower()
                if raw_driver is not None
                else default_driver
            )
            prefix_raw = config.get("schedule.prefix")
            if driver == "redis":
                retention_raw = config.get(
                    "schedule.retention_seconds", 7 * 24 * 3600
                )
                history_raw = config.get("schedule.max_occurrences_per_schedule", 200)
                db_raw = config.get("schedule.database")
                resolved_store = RedisScheduleStore(
                    database=int(db_raw) if db_raw is not None else None,
                    prefix=str(prefix_raw) if prefix_raw else None,
                    retention_seconds=int(
                        retention_raw if retention_raw is not None else 7 * 24 * 3600
                    ),
                    max_occurrences_per_schedule=int(
                        history_raw if history_raw is not None else 200
                    ),
                    clock=utc_now,
                )
            elif driver == "memory":
                ensure_memory_driver_allowed(config)
                resolved_store = MemoryScheduleStore(
                    clock=utc_now,
                    prefix=str(prefix_raw) if prefix_raw else "swiftpy_schedule:",
                )
            else:
                raise InvalidScheduleError(
                    f"Unsupported schedule driver '{raw_driver}': expected 'redis' or 'memory'."
                )

        return cls(
            store=resolved_store,
            dispatcher=dispatcher,
            default_timezone=str(raw_tz),
            grace_seconds=float(grace if grace is not None else 60.0),
            dispatch_lease_seconds=float(lease if lease is not None else 30.0),
            max_dispatch_attempts=int(attempts if attempts is not None else 5),
            default_lock_ttl=int(lock_ttl if lock_ttl is not None else 900),
            poll_interval=float(poll_interval if poll_interval is not None else 1.0),
        )

    def _now(self) -> datetime:
        current = self._clock()
        tz, _ = resolve_timezone("UTC")
        if current.tzinfo is None:
            return current.replace(tzinfo=tz)
        return current.astimezone(tz)

    def _resolve_dispatcher(self) -> QueuePusher:
        if self._dispatcher is not None:
            return self._dispatcher
        try:
            container = get_container()
            if container.has(QueueDispatcher):
                return container.resolve(QueueDispatcher)
        except RuntimeError:
            pass
        return QueueDispatcher.get_instance()

    # ------------------------------------------------------------------
    # Task callable registration
    # ------------------------------------------------------------------

    def register_task(self, name: str, fn: TaskCallable) -> TaskCallable:
        """Register a named callable for safe queued dispatch via ``ScheduledCallableJob``."""
        clean_name = _validate_non_empty_str(name, field="task_name")
        if not callable(fn):
            raise InvalidScheduleError(f"Task handler for '{clean_name}' must be callable.")
        if get_callable_resolver() is None:
            set_callable_resolver(_resolve_callable_from_active)
        self._callables[clean_name] = fn
        self._callable_names[fn] = clean_name
        return fn

    def task(
        self, name: str | None = None
    ) -> Callable[[TaskCallable], TaskCallable]:
        """Decorator registering a sync or async callable as a named scheduled task."""

        def _decorator(fn: TaskCallable) -> TaskCallable:
            resolved_name = (
                _validate_non_empty_str(name, field="task_name")
                if name is not None
                else f"{fn.__module__}:{fn.__qualname__}"
            )
            return self.register_task(resolved_name, fn)

        return _decorator

    def get_registered_task(self, name: str) -> TaskCallable | None:
        """Return a callable previously registered under ``name``, or ``None``."""
        return self._callables.get(name)

    # ------------------------------------------------------------------
    # Target & next-run helpers
    # ------------------------------------------------------------------

    def _resolve_target_spec(
        self,
        task: TaskTarget,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any] | None,
        queue_override: str | None,
        lock_ttl_override: int | None,
    ) -> tuple[
        str,
        Literal["job", "callable"],
        list[Any],
        dict[str, Any],
        str,
        int | None,
    ]:
        kwargs_dict = dict(kwargs) if kwargs is not None else {}
        args_list = list(args)

        if isinstance(task, Job):
            if args_list or kwargs_dict:
                raise InvalidScheduleError(
                    "Cannot pass extra args or kwargs when scheduling an already-instantiated Job."
                )
            task_args = list(task.args)
            task_kwargs = dict(task.kwargs)
            validate_json_arguments(task_args, task_kwargs)
            task_name = f"{task.__class__.__module__}:{task.__class__.__qualname__}"
            target_queue = queue_override or task.queue or "default"
            derived_ttl = (
                lock_ttl_override
                if lock_ttl_override is not None
                else (
                    _derive_lock_ttl_from_job(task, self.default_lock_ttl)
                    if task.timeout is not None
                    else None
                )
            )
            return task_name, "job", task_args, task_kwargs, target_queue, derived_ttl

        if isinstance(task, type) and issubclass(task, Job):
            validate_json_arguments(args_list, kwargs_dict)
            try:
                sample_job = task(*args_list, **kwargs_dict)
            except (TypeError, ValueError) as exc:
                raise InvalidScheduleError(
                    f"Cannot instantiate Job '{task.__name__}' with provided arguments: {exc}"
                ) from exc
            task_name = f"{task.__module__}:{task.__qualname__}"
            target_queue = queue_override or sample_job.queue or "default"
            derived_ttl = (
                lock_ttl_override
                if lock_ttl_override is not None
                else (
                    _derive_lock_ttl_from_job(sample_job, self.default_lock_ttl)
                    if sample_job.timeout is not None
                    else None
                )
            )
            return task_name, "job", args_list, kwargs_dict, target_queue, derived_ttl

        if isinstance(task, str):
            task_name = _validate_non_empty_str(task, field="task")
            registered = self._callables.get(task_name)
            if registered is None:
                raise InvalidScheduleError(
                    f"Task name '{task_name}' is not registered. Register it via scheduler.task('{task_name}')."
                )
            validate_json_arguments(args_list, kwargs_dict)
            _validate_callable_binding(registered, args_list, kwargs_dict, label=task_name)
            return (
                task_name,
                "callable",
                args_list,
                kwargs_dict,
                queue_override or "default",
                lock_ttl_override,
            )

        if callable(task):
            reg_name = self._callable_names.get(task)
            if reg_name is None:
                raise InvalidScheduleError(
                    "Unregistered callables cannot be scheduled directly; decorate with @scheduler.task() "
                    "or pass a swiftpy.queue.Job subclass/instance."
                )
            validate_json_arguments(args_list, kwargs_dict)
            _validate_callable_binding(task, args_list, kwargs_dict, label=reg_name)
            return (
                reg_name,
                "callable",
                args_list,
                kwargs_dict,
                queue_override or "default",
                lock_ttl_override,
            )

        raise InvalidScheduleError(
            f"Unsupported schedule target of type {type(task).__name__}: expected Job subclass, Job instance, "
            "or registered task callable."
        )

    def _compute_next_run(
        self, record: ScheduleRecord, after: datetime
    ) -> datetime | None:
        if record.trigger == TriggerType.ONCE:
            return None
        if record.trigger == TriggerType.INTERVAL:
            if record.interval_seconds is None or record.interval_seconds <= 0:
                raise InvalidScheduleError(
                    f"Interval schedule '{record.id}' missing valid interval_seconds."
                )
            base_utc = (
                after.replace(tzinfo=UTC)
                if after.tzinfo is None
                else after.astimezone(UTC)
            )
            return base_utc + timedelta(seconds=record.interval_seconds)
        if record.trigger == TriggerType.CRON:
            if not record.expression:
                raise InvalidCronError(
                    f"Cron schedule '{record.id}' missing expression."
                )
            return parse_cron(record.expression).next_run_after(
                after, record.timezone
            )
        raise InvalidScheduleError(f"Unsupported trigger type: {record.trigger!r}")

    def _build_job_for_occurrence(self, occ: ScheduleOccurrence) -> Job:
        verify_schedule_signature(
            occ.signature,
            task_kind=occ.task_kind,
            task_name=occ.task_name,
            args=occ.args,
            kwargs=occ.kwargs,
            queue=occ.queue,
            label=occ.schedule_id,
        )
        if occ.task_kind == "callable":
            job: Job = ScheduledCallableJob(
                task_name=occ.task_name,
                task_args=list(occ.args),
                task_kwargs=dict(occ.kwargs),
            )
        else:
            job = _instantiate_job_class(
                occ.task_name, list(occ.args), dict(occ.kwargs)
            )

        job.queue = occ.queue or job.queue or "default"
        job.data = dict(job.data)
        job.data["_swiftpy_schedule_id"] = occ.schedule_id
        job.data["_swiftpy_schedule_occurrence"] = occ.occurrence_id
        prefix = getattr(self.store, "prefix", None)
        if isinstance(prefix, str) and prefix:
            job.data["_swiftpy_schedule_prefix"] = prefix
        if occ.lock_key is not None and occ.lock_token is not None:
            job.data["_swiftpy_schedule_lock_key"] = occ.lock_key
            job.data["_swiftpy_schedule_lock_token"] = occ.lock_token
            if occ.lock_ttl is not None:
                job.data["_swiftpy_schedule_lock_ttl"] = occ.lock_ttl
        return job

    @staticmethod
    def _sign_record(record: ScheduleRecord) -> None:
        record.signature = sign_schedule_fields(
            task_kind=record.task_kind,
            task_name=record.task_name,
            args=record.args,
            kwargs=record.kwargs,
            queue=record.queue,
        )

    async def _register_recurring(
        self,
        *,
        trigger: TriggerType,
        expression: str | None,
        interval_seconds: float | None,
        timezone: str,
        name: str | None,
        queue: str | None,
        overlap_policy: OverlapPolicy,
        misfire_policy: MisfirePolicy,
        lock_ttl: int | None,
        grace_seconds: float | None,
        upsert: bool,
        task: TaskTarget,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> ScheduleRecord:
        now = self._now()
        (
            task_name,
            task_kind,
            args_list,
            kwargs_dict,
            target_queue,
            effective_lock_ttl,
        ) = self._resolve_target_spec(task, args, kwargs, queue, lock_ttl)

        schedule_id = name if name is not None else uuid.uuid4().hex
        record = ScheduleRecord(
            id=schedule_id,
            name=name,
            trigger=trigger,
            expression=expression,
            interval_seconds=interval_seconds,
            timezone=timezone,
            task_name=task_name,
            task_kind=task_kind,
            args=args_list,
            kwargs=kwargs_dict,
            queue=target_queue,
            enabled=True,
            overlap_policy=overlap_policy,
            misfire_policy=misfire_policy,
            lock_ttl=effective_lock_ttl,
            grace_seconds=grace_seconds,
            created_at=now,
            updated_at=now,
        )
        record.next_run_at = self._compute_next_run(record, now)
        self._sign_record(record)

        saved, was_upserted = await self.store.save_schedule(record, upsert=upsert)
        self._wake_event.set()
        await dispatch_scheduler_event(
            ScheduleRegistered(schedule=saved, upserted=was_upserted)
        )
        return saved

    # ------------------------------------------------------------------
    # Public fluent & direct scheduling API
    # ------------------------------------------------------------------

    def every(self, interval: float) -> IntervalScheduleBuilder:
        """Schedule a recurring interval task (chained with `.seconds()`, `.minutes()`, `.hours()`, `.days()`)."""
        return IntervalScheduleBuilder(self, interval)

    def cron(self, expression: str) -> ScheduleBuilder:
        """Schedule a recurring task using a validated 5-field cron expression or macro."""
        return ScheduleBuilder(self, trigger=TriggerType.CRON, expression=expression)

    def every_minute(self) -> ScheduleBuilder:
        """Schedule a task every minute (`* * * * *`)."""
        return self.cron("* * * * *")

    def hourly(self) -> ScheduleBuilder:
        """Schedule a task at minute 0 of every hour (`0 * * * *`)."""
        return self.cron("0 * * * *")

    def hourly_at(self, minute: int = 0) -> ScheduleBuilder:
        """Schedule a task once per hour at ``minute`` (0..59)."""
        if isinstance(minute, bool) or not isinstance(minute, int) or minute < 0 or minute > 59:
            raise InvalidCronError(
                f"hourly_at minute must be an integer between 0 and 59, got {minute!r}."
            )
        return self.cron(f"{minute} * * * *")

    def daily(self) -> ScheduleBuilder:
        """Schedule a task once per day at midnight (`0 0 * * *`)."""
        return self.cron("0 0 * * *")

    def daily_at(self, time_str: str = "00:00") -> ScheduleBuilder:
        """Schedule a task daily at strict ``HH:MM`` 24-hour time."""
        hour, minute = _parse_hh_mm(time_str)
        return self.cron(f"{minute} {hour} * * *")

    def weekly(self) -> ScheduleBuilder:
        """Schedule a task weekly on Sunday at midnight (`0 0 * * 0`)."""
        return self.cron("0 0 * * 0")

    def weekly_at(
        self, day_of_week: int | str = 1, time_str: str = "00:00"
    ) -> ScheduleBuilder:
        """Schedule a task weekly on ``day_of_week`` (0/7=Sun, 1=Mon..6=Sat or name) at ``HH:MM``."""
        dow = _parse_dow_specifier(day_of_week)
        hour, minute = _parse_hh_mm(time_str)
        return self.cron(f"{minute} {hour} * * {dow}")

    def monthly(self) -> ScheduleBuilder:
        """Schedule a task on the 1st of every month at midnight (`0 0 1 * *`)."""
        return self.cron("0 0 1 * *")

    def monthly_at(self, day: int = 1, time_str: str = "00:00") -> ScheduleBuilder:
        """Schedule a task monthly on ``day`` (1..31) at ``HH:MM``."""
        if isinstance(day, bool) or not isinstance(day, int) or day < 1 or day > 31:
            raise InvalidCronError(
                f"monthly_at day must be an integer between 1 and 31, got {day!r}."
            )
        hour, minute = _parse_hh_mm(time_str)
        return self.cron(f"{minute} {hour} {day} * *")

    async def schedule(
        self,
        task: TaskTarget,
        *,
        at: datetime,
        timezone: str | tzinfo | None = None,
        name: str | None = None,
        queue: str | None = None,
        overlap_policy: OverlapPolicy | str = OverlapPolicy.ALLOW,
        misfire_policy: MisfirePolicy | str = MisfirePolicy.SKIP,
        lock_ttl: int | None = None,
        grace_seconds: float | None = None,
        allow_past: bool = False,
        upsert: bool = False,
        args: Sequence[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
    ) -> ScheduleRecord:
        """
        Register a one-time scheduled task for ``at``.

        Never dispatches onto the Queue until ``now >= at``.
        """
        now = self._now()
        default_tz = timezone if timezone is not None else self.default_timezone
        at_utc, resolved_tz = normalize_schedule_datetime(at, default_tz)
        if not allow_past and at_utc < now:
            raise InvalidScheduleError(
                f"Scheduled execution datetime {at_utc.isoformat()} is in the past "
                f"(now={now.isoformat()}). Pass allow_past=True to allow immediate execution."
            )

        try:
            resolved_overlap = OverlapPolicy(str(overlap_policy))
            resolved_misfire = MisfirePolicy(str(misfire_policy))
        except ValueError as exc:
            raise InvalidScheduleError(f"Invalid schedule policy: {exc}") from exc

        resolved_lock_ttl: int | None = None
        if lock_ttl is not None:
            resolved_lock_ttl = _parse_positive_int(lock_ttl, field="lock_ttl")

        resolved_grace: float | None = None
        if grace_seconds is not None:
            resolved_grace = _parse_non_negative_finite(
                grace_seconds, field="grace_seconds"
            )

        clean_name = (
            _validate_non_empty_str(name, field="name") if name is not None else None
        )
        clean_queue = (
            _validate_non_empty_str(queue, field="queue") if queue is not None else None
        )

        (
            task_name,
            task_kind,
            args_list,
            kwargs_dict,
            target_queue,
            effective_lock_ttl,
        ) = self._resolve_target_spec(
            task, tuple(args), kwargs, clean_queue, resolved_lock_ttl
        )

        schedule_id = clean_name if clean_name is not None else uuid.uuid4().hex
        record = ScheduleRecord(
            id=schedule_id,
            name=clean_name,
            trigger=TriggerType.ONCE,
            run_at=at_utc,
            timezone=resolved_tz,
            task_name=task_name,
            task_kind=task_kind,
            args=args_list,
            kwargs=kwargs_dict,
            queue=target_queue,
            enabled=True,
            overlap_policy=resolved_overlap,
            misfire_policy=resolved_misfire,
            lock_ttl=effective_lock_ttl,
            grace_seconds=resolved_grace,
            next_run_at=at_utc,
            created_at=now,
            updated_at=now,
        )
        self._sign_record(record)

        saved, was_upserted = await self.store.save_schedule(record, upsert=upsert)
        self._wake_event.set()
        await dispatch_scheduler_event(
            ScheduleRegistered(schedule=saved, upserted=was_upserted)
        )
        return saved

    async def delay(
        self,
        task: TaskTarget,
        *,
        seconds: float | timedelta,
        queue: str | None = None,
        args: Sequence[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
    ) -> str:
        """
        Dispatch a delayed one-off task directly through SwiftPY's Queue delay infrastructure.
        """
        raw_delay = (
            seconds.total_seconds() if isinstance(seconds, timedelta) else seconds
        )
        delay_seconds = _parse_positive_finite(raw_delay, field="seconds")
        clean_queue = (
            _validate_non_empty_str(queue, field="queue") if queue is not None else None
        )

        task_name, task_kind, args_list, kwargs_dict, target_queue, _ = (
            self._resolve_target_spec(task, tuple(args), kwargs, clean_queue, None)
        )
        if isinstance(task, Job):
            job = task
        elif task_kind == "callable":
            job = ScheduledCallableJob(
                task_name=task_name,
                task_args=args_list,
                task_kwargs=kwargs_dict,
            )
        else:
            job = _instantiate_job_class(task_name, args_list, kwargs_dict)

        dispatcher = self._resolve_dispatcher()
        return await dispatcher.push(job, queue=target_queue, delay=delay_seconds)

    # ------------------------------------------------------------------
    # Schedule CRUD & lifecycle controls
    # ------------------------------------------------------------------

    async def get(self, schedule_or_name: str) -> ScheduleRecord | None:
        return await self.store.get_schedule(schedule_or_name)

    async def list_schedules(self) -> list[ScheduleRecord]:
        return await self.store.list_schedules()

    async def get_occurrence(self, occurrence_id: str) -> ScheduleOccurrence | None:
        return await self.store.get_occurrence(occurrence_id)

    async def cancel(self, schedule_or_name: str) -> bool:
        """Cancel a registered schedule and un-dispatched pending claims for it."""
        removed = await self.store.delete_schedule(schedule_or_name)
        if removed:
            self._wake_event.set()
        return removed

    async def disable(self, schedule_or_name: str) -> ScheduleRecord:
        """Disable future runs without interrupting any already-running job."""
        rec = await self.store.set_enabled(schedule_or_name, False)
        self._wake_event.set()
        return rec

    async def enable(self, schedule_or_name: str) -> ScheduleRecord:
        """Enable a disabled schedule and recompute next due slot if stale."""
        existing = await self.store.get_schedule(schedule_or_name)
        if existing is None:
            raise ScheduleNotFoundError(f"Schedule '{schedule_or_name}' not found.")

        now = self._now()
        next_run: datetime | None = existing.next_run_at
        if existing.trigger == TriggerType.ONCE:
            if existing.run_at is None:
                raise InvalidScheduleError(
                    f"One-time schedule '{existing.id}' has no run_at datetime."
                )
            next_run = existing.run_at
        elif next_run is None or next_run <= now:
            next_run = self._compute_next_run(existing, now)

        rec = await self.store.set_enabled(
            schedule_or_name, True, next_run_at=next_run
        )
        self._wake_event.set()
        return rec

    async def update(
        self,
        schedule_or_name: str,
        *,
        expression: str | None = None,
        interval_seconds: float | None = None,
        run_at: datetime | None = None,
        timezone: str | tzinfo | None = None,
        queue: str | None = None,
        overlap_policy: OverlapPolicy | str | None = None,
        misfire_policy: MisfirePolicy | str | None = None,
        lock_ttl: int | None = None,
        grace_seconds: float | None = None,
        allow_past: bool = False,
    ) -> ScheduleRecord:
        """Update an existing schedule's timing or policies without corrupting active runs."""
        rec = await self.store.get_schedule(schedule_or_name)
        if rec is None:
            raise ScheduleNotFoundError(f"Schedule '{schedule_or_name}' not found.")

        now = self._now()
        timing_changed = False

        if timezone is not None:
            _, rec.timezone = resolve_timezone(timezone)
            timing_changed = True

        if expression is not None:
            if rec.trigger != TriggerType.CRON:
                raise InvalidScheduleError(
                    f"Cannot update cron expression on non-cron schedule '{rec.id}'."
                )
            rec.expression = parse_cron(expression).normalized
            timing_changed = True

        if interval_seconds is not None:
            if rec.trigger != TriggerType.INTERVAL:
                raise InvalidScheduleError(
                    f"Cannot update interval_seconds on non-interval schedule '{rec.id}'."
                )
            rec.interval_seconds = _parse_positive_finite(
                interval_seconds, field="interval_seconds"
            )
            timing_changed = True

        if run_at is not None:
            if rec.trigger != TriggerType.ONCE:
                raise InvalidScheduleError(
                    f"Cannot update run_at on recurring schedule '{rec.id}'."
                )
            at_utc, resolved_tz = normalize_schedule_datetime(run_at, rec.timezone)
            if not allow_past and at_utc < now:
                raise InvalidScheduleError(
                    f"Updated datetime {at_utc.isoformat()} is in the past."
                )
            rec.run_at = at_utc
            rec.timezone = resolved_tz
            rec.next_run_at = at_utc
            rec.enabled = True
            timing_changed = True

        if queue is not None:
            verify_schedule_signature(
                rec.signature,
                task_kind=rec.task_kind,
                task_name=rec.task_name,
                args=rec.args,
                kwargs=rec.kwargs,
                queue=rec.queue,
                label=rec.id,
            )
            rec.queue = _validate_non_empty_str(queue, field="queue")
            self._sign_record(rec)
        if overlap_policy is not None:
            try:
                rec.overlap_policy = OverlapPolicy(str(overlap_policy))
            except ValueError as exc:
                raise InvalidScheduleError(f"Invalid overlap_policy: {overlap_policy!r}") from exc
        if misfire_policy is not None:
            try:
                rec.misfire_policy = MisfirePolicy(str(misfire_policy))
            except ValueError as exc:
                raise InvalidScheduleError(f"Invalid misfire_policy: {misfire_policy!r}") from exc
        if lock_ttl is not None:
            rec.lock_ttl = _parse_positive_int(lock_ttl, field="lock_ttl")
        if grace_seconds is not None:
            rec.grace_seconds = _parse_non_negative_finite(
                grace_seconds, field="grace_seconds"
            )

        if timing_changed and rec.trigger != TriggerType.ONCE:
            rec.next_run_at = self._compute_next_run(rec, now)

        rec.updated_at = now
        updated = await self.store.update_schedule(rec)
        self._wake_event.set()
        return updated

    # ------------------------------------------------------------------
    # Execution & crash-safe dispatch recovery
    # ------------------------------------------------------------------

    async def _push_occurrence(
        self,
        occ: ScheduleOccurrence,
        *,
        schedule: ScheduleRecord | None = None,
    ) -> ScheduleOccurrence:
        now = self._now()
        try:
            job = self._build_job_for_occurrence(occ)
            dispatcher = self._resolve_dispatcher()
            job_id = await dispatcher.push(job, queue=occ.queue)
        except ScheduleSignatureError as exc:
            logger.error(
                "Refusing to dispatch occurrence '%s' for schedule '%s': %s",
                occ.occurrence_id,
                occ.schedule_id,
                exc,
            )
            rejected = await self.store.mark_dispatch_attempt_failed(
                occ.occurrence_id,
                error=str(exc),
                now=now,
                max_dispatch_attempts=1,
            )
            if rejected is not None and rejected.status == OccurrenceStatus.FAILED:
                await dispatch_scheduler_event(
                    ScheduledTaskFailed(occurrence=rejected, error=str(exc))
                )
            return rejected or occ
        except Exception as exc:
            updated_occ = await self.store.mark_dispatch_attempt_failed(
                occ.occurrence_id,
                error=str(exc),
                now=now,
                max_dispatch_attempts=self.max_dispatch_attempts,
            )
            logger.exception(
                "Scheduler failed to dispatch occurrence '%s' for schedule '%s'",
                occ.occurrence_id,
                occ.schedule_id,
            )
            if (
                updated_occ is not None
                and updated_occ.status == OccurrenceStatus.FAILED
            ):
                await dispatch_scheduler_event(
                    ScheduledTaskFailed(occurrence=updated_occ, error=str(exc))
                )
                return updated_occ
            return updated_occ or occ

        confirmed = await self.store.confirm_dispatched(
            occ.occurrence_id,
            job_id=job_id,
            now=now,
        )
        final_occ = confirmed or occ
        ref_schedule = schedule or await self.store.get_schedule(occ.schedule_id)
        if ref_schedule is not None and final_occ.status == OccurrenceStatus.DISPATCHED:
            await dispatch_scheduler_event(
                ScheduledTaskDispatched(schedule=ref_schedule, occurrence=final_occ)
            )
        return final_occ

    async def _recover_pending_unlocked(
        self, *, limit: int = 50
    ) -> list[ScheduleOccurrence]:
        now = self._now()
        to_retry, exhausted = await self.store.reclaim_pending_dispatches(
            now=now,
            reclaimed_by=self.scheduler_id,
            dispatch_lease_seconds=self.dispatch_lease_seconds,
            max_dispatch_attempts=self.max_dispatch_attempts,
            limit=limit,
        )
        processed: list[ScheduleOccurrence] = []

        for failed_occ in exhausted:
            await dispatch_scheduler_event(
                ScheduledTaskFailed(
                    occurrence=failed_occ,
                    error=failed_occ.error or "Pending dispatch retries exhausted",
                )
            )
            processed.append(failed_occ)

        for retry_occ in to_retry:
            processed.append(await self._push_occurrence(retry_occ))

        return processed

    async def recover_pending_dispatches(
        self, *, limit: int = 50
    ) -> list[ScheduleOccurrence]:
        """Reclaim and recover any expired ``dispatch_pending`` occurrence claims."""
        async with self._tick_lock:
            return await self._recover_pending_unlocked(limit=limit)

    async def tick(self, *, limit: int = 50) -> list[ScheduleOccurrence]:
        """
        Execute one scheduler evaluation cycle:
        1. Recover expired ``dispatch_pending`` claims left by crashed processes.
        2. Atomically claim due schedules into ``dispatch_pending`` or ``skipped``.
        3. Push claimed tasks onto ``QueueDispatcher`` and confirm dispatch.
        """
        async with self._tick_lock:
            processed = await self._recover_pending_unlocked(limit=limit)

            now = self._now()
            outcomes: list[ClaimOutcome] = await self.store.claim_due(
                now=now,
                claimed_by=self.scheduler_id,
                default_grace_seconds=self.grace_seconds,
                default_lock_ttl=self.default_lock_ttl,
                dispatch_lease_seconds=self.dispatch_lease_seconds,
                next_run_calculator=self._compute_next_run,
                limit=limit,
            )

            for outcome in outcomes:
                if outcome.kind == "skipped":
                    await dispatch_scheduler_event(
                        ScheduledTaskSkipped(
                            schedule=outcome.schedule,
                            reason=outcome.reason or "skipped",
                            occurrence=outcome.occurrence,
                        )
                    )
                    processed.append(outcome.occurrence)
                    continue

                await dispatch_scheduler_event(
                    ScheduledTaskDue(
                        schedule=outcome.schedule,
                        scheduled_for=outcome.occurrence.scheduled_for,
                    )
                )
                dispatched_occ = await self._push_occurrence(
                    outcome.occurrence,
                    schedule=outcome.schedule,
                )
                processed.append(dispatched_occ)

            return processed

    run_due = tick

    @property
    def is_running(self) -> bool:
        """Return True while the background scheduler loop task is active."""
        return self._loop_task is not None and not self._loop_task.done()

    def _request_stop(self) -> None:
        self._stopping = True
        self._wake_event.set()

    def _install_signal_handlers(self) -> dict[signal.Signals, Any]:
        if threading.current_thread() is not threading.main_thread():
            return {}
        original: dict[signal.Signals, Any] = {}
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                original[sig] = signal.getsignal(sig)
            except (ValueError, AttributeError):
                continue
            if loop is not None:
                try:
                    loop.add_signal_handler(sig, self._request_stop)
                    continue
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
            try:
                signal.signal(sig, lambda _s, _f: self._request_stop())
            except (ValueError, OSError, RuntimeError):
                original.pop(sig, None)
        return original

    def _restore_signal_handlers(
        self, original: dict[signal.Signals, Any]
    ) -> None:
        for sig, handler in original.items():
            try:
                loop = asyncio.get_running_loop()
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            try:
                if handler is not None:
                    signal.signal(sig, handler)
            except (ValueError, AttributeError):
                pass

    async def _compute_next_wake_timeout(
        self, max_sleep: float, *, progressed: bool = True
    ) -> float:
        now = self._now()
        wait_seconds = max_sleep
        try:
            next_due = await self.store.next_due_at()
        except Exception:
            logger.debug(
                "Could not compute earliest schedule wake delay; using poll interval",
                exc_info=True,
            )
            return max(0.01, max_sleep)
        if next_due is not None:
            delta = (next_due - now).total_seconds()
            if delta <= 0.0:
                # A tick that processed nothing while work is still due must not spin.
                return 0.01 if progressed else max(0.01, max_sleep)
            wait_seconds = min(wait_seconds, delta)
        return max(0.01, min(max_sleep, wait_seconds))

    async def _run_loop(
        self,
        *,
        sleep: float | None = None,
        max_ticks: int | None = None,
    ) -> int:
        interval = (
            _parse_positive_finite(sleep, field="sleep")
            if sleep is not None
            else self.poll_interval
        )
        ticks = 0
        self._stopping = False
        while not self._stopping:
            progressed = False
            try:
                progressed = bool(await self.tick())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled error in scheduler tick cycle")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            if self._stopping:
                break
            self._wake_event.clear()
            timeout = await self._compute_next_wake_timeout(
                interval, progressed=progressed
            )
            if self._stopping:
                break
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
            except TimeoutError:
                pass
        return ticks

    def start(self) -> asyncio.Task[None]:
        """Start the non-blocking background scheduler loop idempotently."""
        if self._loop_task is not None and not self._loop_task.done():
            return self._loop_task
        self._stopping = False
        self._wake_event.clear()

        async def _runner() -> None:
            await self._run_loop()

        task = asyncio.create_task(
            _runner(), name=f"swiftpy-scheduler-{self.scheduler_id}"
        )

        def _on_done(done_task: asyncio.Task[None]) -> None:
            if self._loop_task is done_task:
                self._loop_task = None
            if not done_task.cancelled():
                exc = done_task.exception()
                if exc is not None:
                    logger.exception("Scheduler background loop failed", exc_info=exc)

        task.add_done_callback(_on_done)
        self._loop_task = task
        return task

    async def stop(self) -> None:
        """Stop the background scheduler loop and await clean task shutdown."""
        self._request_stop()
        task = self._loop_task
        if task is None:
            return
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif not task.cancelled():
            _ = task.exception()
        self._loop_task = None

    async def work(
        self,
        *,
        sleep: float | None = None,
        max_ticks: int | None = None,
        install_signals: bool = True,
    ) -> int:
        """Run the scheduler loop in the foreground until stopped or ``max_ticks`` completes."""
        if max_ticks is not None and (
            isinstance(max_ticks, bool)
            or not isinstance(max_ticks, int)
            or max_ticks < 0
        ):
            raise InvalidScheduleError(
                f"max_ticks must be a non-negative integer, got {max_ticks!r}."
            )
        if sleep is not None:
            _parse_positive_finite(sleep, field="sleep")
        if self.is_running:
            await self.stop()
        if max_ticks == 0:
            return 0

        original_handlers: dict[signal.Signals, Any] = {}
        if install_signals:
            original_handlers = self._install_signal_handlers()
        try:
            return await self._run_loop(sleep=sleep, max_ticks=max_ticks)
        finally:
            if install_signals:
                self._restore_signal_handlers(original_handlers)

    async def close(self) -> None:
        """Stop any running background loop and close the schedule store cleanly."""
        await self.stop()
        await self.store.close()


class Schedule:
    """Static facade delegating scheduling operations to ``Scheduler.get_instance()``."""

    @staticmethod
    def every(interval: float) -> IntervalScheduleBuilder:
        return Scheduler.get_instance().every(interval)

    @staticmethod
    def cron(expression: str) -> ScheduleBuilder:
        return Scheduler.get_instance().cron(expression)

    @staticmethod
    def every_minute() -> ScheduleBuilder:
        return Scheduler.get_instance().every_minute()

    @staticmethod
    def hourly() -> ScheduleBuilder:
        return Scheduler.get_instance().hourly()

    @staticmethod
    def hourly_at(minute: int = 0) -> ScheduleBuilder:
        return Scheduler.get_instance().hourly_at(minute)

    @staticmethod
    def daily() -> ScheduleBuilder:
        return Scheduler.get_instance().daily()

    @staticmethod
    def daily_at(time_str: str = "00:00") -> ScheduleBuilder:
        return Scheduler.get_instance().daily_at(time_str)

    @staticmethod
    def weekly() -> ScheduleBuilder:
        return Scheduler.get_instance().weekly()

    @staticmethod
    def weekly_at(
        day_of_week: int | str = 1, time_str: str = "00:00"
    ) -> ScheduleBuilder:
        return Scheduler.get_instance().weekly_at(day_of_week, time_str)

    @staticmethod
    def monthly() -> ScheduleBuilder:
        return Scheduler.get_instance().monthly()

    @staticmethod
    def monthly_at(day: int = 1, time_str: str = "00:00") -> ScheduleBuilder:
        return Scheduler.get_instance().monthly_at(day, time_str)

    @staticmethod
    def task(
        name: str | None = None,
    ) -> Callable[[TaskCallable], TaskCallable]:
        return Scheduler.get_instance().task(name)

    @staticmethod
    def register_task(name: str, fn: TaskCallable) -> TaskCallable:
        return Scheduler.get_instance().register_task(name, fn)

    @staticmethod
    async def schedule(
        task: TaskTarget,
        *,
        at: datetime,
        timezone: str | tzinfo | None = None,
        name: str | None = None,
        queue: str | None = None,
        overlap_policy: OverlapPolicy | str = OverlapPolicy.ALLOW,
        misfire_policy: MisfirePolicy | str = MisfirePolicy.SKIP,
        lock_ttl: int | None = None,
        grace_seconds: float | None = None,
        allow_past: bool = False,
        upsert: bool = False,
        args: Sequence[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
    ) -> ScheduleRecord:
        return await Scheduler.get_instance().schedule(
            task,
            at=at,
            timezone=timezone,
            name=name,
            queue=queue,
            overlap_policy=overlap_policy,
            misfire_policy=misfire_policy,
            lock_ttl=lock_ttl,
            grace_seconds=grace_seconds,
            allow_past=allow_past,
            upsert=upsert,
            args=args,
            kwargs=kwargs,
        )

    @staticmethod
    async def delay(
        task: TaskTarget,
        *,
        seconds: float | timedelta,
        queue: str | None = None,
        args: Sequence[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
    ) -> str:
        return await Scheduler.get_instance().delay(
            task, seconds=seconds, queue=queue, args=args, kwargs=kwargs
        )

    @staticmethod
    async def get(schedule_or_name: str) -> ScheduleRecord | None:
        return await Scheduler.get_instance().get(schedule_or_name)

    @staticmethod
    async def list_schedules() -> list[ScheduleRecord]:
        return await Scheduler.get_instance().list_schedules()

    @staticmethod
    async def get_occurrence(occurrence_id: str) -> ScheduleOccurrence | None:
        return await Scheduler.get_instance().get_occurrence(occurrence_id)

    @staticmethod
    async def cancel(schedule_or_name: str) -> bool:
        return await Scheduler.get_instance().cancel(schedule_or_name)

    @staticmethod
    async def enable(schedule_or_name: str) -> ScheduleRecord:
        return await Scheduler.get_instance().enable(schedule_or_name)

    @staticmethod
    async def disable(schedule_or_name: str) -> ScheduleRecord:
        return await Scheduler.get_instance().disable(schedule_or_name)

    @staticmethod
    async def update(
        schedule_or_name: str,
        *,
        expression: str | None = None,
        interval_seconds: float | None = None,
        run_at: datetime | None = None,
        timezone: str | tzinfo | None = None,
        queue: str | None = None,
        overlap_policy: OverlapPolicy | str | None = None,
        misfire_policy: MisfirePolicy | str | None = None,
        lock_ttl: int | None = None,
        grace_seconds: float | None = None,
        allow_past: bool = False,
    ) -> ScheduleRecord:
        return await Scheduler.get_instance().update(
            schedule_or_name,
            expression=expression,
            interval_seconds=interval_seconds,
            run_at=run_at,
            timezone=timezone,
            queue=queue,
            overlap_policy=overlap_policy,
            misfire_policy=misfire_policy,
            lock_ttl=lock_ttl,
            grace_seconds=grace_seconds,
            allow_past=allow_past,
        )

    @staticmethod
    async def tick(*, limit: int = 50) -> list[ScheduleOccurrence]:
        return await Scheduler.get_instance().tick(limit=limit)

    @staticmethod
    async def run_due(*, limit: int = 50) -> list[ScheduleOccurrence]:
        return await Scheduler.get_instance().tick(limit=limit)


def scheduler() -> Scheduler:
    """Return the active container's ``Scheduler`` instance."""
    return Scheduler.get_instance()


def _resolve_callable_from_active(name: str) -> TaskCallable | None:
    inst = Scheduler._get_existing_instance()
    if inst is not None:
        return inst.get_registered_task(name)
    return None


def _resolve_store_from_active() -> ScheduleStore | None:
    inst = Scheduler._get_existing_instance()
    if inst is not None:
        return inst.store
    return None


set_callable_resolver(_resolve_callable_from_active)
set_active_store_resolver(_resolve_store_from_active)


__all__ = [
    "IntervalScheduleBuilder",
    "QueuePusher",
    "Schedule",
    "ScheduleBuilder",
    "Scheduler",
    "TaskTarget",
    "scheduler",
]
