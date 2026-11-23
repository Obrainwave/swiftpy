from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import redis.asyncio as aioredis

from swiftpy.console.cli import CLI
from swiftpy.core.application import Application
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container, set_container
from swiftpy.core.context import Context
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job
from swiftpy.scheduling import (
    MemoryScheduleStore,
    OccurrenceStatus,
    RedisScheduleStore,
    Scheduler,
)


class LifecycleSampleJob(Job):
    queue = "sched_cli_q"
    tries = 2
    backoff = 1
    timeout = 10.0

    def __init__(self, marker: str = "ok") -> None:
        super().__init__(marker=marker)
        self.marker = marker

    async def handle(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_lifecycle_globals() -> Generator[None, None, None]:
    original_trusted = Job._trusted_classes.copy()
    Job.allow_job_class(LifecycleSampleJob)
    Scheduler.set_instance(None)
    QueueDispatcher.set_instance(None)
    EventDispatcher.set_instance(EventDispatcher())
    Context.clear()
    try:
        yield
    finally:
        Job._trusted_classes = original_trusted
        Scheduler.set_instance(None)
        QueueDispatcher.set_instance(None)
        EventDispatcher.set_instance(None)
        Context.clear()
        set_container(Container())


@pytest.fixture
async def redis_cli_env() -> AsyncGenerator[
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
    sched_prefix = f"swiftpy_sched_p7:{run_id}:"
    queue_prefix = f"swiftpy_queue_p7:{run_id}:"
    try:
        await client.ping()
        yield client, sched_prefix, queue_prefix
    finally:
        for pattern in (f"{sched_prefix}*", f"{queue_prefix}*"):
            keys: list[str] = [key async for key in client.scan_iter(match=pattern)]
            if keys:
                await client.delete(*keys)
        await client.aclose()


def test_schedule_config_file_defaults_and_eager_app_validation() -> None:
    cfg = Config.load("config")
    assert cfg.get("schedule.driver") == "redis"
    assert cfg.get("schedule.timezone") == "UTC"
    assert cfg.get("schedule.auto_start") is False
    assert cfg.get("schedule.poll_interval") == 1.0
    assert cfg.get("schedule.grace_seconds") == 60.0
    assert cfg.get("schedule.dispatch_lease_seconds") == 30.0
    assert cfg.get("schedule.max_dispatch_attempts") == 5
    assert cfg.get("schedule.default_lock_ttl") == 900
    assert cfg.get("schedule.prefix") == "swiftpy_schedule:"

    # Invalid schedule.* config fails fast during Application.__init__
    with pytest.raises(ConfigurationError, match="schedule.driver"):
        Application(
            config={"schedule": {"driver": "invalid-driver"}},
            auto_discover_providers=False,
        )

    with pytest.raises(ConfigurationError):
        Application(
            config={"schedule": {"timezone": "Mars/Olympus_Mons"}},
            auto_discover_providers=False,
        )

    with pytest.raises(ConfigurationError, match="schedule.poll_interval"):
        Application(
            config={"schedule": {"poll_interval": 0}},
            auto_discover_providers=False,
        )


@pytest.mark.asyncio
async def test_application_lifespan_auto_start_idempotency_non_blocking_and_zero_leaks() -> None:
    # 1. Default auto_start=False does NOT spawn any background loop
    app_manual = Application(
        config={"schedule": {"driver": "memory"}},
        auto_discover_providers=False,
    )
    await app_manual.startup()
    try:
        sched_manual = app_manual.container.resolve(Scheduler)
        assert isinstance(sched_manual.store, MemoryScheduleStore)
        assert sched_manual.is_running is False
    finally:
        await app_manual.shutdown()

    # 2. Opt-in auto_start=True starts single idempotent loop, never blocks event loop,
    # and cancels with zero leaked tasks on shutdown
    before_tasks = set(asyncio.all_tasks())
    app_auto = Application(
        config={
            "schedule": {
                "driver": "memory",
                "auto_start": True,
                "poll_interval": 0.25,
                "timezone": "UTC",
            }
        },
        auto_discover_providers=False,
    )
    await app_auto.startup()
    sched = app_auto.container.resolve(Scheduler)
    assert sched.is_running is True

    # Calling start() again must return the exact same Task idempotently
    first_task = sched._loop_task
    assert first_task is not None
    assert sched.start() is first_task

    # Non-blocking event loop check while scheduler sleeps between ticks
    t0 = time.monotonic()

    async def fast_probe() -> int:
        await asyncio.sleep(0.01)
        return 42

    assert await fast_probe() == 42
    assert (time.monotonic() - t0) < 0.2

    await app_auto.shutdown()
    assert sched.is_running is False
    assert sched._loop_task is None

    # Allow event loop one tick to finalize cancelled callbacks
    await asyncio.sleep(0)
    after_tasks = set(asyncio.all_tasks())
    assert after_tasks <= before_tasks

    # 3. Startup failure rollback stops already-started scheduler loop cleanly
    class FailingProvider(ServiceProvider):
        def register(self) -> None:
            return None

        async def startup(self) -> None:
            raise RuntimeError("Downstream startup crash")

    app_rollback = Application(
        config={
            "schedule": {
                "driver": "memory",
                "auto_start": True,
                "poll_interval": 0.2,
            }
        },
        providers=[FailingProvider],
        auto_discover_providers=False,
    )
    with pytest.raises(RuntimeError, match="Downstream startup crash"):
        await app_rollback.startup()

    rolled_back_sched = app_rollback.container.resolve(Scheduler)
    assert rolled_back_sched.is_running is False


@pytest.mark.asyncio
async def test_scheduler_work_signal_handlers_restored_and_max_ticks() -> None:
    orig_sigterm = signal.getsignal(signal.SIGTERM)
    sched = Scheduler(
        store=MemoryScheduleStore(),
        poll_interval=0.05,
    )
    ticks = await sched.work(sleep=0.02, max_ticks=2)
    assert ticks == 2
    assert await sched.work(max_ticks=0) == 0

    # Verify SIGTERM wakes sleeping work() immediately and restores original handler
    work_task = asyncio.create_task(sched.work(sleep=5.0))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    os.kill(os.getpid(), signal.SIGTERM)
    stopped_ticks = await asyncio.wait_for(work_task, timeout=2.0)
    assert stopped_ticks >= 1
    assert (time.monotonic() - t0) < 1.0
    assert signal.getsignal(signal.SIGTERM) == orig_sigterm
    await sched.close()


@pytest.mark.asyncio
async def test_cli_schedule_list_run_and_work_with_real_redis_and_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    redis_cli_env: tuple[aioredis.Redis, str, str],
) -> None:
    client, sched_prefix, queue_prefix = redis_cli_env
    cli = CLI()

    # 1. Invalid CLI options fail cleanly with SystemExit
    for bad_args in (
        ["schedule:run", "--app", "nonexistent_mod:app"],
        ["schedule:run", "--limit", "0", "--prefix", sched_prefix],
        ["schedule:work", "--sleep", "0", "--prefix", sched_prefix],
        ["schedule:work", "--max-ticks", "-1", "--prefix", sched_prefix],
    ):
        with pytest.raises(SystemExit):
            cli.run(bad_args)

    # 2. Create temporary SwiftPY application registering a due & future schedule on boot
    mod_name = f"tmp_sched_app_{uuid.uuid4().hex[:8]}"
    app_file = tmp_path / f"{mod_name}.py"
    app_file.write_text(
        f"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from swiftpy.core.application import Application
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.queue.job import Job
from swiftpy.scheduling import Scheduler


class CliDemoJob(Job):
    queue = "sched_cli_q"

    def __init__(self, label: str = "cli") -> None:
        super().__init__(label=label)
        self.label = label

    async def handle(self) -> None:
        return None


Job.allow_job_class(CliDemoJob)


class DemoScheduleProvider(ServiceProvider):
    def register(self) -> None:
        return None

    async def startup(self) -> None:
        sched = self.container.resolve(Scheduler)
        now = datetime.now(UTC)
        await sched.schedule(
            CliDemoJob,
            at=now - timedelta(seconds=1),
            name="due-now-cli",
            allow_past=True,
            upsert=True,
            kwargs={{"label": "fired-by-cli"}},
        )
        await sched.cron("0 * * * *").name("future-cron-cli").upsert().run(
            CliDemoJob, "future"
        )


app = Application(
    providers=[DemoScheduleProvider],
    auto_discover_providers=False,
    queue={{
        "connections": {{
            "redis": {{
                "prefix": {queue_prefix!r},
            }},
        }},
    }},
    schedule={{
        "driver": "redis",
        "prefix": {sched_prefix!r},
        "auto_start": False,
    }},
)
""",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        # 3. `swiftpy schedule:list --app ... --json`
        cli.run(["schedule:list", "--app", f"{mod_name}:app", "--json"])
        list_json = json.loads(capsys.readouterr().out)
        assert len(list_json) == 2
        assert {item["name"] for item in list_json} == {
            "due-now-cli",
            "future-cron-cli",
        }

        # 4. `swiftpy schedule:list --prefix ...` (standalone table view)
        cli.run(["schedule:list", "--prefix", sched_prefix])
        table_out = capsys.readouterr().out
        assert "[due-now-cli]" in table_out
        assert "[future-cron-cli]" in table_out

        # 5. `swiftpy schedule:run --app ...` dispatches the due schedule onto Redis queue!
        cli.run(["schedule:run", "--app", f"{mod_name}:app"])
        run_out = capsys.readouterr().out
        assert "dispatched=1" in run_out
        assert await client.llen(f"{queue_prefix}sched_cli_q") == 1

        # Verify persisted schedule status in Redis
        verify_store = RedisScheduleStore(redis_client=client, prefix=sched_prefix)
        due_rec = await verify_store.get_schedule("due-now-cli")
        assert due_rec is not None
        assert due_rec.enabled is False
        assert due_rec.last_status == OccurrenceStatus.DISPATCHED
        await verify_store.close()

        # 6. `swiftpy schedule:work --app ... --sleep 0.02 --max-ticks 2`
        cli.run(
            ["schedule:work", "--app", f"{mod_name}:app", "--sleep", "0.02", "--max-ticks", "2"]
        )
        work_out = capsys.readouterr().out
        assert "stopped after 2 tick(s)" in work_out

        # 7. Empty standalone `--prefix` list prints clean message
        empty_prefix = f"{sched_prefix}empty:"
        cli.run(["schedule:list", "--prefix", empty_prefix])
        assert "No scheduled tasks registered." in capsys.readouterr().out
    finally:
        sys.modules.pop(mod_name, None)
        importlib.invalidate_caches()
