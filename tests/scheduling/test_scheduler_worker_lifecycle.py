from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis.asyncio as aioredis

from swiftpy.core.container import Container, bind_container
from swiftpy.core.context import Context
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job
from swiftpy.queue.worker import QueueWorker
from swiftpy.scheduling import (
    OccurrenceStatus,
    RedisScheduleStore,
    ScheduledTaskCompleted,
    ScheduledTaskFailed,
    Scheduler,
)
from swiftpy.scheduling.jobs import get_callable_resolver, set_callable_resolver


class FakeClock:
    def __init__(self, initial: datetime) -> None:
        self.now = initial

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


_GATE_STARTED: asyncio.Event | None = None
_GATE_CONTINUE: asyncio.Event | None = None
_EXEC_LOG: list[str] = []


class GatedScheduledJob(Job):
    queue = "sched_q"
    tries = 2
    backoff = 0.05
    timeout = 30.0

    def __init__(self, tag: str = "run") -> None:
        super().__init__(tag=tag)
        self.tag = tag

    async def handle(self) -> None:
        _EXEC_LOG.append(f"start:{self.tag}")
        if _GATE_STARTED is not None:
            _GATE_STARTED.set()
        if _GATE_CONTINUE is not None:
            await _GATE_CONTINUE.wait()
        _EXEC_LOG.append(f"end:{self.tag}")


class RetryThenSuccessScheduledJob(Job):
    queue = "sched_q"
    tries = 3
    backoff = 0.05
    timeout = 30.0

    def __init__(self, key: str, fail_times: int = 2) -> None:
        super().__init__(key=key, fail_times=fail_times)
        self.key = key
        self.fail_times = fail_times

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        password = os.getenv("REDIS_PASSWORD") or None
        db = int(os.getenv("REDIS_QUEUE_DB", "1"))
        client = aioredis.Redis(
            host=host, port=port, password=password, db=db, decode_responses=True
        )
        try:
            attempts = await client.incr(self.key)
            if attempts <= self.fail_times:
                raise RuntimeError(f"Transient failure #{attempts}")
        finally:
            await client.aclose()


class AlwaysFailingScheduledJob(Job):
    queue = "sched_q"
    tries = 2
    backoff = 0.02
    timeout = 30.0

    def __init__(self, reason: str = "boom") -> None:
        super().__init__(reason=reason)
        self.reason = reason

    async def handle(self) -> None:
        raise ValueError(self.reason)


@pytest.fixture(autouse=True)
def _clean_worker_sched_state() -> Generator[None, None, None]:
    global _GATE_STARTED, _GATE_CONTINUE
    original_trusted = Job._trusted_classes.copy()
    original_resolver = get_callable_resolver()
    for cls in (GatedScheduledJob, RetryThenSuccessScheduledJob, AlwaysFailingScheduledJob):
        Job.allow_job_class(cls)
    _GATE_STARTED = None
    _GATE_CONTINUE = None
    _EXEC_LOG.clear()
    Scheduler.set_instance(None)
    EventDispatcher.set_instance(EventDispatcher())
    Context.clear()
    try:
        yield
    finally:
        Job._trusted_classes = original_trusted
        set_callable_resolver(original_resolver)
        _GATE_STARTED = None
        _GATE_CONTINUE = None
        _EXEC_LOG.clear()
        Scheduler.set_instance(None)
        EventDispatcher.set_instance(None)
        Context.clear()


@pytest.fixture
async def redis_worker_env() -> AsyncGenerator[
    tuple[aioredis.Redis, str, str, str], None
]:
    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD") or None
    db = int(os.getenv("REDIS_QUEUE_DB", "1"))
    client = aioredis.Redis(
        host=host,
        port=port,
        password=password,
        db=db,
        decode_responses=True,
        encoding_errors="replace",
    )
    run_id = uuid.uuid4().hex[:10]
    sched_prefix = f"swiftpy_sched_p6:{run_id}:"
    queue_prefix = f"swiftpy_queue_p6:{run_id}:"
    track_key = f"swiftpy_track_p6:{run_id}"
    try:
        await client.ping()
        yield client, sched_prefix, queue_prefix, track_key
    finally:
        for pattern in (f"{sched_prefix}*", f"{queue_prefix}*", f"{track_key}*"):
            keys: list[str] = [key async for key in client.scan_iter(match=pattern)]
            if keys:
                await client.delete(*keys)
        await client.aclose()


@pytest.mark.asyncio
async def test_worker_updates_running_completed_and_releases_overlap_lock_e2e(
    redis_worker_env: tuple[aioredis.Redis, str, str, str],
) -> None:
    global _GATE_STARTED, _GATE_CONTINUE
    client, sched_prefix, queue_prefix, _ = redis_worker_env
    clock = FakeClock(datetime(2026, 9, 1, 10, 0, tzinfo=UTC))
    store = RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
    dispatcher = QueueDispatcher(redis_client=client, prefix=queue_prefix)

    completed_events: list[ScheduledTaskCompleted] = []
    EventDispatcher.get_instance().listen(
        ScheduledTaskCompleted, lambda evt: completed_events.append(evt)
    )

    container = Container()
    sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)
    container.instance(Scheduler, sched)

    callable_ran: list[str] = []

    @sched.task("worker_callable")
    async def run_callable(msg: str) -> None:
        callable_ran.append(msg)

    _GATE_STARTED = asyncio.Event()
    _GATE_CONTINUE = asyncio.Event()

    with bind_container(container):
        locked_rec = (
            await sched.every(1)
            .minutes()
            .name("gated-sync")
            .without_overlapping(lock_ttl=300)
            .run(GatedScheduledJob, "first")
        )
        callable_rec = (
            await sched.every(1)
            .minutes()
            .name("callable-sync")
            .queue("sched_q")
            .run("worker_callable", "hello-worker")
        )

        # T=10:01: tick pushes BOTH jobs; overlap lock held for gated-sync
        clock.advance(minutes=1)
        tick1 = await sched.tick()
        assert len(tick1) == 2
        gated_occ = next(o for o in tick1 if o.schedule_id == locked_rec.id)
        assert gated_occ.lock_key is not None
        assert await client.get(gated_occ.lock_key) == gated_occ.lock_token

        worker = QueueWorker(
            queues="sched_q",
            dispatcher=dispatcher,
            sleep=0.05,
            promote_interval=0.05,
            stop_when_empty=True,
        )
        worker_task = asyncio.create_task(worker.work())

        # Wait until GatedScheduledJob enters handle() -> must be RUNNING in store
        await asyncio.wait_for(_GATE_STARTED.wait(), timeout=5.0)
        running_occ = await store.get_occurrence(gated_occ.occurrence_id)
        assert running_occ is not None
        assert running_occ.status == OccurrenceStatus.RUNNING
        assert running_occ.started_at is not None

        # While gated job is actively RUNNING, next due tick skips gated-sync (overlap)
        clock.advance(minutes=1)
        tick2 = await sched.tick()
        skipped_gated = [o for o in tick2 if o.schedule_id == locked_rec.id]
        assert len(skipped_gated) == 1
        assert skipped_gated[0].status == OccurrenceStatus.SKIPPED
        assert "overlap" in (skipped_gated[0].error or "").lower()

        # Unblock gated job and wait for QueueWorker to process both initial + tick2 callable
        _GATE_CONTINUE.set()
        await asyncio.wait_for(worker_task, timeout=10.0)

        assert worker.jobs_processed == 3
        assert worker.jobs_failed == 0
        assert _EXEC_LOG == ["start:first", "end:first"]
        assert callable_ran == ["hello-worker", "hello-worker"]

        # Verify gated occurrence COMPLETED, lock RELEASED, recurring schedule still ENABLED
        final_occ = await store.get_occurrence(gated_occ.occurrence_id)
        assert final_occ is not None
        assert final_occ.status == OccurrenceStatus.COMPLETED
        assert await client.get(gated_occ.lock_key) is None

        rec_after = await store.get_schedule(locked_rec.id)
        assert rec_after is not None
        assert rec_after.enabled is True
        assert rec_after.last_status == OccurrenceStatus.COMPLETED
        assert len(completed_events) == 3
        assert {e.occurrence.schedule_id for e in completed_events} == {
            locked_rec.id,
            callable_rec.id,
        }


@pytest.mark.asyncio
async def test_worker_retains_overlap_lock_on_retry_and_frees_on_dlq_exhaustion(
    redis_worker_env: tuple[aioredis.Redis, str, str, str],
) -> None:
    client, sched_prefix, queue_prefix, track_key = redis_worker_env
    clock = FakeClock(datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
    store = RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
    dispatcher = QueueDispatcher(redis_client=client, prefix=queue_prefix)

    failed_events: list[ScheduledTaskFailed] = []
    completed_events: list[ScheduledTaskCompleted] = []
    EventDispatcher.get_instance().listen(
        ScheduledTaskFailed, lambda evt: failed_events.append(evt)
    )
    EventDispatcher.get_instance().listen(
        ScheduledTaskCompleted, lambda evt: completed_events.append(evt)
    )

    sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)

    # 1. Intermediate retries must RETAIN overlap lock until final success!
    flaky_rec = (
        await sched.every(1)
        .minutes()
        .name("flaky-locked")
        .without_overlapping(lock_ttl=300)
        .run(RetryThenSuccessScheduledJob, f"{track_key}:attempts", 2)
    )
    clock.advance(minutes=1)
    tick1 = await sched.tick()
    occ1 = tick1[0]
    assert occ1.lock_key is not None and occ1.lock_token is not None

    worker_step = QueueWorker(
        queues="sched_q",
        dispatcher=dispatcher,
        schedule_store=store,
    )
    raw1 = await dispatcher.pop_and_lease("sched_q", worker_step.worker_id)
    assert raw1 is not None
    # Attempt 1 fails -> released with backoff delay, lock MUST remain held!
    ok1 = await worker_step.process_job("sched_q", raw1)
    assert ok1 is False
    assert await client.get(occ1.lock_key) == occ1.lock_token
    after_retry1 = await store.get_occurrence(occ1.occurrence_id)
    assert after_retry1 is not None
    assert after_retry1.status == OccurrenceStatus.RUNNING
    assert failed_events == []

    # Tick during retry backoff window -> skipped due to retained overlap lock!
    clock.advance(minutes=1)
    overlap_tick = await sched.tick()
    assert len(overlap_tick) == 1
    assert overlap_tick[0].schedule_id == flaky_rec.id
    assert overlap_tick[0].status == OccurrenceStatus.SKIPPED

    # Let QueueWorker finish attempts 2 (fail) and 3 (succeed) -> unlocks!
    worker_full = QueueWorker(
        queues="sched_q",
        dispatcher=dispatcher,
        schedule_store=store,
        sleep=0.03,
        promote_interval=0.03,
        stop_when_empty=True,
    )
    await asyncio.wait_for(worker_full.work(), timeout=10.0)
    assert worker_full.jobs_processed == 1
    assert await client.get(occ1.lock_key) is None
    assert len(completed_events) == 1
    completed_occ1 = await store.get_occurrence(occ1.occurrence_id)
    assert completed_occ1 is not None and completed_occ1.status == OccurrenceStatus.COMPLETED

    # 2. Exhausting retries -> moves to DLQ, transitions to FAILED, releases overlap lock
    await sched.cancel("flaky-locked")
    fail_rec = (
        await sched.every(1)
        .minutes()
        .name("dlq-locked")
        .without_overlapping(lock_ttl=300)
        .run(AlwaysFailingScheduledJob, "fatal error")
    )
    clock.advance(minutes=1)
    tick_fail = await sched.tick()
    fail_occ = tick_fail[0]
    assert fail_occ.lock_key is not None
    assert await client.get(fail_occ.lock_key) == fail_occ.lock_token

    worker_fail = QueueWorker(
        queues="sched_q",
        dispatcher=dispatcher,
        schedule_store=store,
        sleep=0.02,
        promote_interval=0.02,
        stop_when_empty=True,
    )
    await asyncio.wait_for(worker_fail.work(), timeout=10.0)
    assert worker_fail.jobs_failed == 1
    assert await dispatcher.failed_size("sched_q") == 1
    assert await client.get(fail_occ.lock_key) is None

    final_fail_occ = await store.get_occurrence(fail_occ.occurrence_id)
    assert final_fail_occ is not None
    assert final_fail_occ.status == OccurrenceStatus.FAILED
    assert final_fail_occ.error == "fatal error"

    fail_rec_after = await store.get_schedule(fail_rec.id)
    assert fail_rec_after is not None
    assert fail_rec_after.last_status == OccurrenceStatus.FAILED
    assert fail_rec_after.last_error == "fatal error"
    assert len(failed_events) == 1
    assert failed_events[0].error == "fatal error"


@pytest.mark.asyncio
async def test_standalone_worker_without_scheduler_instance_resolves_redis_prefix(
    redis_worker_env: tuple[aioredis.Redis, str, str, str],
) -> None:
    client, sched_prefix, queue_prefix, _ = redis_worker_env
    clock = FakeClock(datetime(2026, 9, 1, 15, 0, tzinfo=UTC))
    sched_store = RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
    dispatcher = QueueDispatcher(redis_client=client, prefix=queue_prefix)

    # Scheduler process pushes job, then exits completely (no in-memory Scheduler remains)
    sched = Scheduler(store=sched_store, dispatcher=dispatcher, clock=clock)
    await sched.every(5).minutes().name("detached-worker-job").without_overlapping(
        lock_ttl=300
    ).run(GatedScheduledJob, "detached")
    clock.advance(minutes=5)
    tick_occs = await sched.tick()
    assert len(tick_occs) == 1
    occ = tick_occs[0]
    assert occ.lock_key is not None
    assert await client.get(occ.lock_key) == occ.lock_token

    # Simulate completely isolated QueueWorker OS process: no active Scheduler singleton
    Scheduler.set_instance(None)
    worker = QueueWorker(
        queues="sched_q",
        dispatcher=QueueDispatcher(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            database=int(os.getenv("REDIS_QUEUE_DB", "1")),
            prefix=queue_prefix,
        ),
        sleep=0.03,
        promote_interval=0.03,
        stop_when_empty=True,
    )
    # Override dispatcher._owns_client scenario cleanly by explicitly running & closing
    try:
        await asyncio.wait_for(worker.work(), timeout=10.0)
    finally:
        await worker.dispatcher.close()

    assert worker.jobs_processed == 1
    assert worker._owned_schedule_stores == {}
    assert await client.get(occ.lock_key) is None
    finished = await sched_store.get_occurrence(occ.occurrence_id)
    assert finished is not None and finished.status == OccurrenceStatus.COMPLETED


@pytest.mark.asyncio
async def test_worker_best_effort_resilience_when_schedule_hook_raises(
    redis_worker_env: tuple[aioredis.Redis, str, str, str],
) -> None:
    class FaultyTrackingStore(RedisScheduleStore):
        def __init__(self, redis_client: aioredis.Redis, prefix: str) -> None:
            super().__init__(redis_client=redis_client, prefix=prefix)
            self.mark_running_calls = 0
            self.mark_completed_calls = 0

        async def mark_running(self, occurrence_id: str, *, now: datetime) -> None:
            self.mark_running_calls += 1
            raise RuntimeError("Simulated transient Redis tracking failure")

        async def mark_completed(self, occurrence_id: str, *, now: datetime) -> None:
            self.mark_completed_calls += 1

    client, sched_prefix, queue_prefix, _ = redis_worker_env
    dispatcher = QueueDispatcher(redis_client=client, prefix=queue_prefix)
    job = GatedScheduledJob("resilient")
    job.data["_swiftpy_schedule_occurrence"] = "occ-blip-1"
    await dispatcher.push(job, queue="sched_q")

    faulty_store = FaultyTrackingStore(redis_client=client, prefix=sched_prefix)
    worker = QueueWorker(
        queues="sched_q",
        dispatcher=dispatcher,
        schedule_store=faulty_store,
        stop_when_empty=True,
        sleep=0.03,
    )
    await asyncio.wait_for(worker.work(), timeout=5.0)
    assert worker.jobs_processed == 1
    assert worker.jobs_failed == 0
    assert faulty_store.mark_running_calls == 1
    assert faulty_store.mark_completed_calls == 1
    assert _EXEC_LOG == ["start:resilient", "end:resilient"]


@pytest.mark.asyncio
async def test_overlap_lock_survives_queue_wait_and_slow_job_via_rearm_and_heartbeat(
    redis_worker_env: tuple[aioredis.Redis, str, str, str],
) -> None:
    global _GATE_STARTED, _GATE_CONTINUE
    client, sched_prefix, queue_prefix, _ = redis_worker_env
    clock = FakeClock(datetime(2026, 9, 2, 10, 0, tzinfo=UTC))
    store = RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
    dispatcher = QueueDispatcher(redis_client=client, prefix=queue_prefix)
    sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)
    container = Container()
    container.instance(Scheduler, sched)

    _GATE_STARTED = asyncio.Event()
    _GATE_CONTINUE = asyncio.Event()

    with bind_container(container):
        rec = (
            await sched.every(1)
            .minutes()
            .name("slow-locked")
            .without_overlapping(lock_ttl=2)
            .run(GatedScheduledJob, "slow")
        )
        clock.advance(minutes=1)
        (occ,) = await sched.tick()
        assert occ.schedule_id == rec.id
        assert occ.lock_key is not None
        assert occ.lock_token is not None

        # Queue wait consumes most of the lock TTL before any worker picks the job up
        await asyncio.sleep(1.5)
        assert await client.get(occ.lock_key) == occ.lock_token

        worker = QueueWorker(
            queues="sched_q",
            dispatcher=dispatcher,
            sleep=0.05,
            promote_interval=0.05,
            stop_when_empty=True,
        )
        worker_task = asyncio.create_task(worker.work())
        await asyncio.wait_for(_GATE_STARTED.wait(), timeout=5.0)

        # Running far longer than the original TTL: lock must still be held by the same token
        await asyncio.sleep(3.0)
        assert await client.get(occ.lock_key) == occ.lock_token
        assert await client.pttl(occ.lock_key) > 0

        _GATE_CONTINUE.set()
        await asyncio.wait_for(worker_task, timeout=10.0)

        assert await client.get(occ.lock_key) is None
        final_occ = await store.get_occurrence(occ.occurrence_id)
        assert final_occ is not None
        assert final_occ.status == OccurrenceStatus.COMPLETED
        assert not [
            t for t in asyncio.all_tasks() if "_lock_heartbeat" in repr(t.get_coro())
        ]


@pytest.mark.asyncio
async def test_extend_overlap_lock_requires_matching_token(
    redis_worker_env: tuple[aioredis.Redis, str, str, str],
) -> None:
    client, sched_prefix, _, _ = redis_worker_env
    store = RedisScheduleStore(redis_client=client, prefix=sched_prefix)
    lock_key = f"{sched_prefix}overlap:manual"
    await client.set(lock_key, "owner", px=500)

    assert await store.extend_overlap_lock(lock_key, "stale-token", 60) is False
    assert await client.pttl(lock_key) <= 500
    assert await store.extend_overlap_lock(lock_key, "owner", 60) is True
    assert await client.pttl(lock_key) > 50_000


@pytest.mark.asyncio
async def test_worker_skips_duplicate_delivery_of_completed_occurrence(
    redis_worker_env: tuple[aioredis.Redis, str, str, str],
) -> None:
    client, sched_prefix, queue_prefix, _ = redis_worker_env
    clock = FakeClock(datetime(2026, 9, 3, 10, 0, tzinfo=UTC))
    store = RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
    dispatcher = QueueDispatcher(redis_client=client, prefix=queue_prefix)
    sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)
    container = Container()
    container.instance(Scheduler, sched)
    ran: list[str] = []

    @sched.task("dup_callable")
    async def run_callable(msg: str) -> None:
        ran.append(msg)

    with bind_container(container):
        await sched.every(1).minutes().name("dup").queue("sched_q").run(
            "dup_callable", "once"
        )
        clock.advance(minutes=1)
        (occ,) = await sched.tick()

        async def _drain() -> QueueWorker:
            worker = QueueWorker(
                queues="sched_q",
                dispatcher=dispatcher,
                sleep=0.05,
                promote_interval=0.05,
                stop_when_empty=True,
            )
            await asyncio.wait_for(worker.work(), timeout=10.0)
            return worker

        await _drain()
        assert ran == ["once"]

        # Simulate a recovery re-push of the same occurrence after it already completed
        await dispatcher.push(sched._build_job_for_occurrence(occ), queue="sched_q")
        await _drain()
        assert ran == ["once"]

        final_occ = await store.get_occurrence(occ.occurrence_id)
        assert final_occ is not None
        assert final_occ.status == OccurrenceStatus.COMPLETED
