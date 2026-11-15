from __future__ import annotations

import asyncio
import os
import signal
import uuid
from collections.abc import Generator
from typing import TypedDict
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as aioredis

from swiftpy.console.cli import CLI
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job
from swiftpy.queue.worker import QueueWorker
from swiftpy.support.redis_config import resolve_redis_params


class FastJob(Job):
    def __init__(self, key: str, index: int, **kwargs: object) -> None:
        super().__init__(key, index=index, **kwargs)
        self.key = key
        self.index = index

    async def handle(self) -> None:
        pass


class ConcurrentTrackingJob(Job):
    def __init__(self, key: str, index: int, **kwargs: object) -> None:
        super().__init__(key, index=index, **kwargs)
        self.key = key
        self.index = index

    async def handle(self) -> None:
        params = resolve_redis_params(section="queue")
        host = os.getenv("REDIS_HOST", str(params["host"]))
        port = int(os.getenv("REDIS_PORT", str(params["port"])))
        db = int(params["database"])
        c = aioredis.Redis(
            host=host,
            port=port,
            db=db,
            password=os.getenv("REDIS_PASSWORD") or params["password"],
            decode_responses=True,
        )
        try:
            curr = await c.incr(f"{self.key}:active")
            await c.rpush(f"{self.key}:max", str(curr))
            await asyncio.sleep(0.1)
            await c.decr(f"{self.key}:active")
        finally:
            await c.aclose()


@pytest.fixture(autouse=True)
def allow_worker_behavior_jobs() -> Generator[None, None, None]:
    prev_trusted = Job._trusted_classes.copy()
    Job.allow_job_class(FastJob)
    Job.allow_job_class(ConcurrentTrackingJob)
    try:
        yield
    finally:
        Job._trusted_classes = prev_trusted


class _RedisParams(TypedDict):
    host: str
    port: int
    password: str | None
    prefix: str


@pytest.fixture
def redis_params() -> _RedisParams:
    return {
        "host": os.getenv("REDIS_HOST", "127.0.0.1"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "password": os.getenv("REDIS_PASSWORD") or None,
        "prefix": f"test_wb_{uuid.uuid4().hex[:6]}:",
    }


def test_invalid_queue_names_raise_value_error() -> None:
    """Queue names must strictly match ^[A-Za-z0-9_.-]{1,64}$."""
    with pytest.raises(ValueError, match="Invalid queue name"):
        QueueWorker(queues="default:colon")

    with pytest.raises(ValueError, match="Invalid queue name"):
        QueueWorker(queues="bad space")

    with pytest.raises(ValueError, match="Invalid queue name"):
        QueueWorker(queues="a" * 65)

    with pytest.raises(ValueError, match="Invalid queue name"):
        QueueWorker(queues="")


@pytest.mark.asyncio
async def test_worker_concurrency_bound_respected(
    redis_params: _RedisParams,
) -> None:
    """When concurrency=3, no more than 3 jobs execute concurrently."""
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    track_key = f"track_{uuid.uuid4().hex[:6]}"
    dispatcher = QueueDispatcher(**redis_params)

    try:
        # Push 6 jobs
        for i in range(6):
            await dispatcher.push(
                ConcurrentTrackingJob(key=track_key, index=i), queue=q_name
            )

        worker = QueueWorker(
            queues=q_name,
            dispatcher=dispatcher,
            concurrency=3,
            stop_when_empty=True,
            sleep=0.05,
        )
        await worker.work()

        client = await dispatcher.get_client()
        active_counts = [int(v) for v in await client.lrange(f"{track_key}:max", 0, -1)]
        assert len(active_counts) == 6
        assert max(active_counts) <= 3
        assert worker.jobs_processed == 6
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()


@pytest.mark.asyncio
async def test_failed_list_capping(
    redis_params: _RedisParams,
) -> None:
    """Dead letter queue must be capped by failed_max_size."""
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    dispatcher = QueueDispatcher(**redis_params, failed_max_size=3)

    try:
        # Push 6 failures
        for i in range(6):
            await dispatcher.push_failed(
                job=None,
                exception=ValueError(f"Fail {i}"),
                queue=q_name,
                payload={"job_id": f"job-{i}"},
            )

        assert await dispatcher.failed_size(q_name) == 3
        records = await dispatcher.get_failed_jobs(q_name)
        assert len(records) == 3
        # Should keep the newest 3: job-3, job-4, job-5
        assert [r["job_id"] for r in records] == ["job-3", "job-4", "job-5"]
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()


@pytest.mark.asyncio
async def test_retry_failed_and_flush_failed_helpers(
    redis_params: _RedisParams,
) -> None:
    """Verify retry_failed and flush_failed helpers on dispatcher."""
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    dispatcher = QueueDispatcher(**redis_params)

    try:
        job1 = FastJob(key="k1", index=1)
        job2 = FastJob(key="k2", index=2)

        await dispatcher.push_failed(
            job=job1, exception=RuntimeError("err1"), queue=q_name
        )
        await dispatcher.push_failed(
            job=job2, exception=RuntimeError("err2"), queue=q_name
        )
        assert await dispatcher.failed_size(q_name) == 2
        assert await dispatcher.size(q_name) == 0

        # Retry single job by ID
        retried = await dispatcher.retry_failed(q_name, job_id=job1.job_id)
        assert retried == 1
        assert await dispatcher.failed_size(q_name) == 1
        assert await dispatcher.size(q_name) == 1

        # Retry all remaining
        retried_all = await dispatcher.retry_failed(q_name, job_id="all")
        assert retried_all == 1
        assert await dispatcher.failed_size(q_name) == 0
        assert await dispatcher.size(q_name) == 2

        # Flush
        await dispatcher.push_failed(
            job=job1, exception=RuntimeError("err3"), queue=q_name
        )
        assert await dispatcher.failed_size(q_name) == 1
        cleared = await dispatcher.flush_failed(q_name)
        assert cleared == 1
        assert await dispatcher.failed_size(q_name) == 0
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()


@pytest.mark.asyncio
async def test_signal_handlers_restored_on_worker_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker must restore original signal handlers upon exit."""
    orig_sigterm = signal.getsignal(signal.SIGTERM)
    worker = QueueWorker(queues="default", stop_when_empty=True, sleep=0.01)

    # Mock dispatcher methods to avoid connecting to redis
    monkeypatch.setattr(
        worker.dispatcher, "reap_expired_leases", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        worker.dispatcher, "pop_and_lease", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(worker.dispatcher, "size", AsyncMock(return_value=0))
    monkeypatch.setattr(worker.dispatcher, "delayed_size", AsyncMock(return_value=0))
    monkeypatch.setattr(worker.dispatcher, "processing_size", AsyncMock(return_value=0))
    monkeypatch.setattr(worker.dispatcher, "close", AsyncMock())
    await worker.work()

    curr_sigterm = signal.getsignal(signal.SIGTERM)
    assert curr_sigterm == orig_sigterm


@pytest.mark.asyncio
async def test_queue_cli_commands_and_app_failure(
    redis_params: _RedisParams,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify queue:failed, queue:retry, and queue:flush CLI commands and --app failure."""
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    prefix = str(redis_params["prefix"])

    # 1. Invalid app target raises SystemExit
    cli = CLI()
    with pytest.raises(SystemExit):
        cli.run(["queue:work", "--app", "nonexistent_app_module:app", "--prefix", prefix])

    # 2. Add failed job directly
    dispatcher = QueueDispatcher(**redis_params)
    try:
        await dispatcher.push_failed(
            job=None,
            exception=RuntimeError("CLI failure test"),
            queue=q_name,
            payload={"job_id": "test-cli-id"},
        )

        # 3. CLI queue:failed
        cli.run(["queue:failed", "--queue", q_name, "--prefix", prefix])
        out = capsys.readouterr().out
        assert "test-cli-id" in out
        assert "CLI failure test" in out

        # 4. CLI queue:retry
        cli.run(["queue:retry", "test-cli-id", "--queue", q_name, "--prefix", prefix])
        out2 = capsys.readouterr().out
        assert "Retried 1 failed job(s)" in out2

        # 5. CLI queue:flush
        await dispatcher.push_failed(
            job=None,
            exception=RuntimeError("flush me"),
            queue=q_name,
            payload={"job_id": "flush-id"},
        )
        cli.run(["queue:flush", "--queue", q_name, "--prefix", prefix])
        out3 = capsys.readouterr().out
        assert "Flushed 1 failed job(s)" in out3
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()

