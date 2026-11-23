from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis.asyncio as aioredis

from swiftpy.core.application import Application
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container, bind_container
from swiftpy.core.context import Context
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job
from swiftpy.queue.worker import QueueWorker
from swiftpy.scheduling import (
    MemoryScheduleStore,
    OccurrenceStatus,
    RedisScheduleStore,
    Scheduler,
    TriggerType,
)


class FakeClock:
    def __init__(self, initial: datetime) -> None:
        self.now = initial

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


class CountingQueueDispatcher(QueueDispatcher):
    def __init__(
        self,
        *,
        redis_client: aioredis.Redis,
        prefix: str,
        fail_times: int = 0,
    ) -> None:
        super().__init__(redis_client=redis_client, prefix=prefix)
        self.push_calls = 0
        self.fail_times = fail_times
        self._push_lock = asyncio.Lock()

    async def push(
        self,
        job: Job,
        queue: str | None = None,
        delay: float = 0,
    ) -> str:
        async with self._push_lock:
            self.push_calls += 1
            if self.fail_times > 0:
                self.fail_times -= 1
                raise ConnectionError("Simulated Redis push outage")
        return await super().push(job, queue=queue, delay=delay)


_PAUSE_START: asyncio.Event | None = None
_PAUSE_RELEASE: asyncio.Event | None = None
_COMPLETED_TAGS: list[str] = []


class RecoveryTestJob(Job):
    queue = "recovery_q"
    tries = 2
    backoff = 0.05
    timeout = 30.0

    def __init__(self, tag: str = "job", paused: bool = False) -> None:
        super().__init__(tag=tag, paused=paused)
        self.tag = tag
        self.paused = paused

    async def handle(self) -> None:
        if self.paused:
            if _PAUSE_START is not None:
                _PAUSE_START.set()
            if _PAUSE_RELEASE is not None:
                await _PAUSE_RELEASE.wait()
        _COMPLETED_TAGS.append(self.tag)


@pytest.fixture(autouse=True)
def _clean_recovery_state() -> Generator[None, None, None]:
    global _PAUSE_START, _PAUSE_RELEASE
    original_trusted = Job._trusted_classes.copy()
    Job.allow_job_class(RecoveryTestJob)
    _PAUSE_START = None
    _PAUSE_RELEASE = None
    _COMPLETED_TAGS.clear()
    Scheduler.set_instance(None)
    QueueDispatcher.set_instance(None)
    EventDispatcher.set_instance(EventDispatcher())
    Context.clear()
    try:
        yield
    finally:
        Job._trusted_classes = original_trusted
        _PAUSE_START = None
        _PAUSE_RELEASE = None
        _COMPLETED_TAGS.clear()
        Scheduler.set_instance(None)
        QueueDispatcher.set_instance(None)
        EventDispatcher.set_instance(None)
        Context.clear()


@pytest.fixture
async def redis_recovery_env() -> AsyncGenerator[
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
    sched_prefix = f"swiftpy_sched_p8:{run_id}:"
    queue_prefix = f"swiftpy_queue_p8:{run_id}:"
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
async def test_concurrent_reclaim_race_and_worker_execution_after_crash(
    redis_recovery_env: tuple[aioredis.Redis, str, str],
) -> None:
    client, sched_prefix, queue_prefix = redis_recovery_env
    clock = FakeClock(datetime(2026, 10, 1, 8, 0, tzinfo=UTC))

    # 6 independent Scheduler instances racing recovery of one crashed claim
    stores = [
        RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
        for _ in range(6)
    ]
    dispatcher = CountingQueueDispatcher(redis_client=client, prefix=queue_prefix)
    schedulers = [
        Scheduler(
            store=stores[i],
            dispatcher=dispatcher,
            clock=clock,
            dispatch_lease_seconds=15.0,
            max_dispatch_attempts=4,
            scheduler_id=f"rec-node-{i}",
        )
        for i in range(6)
    ]

    await schedulers[0].every(5).minutes().name("crash-before-push").without_overlapping(
        lock_ttl=300
    ).run(RecoveryTestJob, "recovered-run")

    # Node 0 crashes right after atomic claim into DISPATCH_PENDING
    clock.advance(minutes=5)
    claims = await stores[0].claim_due(
        now=clock.now,
        claimed_by="rec-node-0",
        default_grace_seconds=60.0,
        default_lock_ttl=300,
        dispatch_lease_seconds=15.0,
        next_run_calculator=schedulers[0]._compute_next_run,
    )
    assert len(claims) == 1
    assert dispatcher.push_calls == 0

    # Lease expires; 6 concurrent Schedulers race recover_pending_dispatches() -> SINGLE push!
    clock.advance(seconds=16)
    results = await asyncio.gather(
        *(s.recover_pending_dispatches() for s in schedulers)
    )
    recovered = [occ for batch in results for occ in batch]
    assert len(recovered) == 1
    assert dispatcher.push_calls == 1
    assert recovered[0].status == OccurrenceStatus.DISPATCHED
    assert await dispatcher.size("recovery_q") == 1

    # QueueWorker executes recovered occurrence and releases overlap lock cleanly
    worker = QueueWorker(
        queues="recovery_q",
        dispatcher=dispatcher,
        schedule_store=stores[0],
        sleep=0.02,
        stop_when_empty=True,
    )
    await asyncio.wait_for(worker.work(), timeout=5.0)
    assert worker.jobs_processed == 1
    assert _COMPLETED_TAGS == ["recovered-run"]
    assert recovered[0].lock_key is not None
    assert await client.get(recovered[0].lock_key) is None


@pytest.mark.asyncio
async def test_simulated_redis_failures_during_claim_and_push_preserve_consistency(
    redis_recovery_env: tuple[aioredis.Redis, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, sched_prefix, queue_prefix = redis_recovery_env
    clock = FakeClock(datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    store = RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
    flaky_disp = CountingQueueDispatcher(
        redis_client=client, prefix=queue_prefix, fail_times=1
    )
    sched = Scheduler(
        store=store,
        dispatcher=flaky_disp,
        clock=clock,
        max_dispatch_attempts=3,
    )

    rec = await sched.cron("5 9 * * *").name("resilient-cron").run(
        RecoveryTestJob, "after-outages"
    )
    assert rec.next_run_at == datetime(2026, 10, 1, 9, 5, tzinfo=UTC)

    # 1. Inject failure during atomic _eval_lua inside claim_due()
    clock.advance(minutes=5)
    orig_eval_lua = store._eval_lua

    async def _fail_once(
        c: aioredis.Redis, script: str, numkeys: int, *keys_and_args: str | float
    ) -> object:
        raise ConnectionError("Simulated Redis drop during claim Lua")

    monkeypatch.setattr(store, "_eval_lua", _fail_once)
    with pytest.raises(ConnectionError, match="claim Lua"):
        await sched.tick()

    # Schedule state & due ZSET remain completely uncorrupted after failed claim
    monkeypatch.setattr(store, "_eval_lua", orig_eval_lua)
    intact = await store.get_schedule("resilient-cron")
    assert intact is not None
    assert intact.enabled is True
    assert intact.next_run_at == datetime(2026, 10, 1, 9, 5, tzinfo=UTC)
    assert intact.last_claimed_occurrence is None

    # 2. Next tick succeeds in claiming, hits 1 simulated push failure -> stays DISPATCH_PENDING
    res1 = await sched.tick()
    assert len(res1) == 1
    assert res1[0].status == OccurrenceStatus.DISPATCH_PENDING
    assert await flaky_disp.size("recovery_q") == 0

    # 3. Subsequent tick recovers pending dispatch cleanly and pushes onto real Redis
    res2 = await sched.tick()
    assert len(res2) == 1
    assert res2[0].status == OccurrenceStatus.DISPATCHED
    assert res2[0].dispatch_attempts == 2
    assert await flaky_disp.size("recovery_q") == 1


@pytest.mark.asyncio
async def test_crashed_worker_overlap_ttl_expiry_and_stale_release_protection(
    redis_recovery_env: tuple[aioredis.Redis, str, str],
) -> None:
    client, sched_prefix, queue_prefix = redis_recovery_env
    clock = FakeClock(datetime(2026, 10, 1, 10, 0, tzinfo=UTC))
    store = RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
    dispatcher = CountingQueueDispatcher(redis_client=client, prefix=queue_prefix)
    sched = Scheduler(
        store=store,
        dispatcher=dispatcher,
        clock=clock,
        grace_seconds=30.0,
    )

    rec = (
        await sched.every(2)
        .seconds()
        .name("crash-ttl-recover")
        .without_overlapping(lock_ttl=1)
        .run(RecoveryTestJob, "ttl-job")
    )

    # T=10:00:02: occ1 claimed & dispatched, popped by worker that crashes mid-execution
    clock.advance(seconds=2)
    t1 = await sched.tick()
    occ1 = t1[0]
    assert occ1.lock_key is not None and occ1.lock_token is not None
    raw1 = await dispatcher.pop_and_lease("recovery_q", "dead-worker", lease_duration=60.0)
    assert raw1 is not None
    await store.mark_running(occ1.occurrence_id, now=clock.now)
    # Lock held -> T=10:00:04 skipped while worker alive/within lock_ttl
    clock.advance(seconds=2)
    t2 = await sched.tick()
    assert len(t2) == 1 and t2[0].status == OccurrenceStatus.SKIPPED

    # Wait past lock_ttl=1s -> lock expires so dead/OOM worker cannot stall schedule permanently
    await asyncio.sleep(1.15)
    clock.advance(seconds=2)
    t3 = await sched.tick()
    assert len(t3) == 1
    occ_new = t3[0]
    assert occ_new.status == OccurrenceStatus.DISPATCHED
    assert occ_new.lock_token is not None and occ_new.lock_token != occ1.lock_token
    assert await client.get(f"{sched_prefix}overlap:{rec.id}") == occ_new.lock_token

    # Stale slow worker #1 tries late direct release & late mark_completed -> MUST NOT touch occ_new's lock!
    assert await store.release_overlap_lock(occ1.lock_key, occ1.lock_token) is False
    await store.mark_completed(occ1.occurrence_id, now=clock.now)
    assert await client.get(f"{sched_prefix}overlap:{rec.id}") == occ_new.lock_token


@pytest.mark.asyncio
async def test_disable_update_and_cancel_while_occurrences_are_in_flight(
    redis_recovery_env: tuple[aioredis.Redis, str, str],
) -> None:
    global _PAUSE_START, _PAUSE_RELEASE
    client, sched_prefix, queue_prefix = redis_recovery_env
    clock = FakeClock(datetime(2026, 10, 1, 11, 0, tzinfo=UTC))
    store = RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
    dispatcher = CountingQueueDispatcher(redis_client=client, prefix=queue_prefix)
    sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)

    _PAUSE_START = asyncio.Event()
    _PAUSE_RELEASE = asyncio.Event()

    # Case A & B: RUNNING occurrence survives schedule disable() and update() intact
    await sched.every(5).minutes().name("mutable-inflight").without_overlapping(
        lock_ttl=300
    ).run(RecoveryTestJob, "inflight-tag", paused=True)

    clock.advance(minutes=5)
    t1 = await sched.tick()
    occ_inflight = t1[0]
    assert occ_inflight.lock_key is not None

    worker = QueueWorker(
        queues="recovery_q",
        dispatcher=dispatcher,
        schedule_store=store,
        sleep=0.02,
        stop_when_empty=True,
    )
    worker_task = asyncio.create_task(worker.work())
    await asyncio.wait_for(_PAUSE_START.wait(), timeout=5.0)

    # While worker is actively RUNNING occ_inflight, update cadence then disable schedule!
    updated = await sched.update("mutable-inflight", interval_seconds=900.0)
    assert updated.interval_seconds == 900.0
    disabled = await sched.disable("mutable-inflight")
    assert disabled.enabled is False

    # Future due advance must NOT fire while disabled
    clock.advance(minutes=20)
    assert await sched.tick() == []

    # Release paused worker -> occ_inflight completes, releases lock, preserves disabled + interval=900!
    _PAUSE_RELEASE.set()
    await asyncio.wait_for(worker_task, timeout=5.0)
    assert _COMPLETED_TAGS == ["inflight-tag"]
    assert await client.get(occ_inflight.lock_key) is None

    occ_done = await store.get_occurrence(occ_inflight.occurrence_id)
    assert occ_done is not None and occ_done.status == OccurrenceStatus.COMPLETED
    rec_after = await sched.get("mutable-inflight")
    assert rec_after is not None
    assert rec_after.enabled is False
    assert rec_after.interval_seconds == 900.0
    assert rec_after.trigger == TriggerType.INTERVAL

    # Case C: cancel() after job is already queued -> worker finishes job and frees lock safely
    _PAUSE_START.clear()
    await sched.every(5).minutes().name("cancel-while-queued").without_overlapping(
        lock_ttl=300
    ).run(RecoveryTestJob, "cancelled-queued", paused=False)
    clock.advance(minutes=5)
    t_cancel = await sched.tick()
    occ_queued = t_cancel[0]
    assert occ_queued.lock_key is not None
    assert await client.get(occ_queued.lock_key) == occ_queued.lock_token

    assert await sched.cancel("cancel-while-queued") is True
    assert await sched.get("cancel-while-queued") is None
    # Queued occurrence stays DISPATCHED so worker executes and frees lock cleanly
    worker2 = QueueWorker(
        queues="recovery_q",
        dispatcher=dispatcher,
        schedule_store=store,
        sleep=0.02,
        stop_when_empty=True,
    )
    await asyncio.wait_for(worker2.work(), timeout=5.0)
    assert worker2.jobs_processed == 1
    assert "cancelled-queued" in _COMPLETED_TAGS
    assert await client.get(occ_queued.lock_key) is None
    occ_q_final = await store.get_occurrence(occ_queued.occurrence_id)
    assert occ_q_final is not None and occ_q_final.status == OccurrenceStatus.COMPLETED


@pytest.mark.asyncio
async def test_concurrent_boot_upsert_synchronization_across_replicas(
    redis_recovery_env: tuple[aioredis.Redis, str, str],
) -> None:
    client, sched_prefix, queue_prefix = redis_recovery_env
    clock = FakeClock(datetime(2026, 10, 1, 14, 0, tzinfo=UTC))
    stores = [
        RedisScheduleStore(redis_client=client, prefix=sched_prefix, clock=clock)
        for _ in range(5)
    ]
    dispatcher = CountingQueueDispatcher(redis_client=client, prefix=queue_prefix)
    schedulers = [
        Scheduler(store=stores[i], dispatcher=dispatcher, clock=clock)
        for i in range(5)
    ]

    # Seed prior operational state before concurrent worker boot sync
    seed = (
        await schedulers[0].cron("0 * * * *")
        .name("boot-sync-job")
        .upsert()
        .run(RecoveryTestJob, "v1")
    )
    await schedulers[0].disable("boot-sync-job")

    clock.advance(minutes=15)
    boot_records = await asyncio.gather(
        *(
            s.cron("0 * * * *").name("boot-sync-job").upsert().run(RecoveryTestJob, "v1")
            for s in schedulers
        )
    )
    assert len({r.id for r in boot_records}) == 1
    assert all(r.id == seed.id for r in boot_records)
    assert all(r.enabled is False for r in boot_records)
    assert all(r.next_run_at == seed.next_run_at for r in boot_records)
    assert len(await stores[0].list_schedules()) == 1


def test_memory_store_production_guard_warning_and_non_durability_docs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 1. Explicit runtime durability flag & documentation
    assert MemoryScheduleStore.is_durable is False
    assert RedisScheduleStore.is_durable is True
    doc = MemoryScheduleStore.__doc__ or ""
    assert "NOT durable across process restarts" in doc
    assert "process-local" in doc

    # 2. Application rejects driver="memory" in production unless explicitly opted in
    prod_key = "base64:" + "Q" * 44
    with pytest.raises(ConfigurationError, match="non-durable"):
        Application(
            config={
                "app": {"env": "production", "key": prod_key},
                "schedule": {"driver": "memory"},
            },
            auto_discover_providers=False,
        )

    # 3. Scheduler.from_config rejects driver="memory" in production
    prod_cfg = Config(
        {
            "app": {"env": "production", "key": prod_key},
            "schedule": {"driver": "memory"},
        }
    )
    with pytest.raises(ConfigurationError, match="non-durable"):
        Scheduler.from_config(prod_cfg)

    # 4. Default driver in production resolves to durable RedisScheduleStore cleanly
    prod_default = Scheduler.from_config(
        Config({"app": {"env": "production", "key": prod_key}})
    )
    assert isinstance(prod_default.store, RedisScheduleStore)
    assert prod_default.store.is_durable is True

    # 5. Direct MemoryScheduleStore() in active production container emits warning cleanly
    c = Container()
    c.instance(Config, Config({"app": {"env": "production"}}))
    with bind_container(c), caplog.at_level(logging.WARNING, logger="swiftpy.scheduling"):
        mem = MemoryScheduleStore()
    assert mem.is_durable is False
    assert any("NOT durable" in rec.message for rec in caplog.records)
