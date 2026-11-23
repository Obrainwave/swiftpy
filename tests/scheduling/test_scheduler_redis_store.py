from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis.asyncio as aioredis

from swiftpy.core.config import Config
from swiftpy.core.container import Container, bind_container
from swiftpy.core.context import Context
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.queue.job import Job
from swiftpy.scheduling import (
    DuplicateScheduleError,
    MisfirePolicy,
    OccurrenceStatus,
    RedisScheduleStore,
    ScheduleNotFoundError,
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


class SharedFakeDispatcher:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.pushed: list[tuple[Job, str | None, float]] = []
        self.fail_times = fail_times
        self.lock = asyncio.Lock()

    async def push(
        self,
        job: Job,
        queue: str | None = None,
        delay: float = 0,
    ) -> str:
        async with self.lock:
            if self.fail_times > 0:
                self.fail_times -= 1
                raise RuntimeError("Simulated Redis push failure")
            self.pushed.append((job, queue, delay))
            return job.job_id


class RedisSampleJob(Job):
    queue = "reports"
    tries = 2
    backoff = 5
    timeout = 60.0

    def __init__(self, item_id: int = 1, kind: str = "daily") -> None:
        super().__init__(item_id=item_id, kind=kind)
        self.item_id = item_id
        self.kind = kind

    async def handle(self) -> None:
        return None


def _test_redis_params(database: int = 1) -> tuple[str, int, str | None, int]:
    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD") or None
    db = int(os.getenv("REDIS_QUEUE_DB", str(database)))
    return host, port, password, db


@pytest.fixture(autouse=True)
def _reset_scheduling_env() -> Generator[None, None, None]:
    original_trusted = Job._trusted_classes.copy()
    Job.allow_job_class(RedisSampleJob)
    Scheduler.set_instance(None)
    EventDispatcher.set_instance(EventDispatcher())
    Context.clear()
    try:
        yield
    finally:
        Job._trusted_classes = original_trusted
        Scheduler.set_instance(None)
        EventDispatcher.set_instance(None)
        Context.clear()


@pytest.fixture
async def redis_env() -> AsyncGenerator[tuple[aioredis.Redis, str], None]:
    host, port, password, db = _test_redis_params(1)
    client = aioredis.Redis(
        host=host,
        port=port,
        password=password,
        db=db,
        decode_responses=True,
        encoding_errors="replace",
    )
    prefix = f"swiftpy_schedule_test:{uuid.uuid4().hex[:10]}:"
    try:
        await client.ping()
        yield client, prefix
    finally:
        keys: list[str] = [key async for key in client.scan_iter(match=f"{prefix}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@pytest.mark.asyncio
async def test_redis_store_connection_defaults_and_isolation(
    redis_env: tuple[aioredis.Redis, str],
) -> None:
    external_client, prefix = redis_env
    host, port, password, _db = _test_redis_params(1)

    container = Container()
    container.instance(
        Config,
        Config(
            {
                "queue": {"connections": {"redis": {"database": 1}}},
                "redis": {"cache_db": 0},
            }
        ),
    )

    with bind_container(container):
        owned_store = RedisScheduleStore(
            host=host,
            port=port,
            password=password,
            prefix=prefix,
        )
        assert owned_store.database == 1
        assert owned_store.prefix == prefix

        client = await owned_store.get_client()
        await client.set(f"{prefix}probe", "ok")
        assert await external_client.get(f"{prefix}probe") == "ok"
        await owned_store.close()
        assert owned_store._client is None

    # External client passed in must remain open when store.close() is called
    shared_store = RedisScheduleStore(redis_client=external_client, prefix=prefix)
    await shared_store.close()
    assert await external_client.ping() is True


@pytest.mark.asyncio
async def test_redis_store_crud_upsert_state_preservation_and_nullable_cleanup(
    redis_env: tuple[aioredis.Redis, str],
) -> None:
    client, prefix = redis_env
    clock = FakeClock(datetime(2026, 7, 1, 9, 0, tzinfo=UTC))
    dispatcher = SharedFakeDispatcher()

    # Boot 1: register named cron schedule + run once so operational state populates
    store1 = RedisScheduleStore(redis_client=client, prefix=prefix, clock=clock)
    sched1 = Scheduler(
        store=store1,
        dispatcher=dispatcher,
        clock=clock,
        scheduler_id="boot-1",
    )
    cron_rec = (
        await sched1.cron("0 * * * *")
        .name("hourly-sync")
        .without_overlapping(lock_ttl=300)
        .run(RedisSampleJob, 10)
    )
    assert cron_rec.lock_ttl == 300
    assert cron_rec.next_run_at == datetime(2026, 7, 1, 10, 0, tzinfo=UTC)

    clock.advance(hours=1)
    tick1 = await sched1.tick()
    assert len(tick1) == 1
    assert tick1[0].status == OccurrenceStatus.DISPATCHED
    await store1.mark_completed(tick1[0].occurrence_id, now=clock.now)
    await sched1.disable("hourly-sync")

    # Also test empty args=[] and kwargs={} round-tripping through Lua Hashes cleanly
    empty_args_rec = (
        await sched1.every(10)
        .minutes()
        .name("empty-args")
        .run(RedisSampleJob)
    )
    loaded_empty = await store1.get_schedule(empty_args_rec.id)
    assert loaded_empty is not None
    assert loaded_empty.args == []
    assert loaded_empty.kwargs == {}

    # Duplicate without upsert=True fails with DuplicateScheduleError
    with pytest.raises(DuplicateScheduleError):
        await sched1.cron("0 * * * *").name("hourly-sync").run(RedisSampleJob, 10)

    # Boot 2: fresh Scheduler + RedisScheduleStore (restart) upserts same timing
    store2 = RedisScheduleStore(redis_client=client, prefix=prefix, clock=clock)
    sched2 = Scheduler(
        store=store2,
        dispatcher=dispatcher,
        clock=clock,
        scheduler_id="boot-2",
    )
    clock.advance(minutes=10)  # T=10:10
    same_timing = (
        await sched2.cron("0 * * * *")
        .name("hourly-sync")
        .upsert()
        .run(RedisSampleJob, 20, kind="updated")
    )
    assert same_timing.id == cron_rec.id
    assert same_timing.enabled is False  # Preserved across restart!
    assert same_timing.last_run_at == datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    assert same_timing.last_status == OccurrenceStatus.COMPLETED
    assert same_timing.next_run_at == datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    assert same_timing.args == [20]
    assert same_timing.kwargs == {"kind": "updated"}
    # Omitted lock_ttl on RedisSampleJob resolves derived default (60*2 + 5*1 + 60 = 185)
    assert same_timing.lock_ttl == 185

    # Re-enable without explicit next_run_at directly on store restores due ZSET via next_run_ts
    re_enabled = await store2.set_enabled("hourly-sync", True)
    assert re_enabled.enabled is True
    assert await client.zscore(f"{prefix}due", cron_rec.id) == pytest.approx(
        datetime(2026, 7, 1, 11, 0, tzinfo=UTC).timestamp()
    )

    # Boot 3: upsert switching trigger clears stale cron expression field cleanly
    changed_trigger = (
        await sched2.every(15)
        .minutes()
        .name("hourly-sync")
        .upsert()
        .run(RedisSampleJob, 30)
    )
    assert changed_trigger.trigger == TriggerType.INTERVAL
    assert changed_trigger.expression is None
    assert changed_trigger.interval_seconds == 900.0
    assert changed_trigger.next_run_at == datetime(2026, 7, 1, 10, 25, tzinfo=UTC)

    # Missing schedule errors & list ordering
    with pytest.raises(ScheduleNotFoundError):
        await sched2.disable("missing-schedule")

    listed = await sched2.list_schedules()
    assert [r.name for r in listed] == ["empty-args", "hourly-sync"]


@pytest.mark.asyncio
async def test_redis_multi_instance_atomic_claim_due_race(
    redis_env: tuple[aioredis.Redis, str],
) -> None:
    client, prefix = redis_env
    clock = FakeClock(datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    shared_dispatcher = SharedFakeDispatcher()

    stores = [
        RedisScheduleStore(redis_client=client, prefix=prefix, clock=clock)
        for _ in range(8)
    ]
    schedulers = [
        Scheduler(
            store=stores[i],
            dispatcher=shared_dispatcher,
            clock=clock,
            scheduler_id=f"node-{i}",
        )
        for i in range(8)
    ]

    await schedulers[0].cron("5 10 * * *").name("race-cron").run(RedisSampleJob, 1)
    await schedulers[0].every(5).minutes().name("race-interval").run(RedisSampleJob, 2)
    await schedulers[0].schedule(
        RedisSampleJob,
        at=datetime(2026, 7, 1, 10, 5, tzinfo=UTC),
        name="race-once",
        args=(3,),
    )

    # Advance to T=10:05:00 when ALL THREE schedules are simultaneously due
    clock.advance(minutes=5)

    # 8 concurrent scheduler instances race tick() simultaneously against Redis
    batch_results = await asyncio.gather(*(sched.tick() for sched in schedulers))
    all_outcomes = [occ for batch in batch_results for occ in batch]

    # Exactly 1 occurrence per schedule across all 8 competing instances!
    assert len(all_outcomes) == 3
    assert len(shared_dispatcher.pushed) == 3
    assert {occ.schedule_id for occ in all_outcomes} == {
        "race-cron",
        "race-interval",
        "race-once",
    }
    assert all(occ.status == OccurrenceStatus.DISPATCHED for occ in all_outcomes)

    once_rec = await stores[0].get_schedule("race-once")
    assert once_rec is not None
    assert once_rec.enabled is False
    assert once_rec.next_run_at is None

    # Immediate duplicate tick must emit zero duplicates
    assert await schedulers[3].tick() == []


@pytest.mark.asyncio
async def test_redis_overlap_lock_px_expiry_and_token_compare_and_delete(
    redis_env: tuple[aioredis.Redis, str],
) -> None:
    client, prefix = redis_env
    clock = FakeClock(datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    dispatcher = SharedFakeDispatcher()
    store = RedisScheduleStore(redis_client=client, prefix=prefix, clock=clock)
    sched = Scheduler(
        store=store,
        dispatcher=dispatcher,
        clock=clock,
        grace_seconds=30.0,
    )

    rec = (
        await sched.every(2)
        .seconds()
        .name("px-locked")
        .without_overlapping(lock_ttl=1)
        .run(RedisSampleJob, 7)
    )

    # T=10:00:02 -> occ1 claimed, Redis PX 1000ms lock set
    clock.advance(seconds=2)
    tick1 = await sched.tick()
    assert len(tick1) == 1
    occ1 = tick1[0]
    assert occ1.status == OccurrenceStatus.DISPATCHED
    assert occ1.lock_key == f"{prefix}overlap:{rec.id}"
    assert occ1.lock_token is not None

    # Wrong token must fail to release lock
    assert await store.release_overlap_lock(occ1.lock_key, "wrong-token") is False
    assert await client.get(occ1.lock_key) == occ1.lock_token

    # Wait briefly (lock STILL active within 1000ms) & advance simulated clock +2s -> SKIPPED!
    clock.advance(seconds=2)
    tick2 = await sched.tick()
    assert len(tick2) == 1
    assert tick2[0].status == OccurrenceStatus.SKIPPED
    assert "overlap" in (tick2[0].error or "").lower()

    # Wait past real Redis PX TTL (1s) so occ1's lock expires naturally
    await asyncio.sleep(1.15)
    clock.advance(seconds=2)
    tick3 = await sched.tick()
    assert len(tick3) == 1
    occ3 = tick3[0]
    assert occ3.status == OccurrenceStatus.DISPATCHED
    assert occ3.lock_token is not None and occ3.lock_token != occ1.lock_token

    # Stale occ1 token MUST NOT release occ3's active lock!
    assert occ3.lock_key is not None
    assert await store.release_overlap_lock(occ3.lock_key, occ1.lock_token) is False
    assert await client.get(occ3.lock_key) == occ3.lock_token

    # mark_completed releases occ3's lock cleanly
    completed = await store.mark_completed(occ3.occurrence_id, now=clock.now)
    assert completed is not None and completed.status == OccurrenceStatus.COMPLETED
    assert await client.get(occ3.lock_key) is None


@pytest.mark.asyncio
async def test_redis_reclaim_pending_dispatches_race_delete_and_retention(
    redis_env: tuple[aioredis.Redis, str],
) -> None:
    client, prefix = redis_env
    clock = FakeClock(datetime(2026, 7, 1, 10, 0, tzinfo=UTC))

    # Bound retention & per-schedule history tightly to verify pruning in Redis
    stores = [
        RedisScheduleStore(
            redis_client=client,
            prefix=prefix,
            clock=clock,
            retention_seconds=3600,
            max_occurrences_per_schedule=3,
        )
        for _ in range(6)
    ]
    dispatcher = SharedFakeDispatcher()
    sched = Scheduler(
        store=stores[0],
        dispatcher=dispatcher,
        clock=clock,
        dispatch_lease_seconds=10.0,
        max_dispatch_attempts=3,
    )

    rec = (
        await sched.every(5)
        .minutes()
        .name("recoverable")
        .without_overlapping(lock_ttl=600)
        .run(RedisSampleJob, 1)
    )

    # Simulate Scheduler crash right after claim_due before queue push
    clock.advance(minutes=5)
    outcomes = await stores[0].claim_due(
        now=clock.now,
        claimed_by="dead-node",
        default_grace_seconds=30.0,
        default_lock_ttl=600,
        dispatch_lease_seconds=10.0,
        next_run_calculator=sched._compute_next_run,
    )
    assert len(outcomes) == 1
    pending_occ = outcomes[0].occurrence
    assert pending_occ.status == OccurrenceStatus.DISPATCH_PENDING
    assert await client.zscore(f"{prefix}pending_dispatch", pending_occ.occurrence_id) is not None

    # Before lease expiry: none of the instances reclaim it
    early_retry, early_exhausted = await stores[1].reclaim_pending_dispatches(
        now=clock.now + timedelta(seconds=5),
        reclaimed_by="node-1",
        dispatch_lease_seconds=10.0,
        max_dispatch_attempts=3,
    )
    assert early_retry == []
    assert early_exhausted == []

    # Advance past lease expiration and race 6 concurrent reclaim calls
    clock.advance(seconds=11)
    reclaim_results = await asyncio.gather(
        *(
            s.reclaim_pending_dispatches(
                now=clock.now,
                reclaimed_by=f"reclaimer-{idx}",
                dispatch_lease_seconds=10.0,
                max_dispatch_attempts=3,
            )
            for idx, s in enumerate(stores)
        )
    )
    retries = [occ for to_retry, _ in reclaim_results for occ in to_retry]
    exhausted = [occ for _, exp in reclaim_results for occ in exp]
    assert len(retries) == 1
    assert len(exhausted) == 0
    assert retries[0].occurrence_id == pending_occ.occurrence_id
    assert retries[0].dispatch_attempts == 2

    # Confirm dispatch clears pending_dispatch ZSET and records job_id
    confirmed = await stores[0].confirm_dispatched(
        pending_occ.occurrence_id, job_id="job-redis-99", now=clock.now
    )
    assert confirmed is not None and confirmed.status == OccurrenceStatus.DISPATCHED
    assert await client.zscore(f"{prefix}pending_dispatch", pending_occ.occurrence_id) is None
    await stores[0].mark_completed(pending_occ.occurrence_id, now=clock.now)
    # Terminal occurrence receives positive TTL
    assert await client.ttl(f"{prefix}occ:{pending_occ.occurrence_id}") > 0

    # Test delete_schedule cancels un-dispatched pending claims and releases lock,
    # while leaving already-running occurrences intact for completion
    clock.advance(minutes=5)
    outcomes2 = await stores[0].claim_due(
        now=clock.now,
        claimed_by="node-2",
        default_grace_seconds=30.0,
        default_lock_ttl=600,
        dispatch_lease_seconds=10.0,
        next_run_calculator=sched._compute_next_run,
    )
    occ2 = outcomes2[0].occurrence
    assert occ2.lock_key is not None
    assert await client.get(occ2.lock_key) == occ2.lock_token

    assert await stores[0].delete_schedule(rec.id) is True
    assert await stores[0].delete_schedule(rec.id) is False
    cancelled_occ2 = await stores[0].get_occurrence(occ2.occurrence_id)
    assert cancelled_occ2 is not None
    assert cancelled_occ2.status == OccurrenceStatus.CANCELLED
    assert await client.get(occ2.lock_key) is None
    assert await client.zscore(f"{prefix}pending_dispatch", occ2.occurrence_id) is None

    # Verify history pruning keeps recent history bounded (max_occurrences_per_schedule=3)
    sched_pruned = Scheduler(store=stores[0], dispatcher=dispatcher, clock=clock)
    pruned_rec = (
        await sched_pruned.every(1)
        .minutes()
        .name("bounded-history")
        .on_misfire(MisfirePolicy.FIRE_ONCE)
        .run(RedisSampleJob, 99)
    )
    for _ in range(5):
        clock.advance(minutes=1)
        tick_res = await sched_pruned.tick()
        assert len(tick_res) == 1
        await stores[0].mark_completed(tick_res[0].occurrence_id, now=clock.now)

    indexed_count = await client.zcard(f"{prefix}sched_occs:{pruned_rec.id}")
    assert indexed_count <= 3


@pytest.mark.asyncio
async def test_redis_next_due_at_reads_earliest_enabled_schedule(
    redis_env: tuple[aioredis.Redis, str],
) -> None:
    client, prefix = redis_env
    clock = FakeClock(datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    store = RedisScheduleStore(redis_client=client, prefix=prefix, clock=clock)
    sched = Scheduler(store=store, dispatcher=SharedFakeDispatcher(), clock=clock)

    assert await store.next_due_at() is None

    await sched.every(20).minutes().name("later").run(RedisSampleJob, 1)
    await sched.every(5).minutes().name("sooner").run(RedisSampleJob, 2)
    assert await store.next_due_at() == clock.now + timedelta(minutes=5)

    await sched.disable("sooner")
    assert await store.next_due_at() == clock.now + timedelta(minutes=20)


@pytest.mark.asyncio
async def test_redis_tampered_schedule_hash_is_rejected_at_dispatch(
    redis_env: tuple[aioredis.Redis, str],
) -> None:
    client, prefix = redis_env
    original_key = Job._app_key
    Job.set_app_key("0123456789abcdef0123456789abcdef0123456789abcdef")
    try:
        clock = FakeClock(datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
        store = RedisScheduleStore(redis_client=client, prefix=prefix, clock=clock)
        dispatcher = SharedFakeDispatcher()
        sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)

        safe = await sched.every(1).minutes().name("safe").run(RedisSampleJob, 1)
        evil = await sched.every(1).minutes().name("evil").run(RedisSampleJob, 2)
        assert safe.signature and evil.signature

        # Anyone with raw Redis write access can rewrite execution arguments in place
        await client.hset(f"{prefix}schedule:{evil.id}", "args_json", "[666]")

        clock.advance(minutes=1)
        occurrences = {o.schedule_id: o for o in await sched.tick()}

        assert occurrences[safe.id].status == OccurrenceStatus.DISPATCHED
        assert occurrences[evil.id].status == OccurrenceStatus.FAILED
        assert [j.kwargs["item_id"] for j, _, _ in dispatcher.pushed] == [1]
        stored = await store.get_occurrence(occurrences[evil.id].occurrence_id)
        assert stored is not None
        assert stored.status == OccurrenceStatus.FAILED
    finally:
        Job.set_app_key(original_key)
