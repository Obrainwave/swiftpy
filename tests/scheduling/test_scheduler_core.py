from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from swiftpy import Schedule, Scheduler, scheduler
from swiftpy.core.container import Container, bind_container
from swiftpy.core.context import Context
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.queue.job import Job
from swiftpy.scheduling import (
    DuplicateScheduleError,
    InvalidCronError,
    InvalidScheduleError,
    InvalidTimezoneError,
    MemoryScheduleStore,
    MisfirePolicy,
    OccurrenceStatus,
    OverlapPolicy,
    ScheduledCallableJob,
    ScheduledTaskDispatched,
    ScheduledTaskFailed,
    ScheduledTaskSkipped,
    ScheduleNotFoundError,
    ScheduleRecord,
    ScheduleRegistered,
    ScheduleSignatureError,
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


class FakeDispatcher:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.pushed: list[tuple[Job, str | None, float]] = []
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
            raise RuntimeError("Simulated Redis push failure")
        self.pushed.append((job, queue, delay))
        return job.job_id


class SampleDigestJob(Job):
    queue = "emails"
    tries = 3
    backoff = 10
    timeout = 120.0

    def __init__(self, report_id: int = 1, kind: str = "summary") -> None:
        super().__init__(report_id=report_id, kind=kind)
        self.report_id = report_id
        self.kind = kind

    async def handle(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _clean_scheduling_globals() -> Generator[None, None, None]:
    original_trusted = Job._trusted_classes.copy()
    Job.allow_job_class(SampleDigestJob)
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


@pytest.mark.asyncio
async def test_registration_builders_presets_delay_and_validation() -> None:
    clock = FakeClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    store = MemoryScheduleStore(clock=clock)
    dispatcher = FakeDispatcher()
    sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)

    reg_events: list[ScheduleRegistered] = []
    EventDispatcher.get_instance().listen(ScheduleRegistered, reg_events.append)

    # Intervals
    s_sec = await sched.every(30).seconds().name("every-30s").run(SampleDigestJob, 7, kind="sec")
    assert s_sec.trigger == TriggerType.INTERVAL
    assert s_sec.interval_seconds == 30.0
    assert s_sec.next_run_at == datetime(2026, 6, 1, 10, 0, 30, tzinfo=UTC)
    assert s_sec.queue == "emails"

    s_min = await sched.every(5).minutes().run(SampleDigestJob)
    assert s_min.interval_seconds == 300.0
    assert (await sched.every(2).hours().run(SampleDigestJob)).interval_seconds == 7200.0
    assert (await sched.every(1).days().run(SampleDigestJob)).interval_seconds == 86400.0

    # Presets
    assert (await sched.every_minute().run(SampleDigestJob)).expression == "* * * * *"
    assert (await sched.hourly().run(SampleDigestJob)).expression == "0 * * * *"
    assert (await sched.hourly_at(15).run(SampleDigestJob)).expression == "15 * * * *"
    assert (await sched.daily().run(SampleDigestJob)).expression == "0 0 * * *"
    assert (await sched.daily_at("02:30").timezone("Europe/Berlin").run(SampleDigestJob)).expression == "30 2 * * *"
    assert (await sched.weekly().run(SampleDigestJob)).expression == "0 0 * * 0"
    assert (await sched.weekly_at(1, "08:15").run(SampleDigestJob)).expression == "15 8 * * 1"
    assert (await sched.weekly_at("fri", "17:00").run(SampleDigestJob)).expression == "0 17 * * 5"
    assert (await sched.monthly().run(SampleDigestJob)).expression == "0 0 1 * *"
    assert (await sched.monthly_at(5, "06:00").run(SampleDigestJob)).expression == "0 6 5 * *"
    assert (await sched.cron("0 9 * * 1-5").run(SampleDigestJob)).expression == "0 9 * * 1-5"

    # Delayed execution directly delegates to QueueDispatcher.push(delay=...)
    delayed_id = await sched.delay(SampleDigestJob, seconds=45, args=(99,), kwargs={"kind": "fast"})
    assert delayed_id
    pushed_job, pushed_queue, pushed_delay = dispatcher.pushed[-1]
    assert isinstance(pushed_job, SampleDigestJob)
    assert pushed_job.report_id == 99
    assert pushed_queue == "emails"
    assert pushed_delay == 45.0

    # Delay rejects invalid / non-positive / boolean values
    bad_float_delays: tuple[float, ...] = (0.0, -5.0, float("nan"), float("inf"))
    for bad_delay in bad_float_delays:
        with pytest.raises(InvalidScheduleError):
            await sched.delay(SampleDigestJob, seconds=bad_delay)

    sched_any: Any = sched
    with pytest.raises(InvalidScheduleError):
        await sched_any.delay(SampleDigestJob, seconds=True)

    # Eager invalid builder & target validations
    with pytest.raises(InvalidScheduleError):
        sched.every(0)
    with pytest.raises(InvalidScheduleError):
        sched_any.every(True)
    with pytest.raises(InvalidCronError):
        sched.hourly_at(60)
    with pytest.raises(InvalidCronError):
        sched.daily_at("25:00")
    with pytest.raises(InvalidCronError):
        sched.weekly_at(9, "10:00")
    with pytest.raises(InvalidCronError):
        sched.monthly_at(32, "10:00")
    with pytest.raises(InvalidCronError):
        sched.cron("invalid cron")
    with pytest.raises(InvalidTimezoneError):
        sched.cron("0 * * * *").timezone("Invalid/Zone")
    with pytest.raises(InvalidScheduleError):
        await sched.every(1).minutes().run(SampleDigestJob(1), 2)
    with pytest.raises(InvalidScheduleError):
        await sched.every(1).minutes().run(SampleDigestJob, non_json=object())
    with pytest.raises(InvalidScheduleError):
        await sched.every(1).minutes().run(lambda: None)
    with pytest.raises(InvalidScheduleError):
        await sched.schedule(SampleDigestJob, at=clock.now - timedelta(seconds=10))
    assert len(reg_events) >= 15


@pytest.mark.asyncio
async def test_duplicate_upsert_enable_disable_update_and_cancel() -> None:
    clock = FakeClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    store = MemoryScheduleStore(clock=clock)
    dispatcher = FakeDispatcher()
    sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)

    first = await sched.cron("0 * * * *").name("hourly-job").run(SampleDigestJob, 1)
    with pytest.raises(DuplicateScheduleError):
        await sched.cron("0 * * * *").name("hourly-job").run(SampleDigestJob, 1)

    # Disable then upsert without changing timing: preserves disabled state & next_run_at!
    await sched.disable("hourly-job")
    clock.advance(minutes=10)
    upserted = (
        await sched.cron("0 * * * *")
        .name("hourly-job")
        .queue("priority")
        .upsert()
        .run(SampleDigestJob, 2)
    )
    assert upserted.id == first.id
    assert upserted.enabled is False
    assert upserted.queue == "priority"
    assert upserted.next_run_at == first.next_run_at

    # Enable recomputes future slot if stale, and update updates timing cleanly
    clock.advance(hours=2)
    enabled = await sched.enable("hourly-job")
    assert enabled.enabled is True
    assert enabled.next_run_at is not None and enabled.next_run_at > clock.now

    updated = await sched.update(
        "hourly-job",
        expression="15 * * * *",
        timezone="Europe/Berlin",
        overlap_policy=OverlapPolicy.SKIP,
    )
    assert updated.expression == "15 * * * *"
    assert updated.timezone == "Europe/Berlin"
    assert updated.overlap_policy == OverlapPolicy.SKIP

    assert await sched.cancel("hourly-job") is True
    assert await sched.cancel("hourly-job") is False
    assert await sched.get("hourly-job") is None

    with pytest.raises(ScheduleNotFoundError):
        await sched.disable("missing")


@pytest.mark.asyncio
async def test_deterministic_fake_clock_one_time_and_recurring_execution() -> None:
    clock = FakeClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    store = MemoryScheduleStore(clock=clock)
    dispatcher = FakeDispatcher()
    sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)

    executed_callables: list[str] = []

    @sched.task("ping_callable")
    async def ping_task(label: str) -> None:
        executed_callables.append(label)

    # Set global resolver for this local Scheduler test so ScheduledCallableJob finds it
    Scheduler.set_instance(sched)

    # 1. One-time schedule must NOT dispatch before run_at, and fires once cleanly
    once_rec = await sched.schedule(
        "ping_callable",
        at=clock.now + timedelta(minutes=5),
        name="one-off-ping",
        args=("welcome",),
    )
    # Also register recurring interval every 5m
    rec = await sched.every(5).minutes().name("five-min").run(SampleDigestJob, 5)

    # At T+4m59s: nothing due
    clock.advance(minutes=4, seconds=59)
    assert await sched.tick() == []
    assert dispatcher.pushed == []

    # At T+5m00s: both due -> both dispatched cleanly!
    clock.advance(seconds=1)
    dispatched = await sched.tick()
    assert len(dispatched) == 2
    assert all(occ.status == OccurrenceStatus.DISPATCHED for occ in dispatched)

    # Execute the ScheduledCallableJob directly to verify handler resolution
    callable_jobs = [
        job for job, _, _ in dispatcher.pushed if isinstance(job, ScheduledCallableJob)
    ]
    assert len(callable_jobs) == 1
    await callable_jobs[0].handle()
    assert executed_callables == ["welcome"]

    # One-time schedule is now disabled (executed once), while recurring schedule stays enabled!
    after_once = await sched.get(once_rec.id)
    assert after_once is not None
    assert after_once.enabled is False
    assert after_once.next_run_at is None
    assert after_once.last_status == OccurrenceStatus.DISPATCHED

    after_rec = await sched.get(rec.id)
    assert after_rec is not None
    assert after_rec.enabled is True
    assert after_rec.next_run_at == datetime(2026, 6, 1, 10, 10, tzinfo=UTC)

    # Duplicate tick at the exact same second must NOT double-dispatch
    assert await sched.tick() == []


@pytest.mark.asyncio
async def test_overlap_locks_token_release_and_misfire_policies() -> None:
    clock = FakeClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    store = MemoryScheduleStore(clock=clock)
    dispatcher = FakeDispatcher()
    sched = Scheduler(
        store=store,
        dispatcher=dispatcher,
        clock=clock,
        grace_seconds=30.0,
    )

    skipped_events: list[ScheduledTaskSkipped] = []
    dispatched_events: list[ScheduledTaskDispatched] = []
    EventDispatcher.get_instance().listen(ScheduledTaskSkipped, skipped_events.append)
    EventDispatcher.get_instance().listen(ScheduledTaskDispatched, dispatched_events.append)

    rec = (
        await sched.every(5)
        .minutes()
        .name("locked-sync")
        .without_overlapping(lock_ttl=310)
        .run(SampleDigestJob, 1)
    )
    assert rec.lock_ttl == 310
    derived_probe = (
        await sched.cron("0 3 * * *")
        .name("derived-lock-probe")
        .without_overlapping()
        .run(SampleDigestJob, 2)
    )
    assert derived_probe.lock_ttl == 440

    # T=10:05 -> occurrence 1 claimed & dispatched, overlap lock acquired
    clock.advance(minutes=5)
    tick1 = await sched.tick()
    assert len(tick1) == 1
    occ1 = tick1[0]
    assert occ1.status == OccurrenceStatus.DISPATCHED
    assert occ1.lock_key is not None and occ1.lock_token is not None

    # T=10:10 -> occurrence 1 still active (not completed/unlocked) -> SKIPPED!
    clock.advance(minutes=5)
    tick2 = await sched.tick()
    assert len(tick2) == 1
    assert tick2[0].status == OccurrenceStatus.SKIPPED
    assert len(skipped_events) == 1
    assert skipped_events[-1].reason == "overlap"
    # Parent recurring schedule remains enabled & advanced to 10:15
    refreshed = await sched.get(rec.id)
    assert isinstance(refreshed, ScheduleRecord)
    assert refreshed.enabled is True
    assert refreshed.next_run_at == datetime(2026, 6, 1, 10, 15, tzinfo=UTC)

    # Stale wrong token cannot release the lock
    assert await store.release_overlap_lock(occ1.lock_key, "wrong-token") is False

    # Completing occurrence 1 releases lock cleanly -> occurrence at 10:15 dispatches normally
    completed_occ = await store.mark_completed(occ1.occurrence_id, now=clock.now)
    assert completed_occ is not None and completed_occ.status == OccurrenceStatus.COMPLETED
    clock.advance(minutes=5)
    tick3 = await sched.tick()
    assert len(tick3) == 1
    assert tick3[0].status == OccurrenceStatus.DISPATCHED

    # Complete tick3 and test lock_ttl expiry failsafe + stale release protection
    await store.mark_completed(tick3[0].occurrence_id, now=clock.now)
    # T=10:20 fires occ4
    clock.advance(minutes=5)
    tick4 = await sched.tick()
    assert len(tick4) == 1
    occ4 = tick4[0]
    # Advance past lock_ttl (310s) -> lock expires automatically at 10:25:11 (within 30s grace) allowing occ5!
    clock.advance(minutes=5, seconds=11)
    tick5 = await sched.tick()
    assert len(tick5) == 1
    assert tick5[0].status == OccurrenceStatus.DISPATCHED
    # Stale occ4 token must NOT delete occ5's active lock!
    assert occ4.lock_key is not None and occ4.lock_token is not None
    assert await store.release_overlap_lock(occ4.lock_key, occ4.lock_token) is False

    # Misfire policies: skip vs fire_once across long downtime (jump +1 hour past grace 30s)
    skip_rec = (
        await sched.every(5)
        .minutes()
        .name("misfire-skip")
        .on_misfire(MisfirePolicy.SKIP)
        .run(SampleDigestJob, 10)
    )
    fire_rec = (
        await sched.every(5)
        .minutes()
        .name("misfire-fire-once")
        .on_misfire(MisfirePolicy.FIRE_ONCE)
        .run(SampleDigestJob, 11)
    )
    # Unlock occ5 first so only misfire behavior triggers cleanly after jump
    await store.mark_completed(tick5[0].occurrence_id, now=clock.now)

    clock.advance(hours=1)
    misfire_res = await sched.tick()
    by_sched = {occ.schedule_id: occ for occ in misfire_res}
    assert by_sched[skip_rec.id].status == OccurrenceStatus.SKIPPED
    assert by_sched[fire_rec.id].status == OccurrenceStatus.DISPATCHED
    # Verify catch-up advanced next_run_at strictly beyond now (11:25:01 + 5m)
    post_fire = await sched.get(fire_rec.id)
    assert post_fire is not None and post_fire.next_run_at is not None
    assert post_fire.next_run_at > clock.now


@pytest.mark.asyncio
async def test_pending_dispatch_recovery_and_exhaustion() -> None:
    clock = FakeClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    store = MemoryScheduleStore(clock=clock)
    dispatcher = FakeDispatcher(fail_times=2)
    sched = Scheduler(
        store=store,
        dispatcher=dispatcher,
        clock=clock,
        dispatch_lease_seconds=10.0,
        max_dispatch_attempts=3,
    )

    failed_events: list[ScheduledTaskFailed] = []
    EventDispatcher.get_instance().listen(ScheduledTaskFailed, failed_events.append)

    await sched.every(5).minutes().name("resilient").without_overlapping().run(
        SampleDigestJob, 1
    )

    # T=10:05 -> initial push attempt fails -> occurrence stays DISPATCH_PENDING
    clock.advance(minutes=5)
    t1 = await sched.tick()
    assert len(t1) == 1
    assert t1[0].status == OccurrenceStatus.DISPATCH_PENDING
    assert dispatcher.calls == 1

    # Immediate tick recovers pending attempt 2 -> fails again (still < max 3)
    t2 = await sched.tick()
    assert t2[0].status == OccurrenceStatus.DISPATCH_PENDING
    assert t2[0].dispatch_attempts == 2

    # Third recovery succeeds cleanly and confirms DISPATCHED!
    t3 = await sched.tick()
    assert t3[0].status == OccurrenceStatus.DISPATCHED
    assert t3[0].job_id is not None
    assert failed_events == []

    # Simulate direct crash leaving an unconfirmed DISPATCH_PENDING claim in store,
    # followed by permanent push failures exhausting max_dispatch_attempts and freeing lock.
    dispatcher_down = FakeDispatcher(fail_times=10)
    sched2 = Scheduler(
        store=store,
        dispatcher=dispatcher_down,
        clock=clock,
        dispatch_lease_seconds=10.0,
        max_dispatch_attempts=2,
    )
    # Complete previous lock and trigger next run at 10:10
    await store.mark_completed(t3[0].occurrence_id, now=clock.now)
    clock.advance(minutes=5)
    outcomes = await store.claim_due(
        now=clock.now,
        claimed_by="crashed-proc",
        default_grace_seconds=60.0,
        default_lock_ttl=900,
        dispatch_lease_seconds=10.0,
        next_run_calculator=sched2._compute_next_run,
    )
    assert len(outcomes) == 1
    crashed_occ = outcomes[0].occurrence
    assert crashed_occ.status == OccurrenceStatus.DISPATCH_PENDING

    # Lease expires after 11s -> reclaim attempt 2 fails -> exhausted -> FAILED + overlap lock released!
    clock.advance(seconds=11)
    recovered = await sched2.recover_pending_dispatches()
    assert len(recovered) == 1
    assert recovered[0].status == OccurrenceStatus.FAILED
    assert len(failed_events) == 1
    assert crashed_occ.lock_key is not None and crashed_occ.lock_token is not None
    # Lock was already freed on exhaustion so release returns False
    assert await store.release_overlap_lock(crashed_occ.lock_key, crashed_occ.lock_token) is False


@pytest.mark.asyncio
async def test_schedule_facade_and_container_isolation() -> None:
    c1 = Container()
    c2 = Container()
    clock = FakeClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    s1 = Scheduler(store=MemoryScheduleStore(clock=clock), dispatcher=FakeDispatcher(), clock=clock)
    s2 = Scheduler(store=MemoryScheduleStore(clock=clock), dispatcher=FakeDispatcher(), clock=clock)
    c1.instance(Scheduler, s1)
    c2.instance(Scheduler, s2)

    with bind_container(c1):
        assert scheduler() is s1
        await Schedule.every(1).minutes().name("c1-only").run(SampleDigestJob)
        assert await Schedule.get("c1-only") is not None

    with bind_container(c2):
        assert scheduler() is s2
        assert await Schedule.get("c1-only") is None


class _ListCountingStore(MemoryScheduleStore):
    list_calls = 0

    async def list_schedules(self) -> list[ScheduleRecord]:
        self.list_calls += 1
        return await super().list_schedules()


@pytest.mark.asyncio
async def test_wake_timeout_uses_next_due_without_listing_schedules() -> None:
    clock = FakeClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    store = _ListCountingStore(clock=clock)
    sched = Scheduler(store=store, dispatcher=FakeDispatcher(), clock=clock)

    assert await store.next_due_at() is None
    assert await sched._compute_next_wake_timeout(30.0) == 30.0

    await sched.every(10).seconds().name("soon").run(SampleDigestJob)
    assert await store.next_due_at() == clock.now + timedelta(seconds=10)
    assert await sched._compute_next_wake_timeout(30.0) == pytest.approx(10.0)

    await sched.disable("soon")
    assert await store.next_due_at() is None

    await sched.enable("soon")
    clock.advance(seconds=11)
    assert await sched._compute_next_wake_timeout(5.0) == 0.01
    # Overdue work that no tick could process must back off instead of spinning
    assert await sched._compute_next_wake_timeout(5.0, progressed=False) == 5.0
    assert store.list_calls == 0


_SIGNING_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture
def signing_key() -> Generator[str, None, None]:
    original = Job._app_key
    Job.set_app_key(_SIGNING_KEY)
    try:
        yield _SIGNING_KEY
    finally:
        Job.set_app_key(original)


@pytest.mark.asyncio
async def test_tampered_or_unsigned_schedule_is_never_dispatched(
    signing_key: str,
) -> None:
    clock = FakeClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    store = MemoryScheduleStore(clock=clock)
    dispatcher = FakeDispatcher()
    failed: list[ScheduledTaskFailed] = []
    EventDispatcher.get_instance().listen(
        ScheduledTaskFailed, lambda evt: failed.append(evt)
    )
    sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)

    good = await sched.every(1).minutes().name("good").run(SampleDigestJob, 1)
    tampered = await sched.every(1).minutes().name("tampered").run(SampleDigestJob, 2)
    unsigned = await sched.every(1).minutes().name("unsigned").run(SampleDigestJob, 3)
    assert good.signature is not None

    store._records[tampered.id].args = [999]
    store._records[unsigned.id].signature = None

    clock.advance(minutes=1)
    occurrences = await sched.tick()

    by_schedule = {occ.schedule_id: occ for occ in occurrences}
    assert by_schedule[good.id].status == OccurrenceStatus.DISPATCHED
    assert by_schedule[tampered.id].status == OccurrenceStatus.FAILED
    assert by_schedule[unsigned.id].status == OccurrenceStatus.FAILED
    assert "signature" in (by_schedule[tampered.id].error or "").lower()
    assert [job.kwargs["report_id"] for job, _, _ in dispatcher.pushed] == [1]
    assert len(failed) == 2


@pytest.mark.asyncio
async def test_queue_update_resigns_and_rejects_tampered_record(
    signing_key: str,
) -> None:
    clock = FakeClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    store = MemoryScheduleStore(clock=clock)
    dispatcher = FakeDispatcher()
    sched = Scheduler(store=store, dispatcher=dispatcher, clock=clock)

    rec = await sched.every(1).minutes().name("movable").run(SampleDigestJob, 1)
    old_signature = rec.signature
    moved = await sched.update("movable", queue="priority")
    assert moved.signature is not None
    assert moved.signature != old_signature

    clock.advance(minutes=1)
    (occ,) = await sched.tick()
    assert occ.status == OccurrenceStatus.DISPATCHED
    assert dispatcher.pushed[0][1] == "priority"

    store._records[rec.id].kwargs = {"kind": "evil"}
    with pytest.raises(ScheduleSignatureError):
        await sched.update("movable", queue="other")
