from __future__ import annotations

import os
import time
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
from swiftpy.queue.job import Job, JobSerializationError
from swiftpy.scheduling import (
    InvalidScheduleError,
    MemoryScheduleStore,
    OccurrenceStatus,
    RedisScheduleStore,
    ScheduledCallableJob,
    ScheduledTaskDispatched,
    ScheduledTaskFailed,
    Scheduler,
    ScheduleStore,
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


class QueueTestReportJob(Job):
    queue = "billing"
    tries = 3
    backoff = 5
    timeout = 90.0

    def __init__(self, account_id: int = 1, label: str = "monthly") -> None:
        super().__init__(account_id=account_id, label=label)
        self.account_id = account_id
        self.label = label

    async def handle(self) -> None:
        return None


class FlakyQueueWrapper:
    def __init__(self, inner: QueueDispatcher, *, fail_times: int) -> None:
        self.inner = inner
        self.fail_times = fail_times
        self.calls = 0

    async def push(
        self,
        job: Job,
        queue: str | None = None,
        delay: float = 0,
    ) -> str:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("Simulated Redis network outage during push")
        return await self.inner.push(job, queue=queue, delay=delay)


@pytest.fixture(autouse=True)
def _clean_state() -> Generator[None, None, None]:
    original_trusted = Job._trusted_classes.copy()
    original_resolver = get_callable_resolver()
    Job.allow_job_class(QueueTestReportJob)
    Scheduler.set_instance(None)
    EventDispatcher.set_instance(EventDispatcher())
    Context.clear()
    try:
        yield
    finally:
        Job._trusted_classes = original_trusted
        set_callable_resolver(original_resolver)
        Scheduler.set_instance(None)
        EventDispatcher.set_instance(None)
        Context.clear()


@pytest.fixture
async def redis_queue_env() -> AsyncGenerator[
    tuple[aioredis.Redis, str, str], None
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
    sched_prefix = f"swiftpy_sched_p5:{run_id}:"
    queue_prefix = f"swiftpy_queue_p5:{run_id}:"
    try:
        await client.ping()
        yield client, sched_prefix, queue_prefix
    finally:
        for pattern in (f"{sched_prefix}*", f"{queue_prefix}*"):
            keys: list[str] = [key async for key in client.scan_iter(match=pattern)]
            if keys:
                await client.delete(*keys)
        await client.aclose()


@pytest.mark.asyncio
async def test_crash_after_claim_reclaims_and_pushes_signed_job_to_real_queue_dispatcher(
    redis_queue_env: tuple[aioredis.Redis, str, str],
) -> None:
    client, sched_prefix, queue_prefix = redis_queue_env
    clock = FakeClock(datetime(2026, 8, 1, 10, 0, tzinfo=UTC))

    store_crashed = RedisScheduleStore(
        redis_client=client,
        prefix=sched_prefix,
        clock=clock,
    )
    store_recovery = RedisScheduleStore(
        redis_client=client,
        prefix=sched_prefix,
        clock=clock,
    )
    real_dispatcher = QueueDispatcher(redis_client=client, prefix=queue_prefix)

    dispatched_events: list[ScheduledTaskDispatched] = []
    events = EventDispatcher.get_instance()

    def _on_dispatched(evt: ScheduledTaskDispatched) -> None:
        dispatched_events.append(evt)

    events.listen(ScheduledTaskDispatched, _on_dispatched)

    sched1 = Scheduler(
        store=store_crashed,
        dispatcher=real_dispatcher,
        clock=clock,
        dispatch_lease_seconds=20.0,
        max_dispatch_attempts=4,
        scheduler_id="crashed-node",
    )
    sched2 = Scheduler(
        store=store_recovery,
        dispatcher=real_dispatcher,
        clock=clock,
        dispatch_lease_seconds=20.0,
        max_dispatch_attempts=4,
        scheduler_id="recovery-node",
    )

    rec = (
        await sched1.every(5)
        .minutes()
        .name("billing-rollup")
        .without_overlapping(lock_ttl=300)
        .run(QueueTestReportJob, 42, label="aug")
    )

    # Simulate crashed process leaving an un-pushed dispatch_pending claim in Redis
    clock.advance(minutes=5)
    claims = await store_crashed.claim_due(
        now=clock.now,
        claimed_by="crashed-node",
        default_grace_seconds=60.0,
        default_lock_ttl=300,
        dispatch_lease_seconds=20.0,
        next_run_calculator=sched1._compute_next_run,
    )
    assert len(claims) == 1
    pending_occ = claims[0].occurrence
    assert pending_occ.status == OccurrenceStatus.DISPATCH_PENDING
    assert await real_dispatcher.size("billing") == 0
    assert (
        await client.zscore(f"{sched_prefix}pending_dispatch", pending_occ.occurrence_id)
        is not None
    )

    # Before lease expiry (+10s), recovery tick must NOT prematurely reclaim or push
    clock.advance(seconds=10)
    assert await sched2.recover_pending_dispatches() == []
    assert await real_dispatcher.size("billing") == 0

    # After lease expiry (+11s -> total +21s > 20s), recovery pushes signed Job onto Redis
    clock.advance(seconds=11)
    recovered = await sched2.recover_pending_dispatches()
    assert len(recovered) == 1
    occ = recovered[0]
    assert occ.occurrence_id == pending_occ.occurrence_id
    assert occ.status == OccurrenceStatus.DISPATCHED
    assert occ.dispatch_attempts == 2
    assert occ.job_id is not None
    assert (
        await client.zscore(f"{sched_prefix}pending_dispatch", occ.occurrence_id)
        is None
    )
    assert len(dispatched_events) == 1
    assert dispatched_events[0].occurrence.job_id == occ.job_id

    # Verify raw Redis queue item is HMAC-signed and preserves all schedule metadata
    assert await real_dispatcher.size("billing") == 1
    raw_payload = await client.lpop(f"{queue_prefix}billing")
    assert isinstance(raw_payload, str)
    deserialized = Job.deserialize(raw_payload)
    assert isinstance(deserialized, QueueTestReportJob)
    assert deserialized.job_id == occ.job_id
    assert deserialized.account_id == 42
    assert deserialized.label == "aug"
    assert deserialized.data["_swiftpy_schedule_id"] == rec.id
    assert deserialized.data["_swiftpy_schedule_occurrence"] == occ.occurrence_id
    assert deserialized.data["_swiftpy_schedule_lock_key"] == pending_occ.lock_key
    assert deserialized.data["_swiftpy_schedule_lock_token"] == pending_occ.lock_token


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "redis"])
async def test_monotonic_guards_prevent_stale_status_regressions(
    backend: str,
    redis_queue_env: tuple[aioredis.Redis, str, str],
) -> None:
    client, sched_prefix, _queue_prefix = redis_queue_env
    clock = FakeClock(datetime(2026, 8, 1, 12, 0, tzinfo=UTC))

    store: ScheduleStore = (
        MemoryScheduleStore(clock=clock)
        if backend == "memory"
        else RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
    )
    sched = Scheduler(store=store, clock=clock)

    rec = (
        await sched.every(5)
        .minutes()
        .name(f"mono-{backend}")
        .without_overlapping(lock_ttl=300)
        .run(QueueTestReportJob, 1)
    )

    clock.advance(minutes=5)
    claims = await store.claim_due(
        now=clock.now,
        claimed_by="node-a",
        default_grace_seconds=60.0,
        default_lock_ttl=300,
        dispatch_lease_seconds=20.0,
        next_run_calculator=sched._compute_next_run,
    )
    occ_id = claims[0].occurrence.occurrence_id
    dispatched = await store.confirm_dispatched(occ_id, job_id="job-1", now=clock.now)
    assert dispatched is not None and dispatched.status == OccurrenceStatus.DISPATCHED

    running = await store.mark_running(occ_id, now=clock.now)
    assert running is not None and running.status == OccurrenceStatus.RUNNING

    # Stale confirm_dispatched or mark_dispatch_attempt_failed must NOT regress RUNNING
    stale_confirm = await store.confirm_dispatched(occ_id, job_id="job-stale", now=clock.now)
    assert stale_confirm is not None
    assert stale_confirm.status == OccurrenceStatus.RUNNING
    assert stale_confirm.job_id == "job-1"

    stale_fail_attempt = await store.mark_dispatch_attempt_failed(
        occ_id, error="late error", now=clock.now, max_dispatch_attempts=1
    )
    assert stale_fail_attempt is not None
    assert stale_fail_attempt.status == OccurrenceStatus.RUNNING

    completed = await store.mark_completed(occ_id, now=clock.now)
    assert completed is not None and completed.status == OccurrenceStatus.COMPLETED

    # Terminal COMPLETED must NEVER regress to RUNNING, FAILED, or DISPATCHED
    after_run = await store.mark_running(occ_id, now=clock.now)
    assert after_run is not None and after_run.status == OccurrenceStatus.COMPLETED
    after_fail = await store.mark_failed(occ_id, error="late err", now=clock.now)
    assert after_fail is not None and after_fail.status == OccurrenceStatus.COMPLETED
    after_conf = await store.confirm_dispatched(occ_id, job_id="job-2", now=clock.now)
    assert after_conf is not None and after_conf.status == OccurrenceStatus.COMPLETED

    # Verify FAILED -> mark_completed also cannot overwrite failure
    clock.advance(minutes=5)
    claims2 = await store.claim_due(
        now=clock.now,
        claimed_by="node-b",
        default_grace_seconds=60.0,
        default_lock_ttl=300,
        dispatch_lease_seconds=20.0,
        next_run_calculator=sched._compute_next_run,
    )
    occ2_id = claims2[0].occurrence.occurrence_id
    await store.confirm_dispatched(occ2_id, job_id="job-f1", now=clock.now)
    await store.mark_failed(occ2_id, error="permanent failure", now=clock.now)
    after_failed_complete = await store.mark_completed(occ2_id, now=clock.now)
    assert after_failed_complete is not None
    assert after_failed_complete.status == OccurrenceStatus.FAILED

    # Clean up remaining schedule record
    assert await store.delete_schedule(rec.id) is True


@pytest.mark.asyncio
async def test_scheduler_delay_and_callable_job_roundtrip_on_real_queue_dispatcher(
    redis_queue_env: tuple[aioredis.Redis, str, str],
) -> None:
    client, sched_prefix, queue_prefix = redis_queue_env
    clock = FakeClock(datetime(2026, 8, 1, 14, 0, tzinfo=UTC))
    real_dispatcher = QueueDispatcher(redis_client=client, prefix=queue_prefix)
    store = RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)

    container = Container()
    sched = Scheduler(store=store, dispatcher=real_dispatcher, clock=clock)
    container.instance(Scheduler, sched)
    container.instance(QueueDispatcher, real_dispatcher)

    executed: list[tuple[str, str, int]] = []

    @sched.task("async_digest")
    async def async_digest(region: str, *, batch: int = 1) -> None:
        executed.append(("async", region, batch))

    @sched.task("sync_digest")
    def sync_digest(region: str, *, batch: int = 1) -> None:
        executed.append(("sync", region, batch))

    with bind_container(container):
        t0 = time.time()
        job1_id = await sched.delay(
            QueueTestReportJob,
            seconds=2.5,
            queue="delayed-reports",
            args=(101,),
            kwargs={"label": "cls"},
        )
        job2_id = await sched.delay(
            QueueTestReportJob(202, label="inst"),
            seconds=timedelta(seconds=4),
            queue="delayed-reports",
        )
        job3_id = await sched.delay(
            "async_digest",
            seconds=1.5,
            queue="callables",
            args=("eu-west",),
            kwargs={"batch": 7},
        )
        job4_id = await sched.delay(
            sync_digest,
            seconds=3.0,
            queue="callables",
            kwargs={"region": "us-east", "batch": 9},
        )

        assert await real_dispatcher.delayed_size("delayed-reports") == 2
        assert await real_dispatcher.delayed_size("callables") == 2

        raw_delayed_entries: object = await client.zrange(
            f"{queue_prefix}delayed-reports:delayed", 0, -1, withscores=True
        )
        assert isinstance(raw_delayed_entries, list)
        delayed_reports: list[tuple[str, float]] = []
        for entry in raw_delayed_entries:
            assert isinstance(entry, tuple) and len(entry) == 2
            payload_raw, score_raw = entry
            assert isinstance(payload_raw, str) and isinstance(score_raw, (int, float))
            delayed_reports.append((payload_raw, float(score_raw)))

        assert len(delayed_reports) == 2
        assert all(score >= t0 + 2.0 for _, score in delayed_reports)

        deser_reports = [Job.deserialize(raw) for raw, _ in delayed_reports]
        assert {j.job_id for j in deser_reports} == {job1_id, job2_id}
        assert {
            (j.account_id, j.label)
            for j in deser_reports
            if isinstance(j, QueueTestReportJob)
        } == {(101, "cls"), (202, "inst")}

        raw_callable_list: object = await client.zrange(
            f"{queue_prefix}callables:delayed", 0, -1
        )
        assert isinstance(raw_callable_list, list)
        callable_payloads = [item for item in raw_callable_list if isinstance(item, str)]
        assert len(callable_payloads) == 2
        deser_callables = [Job.deserialize(raw) for raw in callable_payloads]
        assert {j.job_id for j in deser_callables} == {job3_id, job4_id}

        # Execute deserialized ScheduledCallableJob payloads in Redis order
        for cjob in deser_callables:
            assert isinstance(cjob, ScheduledCallableJob)
            await cjob.handle()
        assert executed == [("async", "eu-west", 7), ("sync", "us-east", 9)]

        # Tampered HMAC payload must fail verification before executing anything
        tampered = callable_payloads[0].replace("eu-west", "evil")
        with pytest.raises(JobSerializationError, match="signature verification failed"):
            Job.deserialize(tampered)

        # Unregistered callable raises InvalidScheduleError safely
        missing = ScheduledCallableJob(task_name="nonexistent.callable")
        with pytest.raises(InvalidScheduleError, match="not registered"):
            await missing.handle()


@pytest.mark.asyncio
async def test_transient_push_failures_recover_and_exhaustion_frees_lock(
    redis_queue_env: tuple[aioredis.Redis, str, str],
) -> None:
    client, sched_prefix, queue_prefix = redis_queue_env
    clock = FakeClock(datetime(2026, 8, 1, 16, 0, tzinfo=UTC))
    real_dispatcher = QueueDispatcher(redis_client=client, prefix=queue_prefix)
    flaky_dispatcher = FlakyQueueWrapper(real_dispatcher, fail_times=2)
    store = RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)

    failed_events: list[ScheduledTaskFailed] = []
    EventDispatcher.get_instance().listen(
        ScheduledTaskFailed, lambda evt: failed_events.append(evt)
    )

    # Phase A: transient outage (2 failures, succeeds on 3rd attempt onto real Redis)
    sched_recover = Scheduler(
        store=store,
        dispatcher=flaky_dispatcher,
        clock=clock,
        max_dispatch_attempts=3,
    )
    await sched_recover.every(5).minutes().name("flaky-job").without_overlapping(
        lock_ttl=300
    ).run(QueueTestReportJob, 5)

    clock.advance(minutes=5)
    t1 = await sched_recover.tick()
    assert len(t1) == 1
    assert t1[0].status == OccurrenceStatus.DISPATCH_PENDING
    assert await real_dispatcher.size("billing") == 0

    t2 = await sched_recover.tick()
    assert len(t2) == 1
    assert t2[0].status == OccurrenceStatus.DISPATCH_PENDING
    assert t2[0].dispatch_attempts == 2
    assert await real_dispatcher.size("billing") == 0

    t3 = await sched_recover.tick()
    assert len(t3) == 1
    assert t3[0].status == OccurrenceStatus.DISPATCHED
    assert t3[0].dispatch_attempts == 3
    assert failed_events == []
    assert await real_dispatcher.size("billing") == 1
    rec_after = await store.get_schedule("flaky-job")
    assert rec_after is not None and rec_after.last_error is None

    # Release lock from completed occurrence before testing permanent exhaustion
    await store.mark_completed(t3[0].occurrence_id, now=clock.now)

    # Phase B: outage exceeding max_dispatch_attempts fails permanently & releases Redis lock
    down_dispatcher = FlakyQueueWrapper(real_dispatcher, fail_times=10)
    sched_down = Scheduler(
        store=store,
        dispatcher=down_dispatcher,
        clock=clock,
        max_dispatch_attempts=2,
    )
    clock.advance(minutes=5)
    f1 = await sched_down.tick()
    assert len(f1) == 1 and f1[0].status == OccurrenceStatus.DISPATCH_PENDING
    f2 = await sched_down.tick()
    assert len(f2) == 1 and f2[0].status == OccurrenceStatus.FAILED
    assert len(failed_events) == 1
    assert "Simulated Redis network outage" in failed_events[0].error
    assert f2[0].lock_key is not None
    assert await client.get(f2[0].lock_key) is None
