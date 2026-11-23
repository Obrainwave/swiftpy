# Task Scheduling

## Introduction

SwiftPY includes a built-in asynchronous task scheduler (`swiftpy.scheduling`) that evaluates **when** work is due (one-time datetimes, fixed intervals, and strict 5-field cron expressions with IANA timezones) while reusing SwiftPY's signed Queue infrastructure (`QueueDispatcher` + `QueueWorker`) for **how** work is executed, retried, and routed to the Dead Letter Queue.

Quick imports:

```python
from datetime import UTC, datetime, timedelta

from swiftpy import Job, Schedule, Scheduler, scheduler
from swiftpy.scheduling import (
    MemoryScheduleStore,
    MisfirePolicy,
    OverlapPolicy,
    RedisScheduleStore,
    ScheduledCallableJob,
    ScheduledTaskCompleted,
    ScheduledTaskDispatched,
    ScheduledTaskDue,
    ScheduledTaskFailed,
    ScheduledTaskSkipped,
    ScheduleRegistered,
)
```

---

## Configuration (`config/schedule.py`)

Scheduler defaults live in `config/schedule.py` (automatically scaffolded by `swiftpy new`):

| Environment Variable | Config Key | Default | Notes |
|---|---|---|---|
| `SCHEDULE_DRIVER` | `schedule.driver` | `"redis"` | `"redis"` (durable) or `"memory"` (tests/local only) |
| `SCHEDULE_TIMEZONE` | `schedule.timezone` | `"UTC"` | Default IANA timezone (falls back to `APP_TIMEZONE`, then `"UTC"`, never OS local time) |
| `SCHEDULE_AUTO_START` | `schedule.auto_start` | `False` | Opt-in in-process loop during `Application.startup()`; keep `False` when running `--workers > 1` or dedicated scheduler daemons |
| `SCHEDULE_POLL_INTERVAL` | `schedule.poll_interval` | `1.0` | Max seconds between scheduler wakeups (`> 0`) |
| `SCHEDULE_GRACE_SECONDS` | `schedule.grace_seconds` | `60.0` | Default misfire grace window in seconds (`>= 0`) |
| `SCHEDULE_DISPATCH_LEASE_SECONDS` | `schedule.dispatch_lease_seconds` | `30.0` | Crash-recovery lease timeout for unconfirmed `dispatch_pending` claims (`> 0`) |
| `SCHEDULE_MAX_DISPATCH_ATTEMPTS` | `schedule.max_dispatch_attempts` | `5` | Max push recovery attempts before marking an occurrence failed (`>= 1`) |
| `SCHEDULE_DEFAULT_LOCK_TTL` | `schedule.default_lock_ttl` | `900` | Fallback overlap lock TTL (seconds) when a task has no `timeout` or explicit `lock_ttl` |
| `SCHEDULE_PREFIX` | `schedule.prefix` | `"swiftpy_schedule:"` | Redis key prefix on `REDIS_QUEUE_DB` (isolated from `cache:clear`) |
| `SCHEDULE_RETENTION_SECONDS` | `schedule.retention_seconds` | `604800` (7 days) | Redis expiration TTL for finished occurrence records |
| `SCHEDULE_MAX_OCCURRENCES` | `schedule.max_occurrences_per_schedule` | `200` | Max per-schedule occurrence history entries retained in Redis |

Invalid `schedule.driver`, unresolvable IANA timezone names, or non-finite/out-of-range timeouts fail fast with `ConfigurationError` during `Application(...)` initialization.

---

## Defining Scheduled Tasks

You can register schedules through the `Schedule` static facade, the `scheduler()` helper, or an injected `Scheduler` singleton — all resolve the active container's `Scheduler` instance.

### 1. Recurring Cron & Presets

```python
from swiftpy import Job, Schedule


class SendDailyDigestJob(Job):
    queue = "emails"
    tries = 3
    backoff = 10
    timeout = 120.0

    def __init__(self, team: str = "core") -> None:
        super().__init__(team=team)
        self.team = team

    async def handle(self) -> None:
        ...


# Standard 5-field cron or presets
await (
    Schedule.cron("0 9 * * 1-5")
    .timezone("America/New_York")
    .name("weekday-digest")
    .without_overlapping()
    .upsert()
    .run(SendDailyDigestJob, team="engineering")
)

await Schedule.every_minute().name("heartbeat").upsert().run(SendDailyDigestJob)
await Schedule.every(5).minutes().without_overlapping(lock_ttl=600).run(SendDailyDigestJob)
await Schedule.hourly().run(SendDailyDigestJob)
await Schedule.hourly_at(15).run(SendDailyDigestJob)
await Schedule.daily().run(SendDailyDigestJob)
await Schedule.daily_at("02:30").timezone("Europe/Berlin").run(SendDailyDigestJob)
await Schedule.weekly().run(SendDailyDigestJob)
await Schedule.weekly_at("mon", "08:30").run(SendDailyDigestJob)
await Schedule.monthly().run(SendDailyDigestJob)
await Schedule.monthly_at(1, "00:05").run(SendDailyDigestJob)
```

Supported intervals: `every(N).seconds()`, `.minutes()`, `.hours()`, and `.days()` (all validate finite numbers `> 0`).

### 2. Cron Syntax & DST Rules

SwiftPY uses a zero-dependency strict 5-field Vixie cron engine (`minute hour dom month dow`) with `zoneinfo`:

- Supports `*`, `*/step`, `a-b`, `a-b/step`, comma lists, month names (`jan`–`dec`), weekday names (`sun`–`sat`, `0`/`7` Sunday), and macros (`@hourly`, `@daily`, `@midnight`, `@weekly`, `@monthly`, `@yearly`, `@annually`).
- Follows Vixie DOM/DOW **OR** semantics when both day-of-month and day-of-week are restricted (`!= "*"`).
- Rejects unsupported 6-field second expressions, `L`, `W`, `#`, `?`, or impossible calendar dates (`0 0 31 2 *`) eagerly with `InvalidCronError`.
- Handles IANA DST transitions deterministically: nonexistent spring-forward hours advance cleanly to the next valid instant, and fall-back repeated wall hours fire **once** at the first occurrence (`fold=0`).

### 3. One-Time & Delayed Jobs

```python
from datetime import UTC, datetime, timedelta
from swiftpy import Schedule

# Managed persistent one-time schedule (will not dispatch before run_at)
run_at = datetime.now(UTC) + timedelta(minutes=30)
entry = await Schedule.schedule(
    SendDailyDigestJob,
    at=run_at,
    name="onboarding-42",
    kwargs={"team": "onboarding"},
)

# Transient delayed job (pushes directly onto QueueDispatcher's delayed ZSET)
job_id = await Schedule.delay(
    SendDailyDigestJob,
    seconds=45,
    kwargs={"team": "alerts"},
)
```

> [!NOTE]
> Passing a naive `datetime` to `Schedule.schedule(..., at=...)` attaches the resolved schedule timezone (default `"UTC"`). Ambiguous nonexistent spring-forward wall times raise `InvalidScheduleError`.

### 4. Registering Named Callables Safely (`ScheduledCallableJob`)

For small tasks without a dedicated `Job` subclass, register sync or async callables explicitly at app startup via `@scheduler().task(...)`. Arbitrary lambdas or unregistered callables are rejected with `InvalidScheduleError`, and task arguments must be strictly JSON-serializable:

```python
from swiftpy import scheduler

sched = scheduler()


@sched.task("metrics.flush")
async def flush_metrics(window: str) -> None:
    ...


await sched.every(1).minutes().name("flush-metrics").upsert().run(
    "metrics.flush",
    "1m",
)
```

Named callable schedules dispatch wrapped inside trusted `ScheduledCallableJob` envelopes signed via `APP_KEY`, so `QueueWorker` processes must load the same application target (`--app main:app`) where the `@sched.task(...)` registration runs.

---

## Lifecycle, Boot Synchronization & Concurrency

### Idempotent Boot Registration (`.upsert()`)

- Duplicate `name` values with `upsert=False` (default) raise `DuplicateScheduleError`.
- Chaining `.upsert()` updates definition fields (`trigger`, `expression`, `interval_seconds`, `timezone`, `queue`, `args`, `kwargs`, `overlap_policy`, `misfire_policy`, `lock_ttl`, `grace_seconds`) atomically **without** resetting `enabled`, `last_run_at`, `last_claimed_occurrence`, `last_occurrence_id`, `last_status`, or `last_error`.
- `next_run_at` is only recomputed when timing (`trigger`, `expression`, `interval_seconds`, `run_at`, or `timezone`) changes, keeping restarts across multiple web workers safe.

### Runtime Inspection & Controls

```python
rec = await Schedule.get("weekday-digest")
all_records = await Schedule.list_schedules()
occ = await Schedule.get_occurrence(entry_occ_id)

await Schedule.update("weekday-digest", expression="30 8 * * 1-5")
await Schedule.disable("weekday-digest")   # stops future due runs; active QueueWorker jobs still finish cleanly
await Schedule.enable("weekday-digest")    # recomputes next_run_at if stale
await Schedule.cancel("onboarding-42")     # deletes schedule + cancels un-dispatched claims (queued jobs finish cleanly)
```

### Crash Recovery & Overlap Locks

1. **Scheduler vs. Queue split:** Scheduler claims a due `ScheduleRecord` by writing an independent `ScheduleOccurrence` in `dispatch_pending` (with `lease_until`) and advancing recurring `next_run_at` atomically via Lua (`CLAIM_DUE_LUA`).
2. **Crash recovery:** Once `QueueDispatcher.push()` succeeds, `confirm_dispatched()` transitions the occurrence to `dispatched` and clears the pending lease. If a scheduler crashes between claiming and pushing, any scheduler node reclaims expired leases (`RECLAIM_PENDING_LUA`) at the start of the next `tick()`.
3. **Overlap locking:** `.without_overlapping(lock_ttl=...)` acquires a token-verified Redis `SET ... NX PX` lock during claim. If `lock_ttl` is omitted and the `Job` specifies a `timeout`, SwiftPY derives `ceil(timeout * tries + total_backoff + 60)` (otherwise `default_lock_ttl = 900`).
4. **Worker lifecycle & retry retention:** `QueueWorker` records `running`, `completed`, and `failed` transitions and releases the overlap lock via compare-and-delete Lua **only** upon final `ack()` or DLQ exhaustion (`job.attempts >= job.tries`), retaining the lock across intermediate backoff retries. When a job starts running the lock is re-armed to its full `lock_ttl` (queue wait time does not count against it), a worker heartbeat extends it every `lock_ttl / 3` seconds while the job executes, and it is extended by the backoff delay before a retry. If a worker process is OOM-killed permanently, the heartbeat stops and the lock clears once `lock_ttl` expires.
5. **Delivery semantics:** Dispatch is at-least-once. A crash between `push()` and `confirm_dispatched()` can enqueue the same occurrence twice. A worker skips a duplicate whose occurrence already `completed`, but a duplicate that arrives while the first copy is still running cannot be told apart from crash recovery, so make scheduled jobs idempotent (the occurrence id is available as `job.data["_swiftpy_schedule_occurrence"]`).
6. **Signed schedule records:** Every schedule stores an HMAC-SHA256 signature (keyed by `APP_KEY`; previous keys are accepted during rotation) over its task kind, task name, arguments, and queue. The signature is copied to each occurrence and verified before a job is built, so a record rewritten directly in Redis is marked `failed` (`ScheduleSignatureError`) instead of being executed. Re-register schedules that were created before `APP_KEY` was configured.
7. **Driver default:** when `schedule.driver` is unset, Scheduler uses `redis` in production and `memory` elsewhere; the scaffolded `config/schedule.py` always sets `redis` explicitly.

> [!WARNING]
> **`MemoryScheduleStore` is NOT durable (`is_durable = False`):** `MemoryScheduleStore` keeps all schedules, occurrences, and overlap locks in local Python memory and cannot coordinate across processes or survive restarts. Using `schedule.driver="memory"` when `APP_ENV=production` raises `ConfigurationError` unless explicitly opted in via `schedule.allow_memory_in_production=True`.

---

## Running in Development & Production

### CLI Commands

```bash
# Inspect registered schedules (human-readable table or JSON)
swiftpy schedule:list --app main:app
swiftpy schedule:list --app main:app --json

# Recover expired dispatch_pending claims and process one due tick
swiftpy schedule:run --app main:app

# Run the dedicated async scheduler loop (SIGINT/SIGTERM graceful drain)
swiftpy schedule:work --app main:app --sleep 1.0
```

### Recommended Multi-Container / Multi-Worker Production Topology

Because `uvicorn --workers N` (`swiftpy start --workers 4`) starts N independent ASGI worker processes, keep `SCHEDULE_AUTO_START=false` (the default) on web containers and run:

1. **Web API container(s):** `swiftpy start main:app --host 0.0.0.0 --port 8000 --workers 4`
2. **Scheduler container (1+ replicas sharing Redis DB `1`):** `swiftpy schedule:work --app main:app` (or external cron calling `swiftpy schedule:run --app main:app` once per minute)
3. **Queue Worker container(s):** `swiftpy queue:work --app main:app --queue default,emails --concurrency 4`

For single-process apps or tests where you want an embedded scheduler loop inside ASGI lifespan, set `SCHEDULE_AUTO_START=true`.

---

## Testing Scheduled Tasks

Inject a fake clock + `MemoryScheduleStore` (or real `RedisScheduleStore` on a test prefix) and drive `await sched.tick()` deterministically with zero `asyncio.sleep()` calls:

```python
from datetime import UTC, datetime, timedelta
from swiftpy.scheduling import MemoryScheduleStore, OccurrenceStatus, Scheduler


async def test_scheduled_task_deterministic() -> None:
    now = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    sched = Scheduler(store=MemoryScheduleStore(clock=lambda: now), clock=lambda: now)
    await sched.every(5).minutes().name("sync").run(SendDailyDigestJob)

    assert await sched.tick() == []
    now = now + timedelta(minutes=5)
    # Use a shared mutable clock object in pytest to advance `now` across calls
```

---

## Next Step

Continue to [Developer CLI (`swiftpy`) & Testing Utilities](2026-11-19-cli-and-testing.md) for the complete `swiftpy` command summary and async test helpers.
