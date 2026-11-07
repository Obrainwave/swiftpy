from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
import redis.asyncio as aioredis

from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job
from swiftpy.queue.worker import QueueWorker


class RecordingJob(Job):
    """Job that records its execution in a Redis key."""

    def __init__(self, key: str, value: str = "ok", **kwargs: object) -> None:
        super().__init__(key, value=value, **kwargs)
        self.key = key
        self.value = value

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = aioredis.Redis(host=host, port=port, decode_responses=True)
        try:
            await client.rpush(self.key, str(self.value))
        finally:
            await client.aclose()


class BlockingJob(Job):
    def __init__(self, started_key: str, **kwargs: object) -> None:
        super().__init__(started_key, **kwargs)
        self.started_key = started_key

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        c = aioredis.Redis(host=host, port=port, decode_responses=True)
        try:
            await c.set(self.started_key, "started")
            await asyncio.sleep(10.0)  # Long running task
        finally:
            await c.aclose()


@pytest.fixture
def redis_params() -> dict[str, object]:
    return {
        "host": os.getenv("REDIS_HOST", "127.0.0.1"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "prefix": f"test_q_{uuid.uuid4().hex[:6]}:",
    }


@pytest.mark.asyncio
async def test_successful_job_is_acked_and_removed_from_processing_and_leases(
    redis_params: dict[str, object],
) -> None:
    """Verifies that an executed job is acked and removed from processing and lease sets."""
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    res_key = f"res_{uuid.uuid4().hex[:6]}"
    dispatcher = QueueDispatcher(**redis_params)  # type: ignore[arg-type]

    try:
        job = RecordingJob(key=res_key, value="done")
        await dispatcher.push(job, queue=q_name)

        worker = QueueWorker(
            queues=q_name,
            dispatcher=dispatcher,
            sleep=0.05,
            stop_when_empty=True,
            worker_id="test-worker-ack",
        )
        await worker.work()

        # Job executed
        client = await dispatcher.get_client()
        items = await client.lrange(res_key, 0, -1)
        assert items == ["done"]

        # Queue, processing list, and leases must all be empty
        assert await dispatcher.size(q_name) == 0
        assert await dispatcher.processing_size(q_name, "test-worker-ack") == 0
        assert await dispatcher.leases_size(q_name) == 0
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()


@pytest.mark.asyncio
async def test_kill_after_pop_redelivers_expired_lease_via_reaper(
    redis_params: dict[str, object],
) -> None:
    """Simulates a worker dying after pop without acking; reaper redelivers expired job."""
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    dispatcher = QueueDispatcher(**redis_params)  # type: ignore[arg-type]

    try:
        job = RecordingJob(key="dummy_key", value="survived", tries=3)
        await dispatcher.push(job, queue=q_name)

        # Worker pops and leases the job with a very short lease (0.1s)
        raw_payload = await dispatcher.pop_and_lease(
            queue=q_name,
            worker_id="crashed-worker",
            lease_duration=0.1,
        )
        assert raw_payload is not None
        assert "RecordingJob" in raw_payload

        # In-flight state: ready queue is 0, processing list is 1, lease count is 1
        assert await dispatcher.size(q_name) == 0
        assert await dispatcher.processing_size(q_name, "crashed-worker") == 1
        assert await dispatcher.leases_size(q_name) == 1

        # Wait for lease to expire
        await asyncio.sleep(0.2)

        # Reaper runs and reclaims the expired job
        reaped_count = await dispatcher.reap_expired_leases(q_name)
        assert reaped_count == 1

        # Processing list and leases for crashed worker are cleared, job is back in ready queue
        assert await dispatcher.processing_size(q_name, "crashed-worker") == 0
        assert await dispatcher.leases_size(q_name) == 0
        assert await dispatcher.size(q_name) == 1

        # Check that attempts was incremented
        client = await dispatcher.get_client()
        requeued_raw = await client.lindex(dispatcher._queue_key(q_name), 0)
        assert requeued_raw is not None
        requeued_data = json.loads(requeued_raw)
        assert requeued_data.get("attempts") == 1
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()


@pytest.mark.asyncio
async def test_reaper_moves_expired_job_to_dlq_when_tries_exhausted(
    redis_params: dict[str, object],
) -> None:
    """If a job's lease expires and attempts >= tries, reaper moves it directly to DLQ."""
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    dispatcher = QueueDispatcher(**redis_params)  # type: ignore[arg-type]

    try:
        # Job allows only 1 try
        job = RecordingJob(key="dummy_key", value="fail", tries=1)
        await dispatcher.push(job, queue=q_name)

        # Worker pops with short lease
        await dispatcher.pop_and_lease(
            queue=q_name,
            worker_id="crashed-worker-2",
            lease_duration=0.1,
        )
        await asyncio.sleep(0.2)

        # Reaper runs
        reaped = await dispatcher.reap_expired_leases(q_name)
        assert reaped == 1

        # Ready queue is empty, but DLQ has 1 failed job record
        assert await dispatcher.size(q_name) == 0
        assert await dispatcher.failed_size(q_name) == 1

        failed_records = await dispatcher.get_failed_jobs(q_name)
        assert len(failed_records) == 1
        assert failed_records[0]["exception"]["type"] == "LeaseExpiredError"
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()


@pytest.mark.asyncio
async def test_cancellation_requeues_job_at_head_preserving_attempts(
    redis_params: dict[str, object],
) -> None:
    """A worker task cancelled mid-execution releases the job back to the head of the queue."""
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    dispatcher = QueueDispatcher(**redis_params)  # type: ignore[arg-type]

    try:
        started_key = f"started_{uuid.uuid4().hex[:6]}"
        job = BlockingJob(started_key=started_key, tries=3)
        await dispatcher.push(job, queue=q_name)

        worker = QueueWorker(
            queues=q_name,
            dispatcher=dispatcher,
            sleep=0.05,
            worker_id="cancelling-worker",
        )

        task = asyncio.create_task(worker.work())

        # Wait until job starts executing
        client = await dispatcher.get_client()
        for _ in range(50):
            if await client.get(started_key) == "started":
                break
            await asyncio.sleep(0.05)

        # Cancel worker task mid-execution
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Job must be returned to queue with attempts preserved and processing cleared
        assert await dispatcher.size(q_name) == 1
        assert await dispatcher.processing_size(q_name, "cancelling-worker") == 0
        assert await dispatcher.leases_size(q_name) == 0

        raw_head = await client.lindex(dispatcher._queue_key(q_name), 0)
        assert raw_head is not None
        head_data = json.loads(raw_head)
        assert head_data.get("attempts") == 1
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()
