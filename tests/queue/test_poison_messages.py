from __future__ import annotations

import json
import os
import uuid

import pytest
import redis.asyncio as aioredis

from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job, JobSerializationError
from swiftpy.queue.worker import QueueWorker


class GoodJob(Job):
    """A valid job that records success in Redis."""

    def __init__(self, key: str, value: str = "success", **kwargs: object) -> None:
        super().__init__(key, value=value, **kwargs)
        self.key = key
        self.value = value

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = aioredis.Redis(host=host, port=port, decode_responses=True)
        try:
            await client.rpush(self.key, self.value)
        finally:
            await client.aclose()


@pytest.fixture
def redis_params() -> dict[str, object]:
    return {
        "host": os.getenv("REDIS_HOST", "127.0.0.1"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "prefix": f"test_poison_{uuid.uuid4().hex[:6]}:",
    }


def test_job_deserialize_validates_envelope_types_and_raises_serialization_error() -> None:
    """Job.deserialize must raise JobSerializationError for malformed envelope fields."""
    qualname = f"{GoodJob.__module__}:{GoodJob.__qualname__}"

    # 1. kwargs is not a dict (e.g. kwargs=5)
    with pytest.raises(JobSerializationError, match="kwargs"):
        Job.deserialize({"job_class": qualname, "kwargs": 5})

    # 2. tries is not an int (e.g. tries="abc")
    with pytest.raises(JobSerializationError, match="tries"):
        Job.deserialize({"job_class": qualname, "tries": "abc"})

    # 3. attempts is not an int (e.g. attempts={})
    with pytest.raises(JobSerializationError, match="attempts"):
        Job.deserialize({"job_class": qualname, "attempts": {}})

    # 4. args is not a list/tuple
    with pytest.raises(JobSerializationError, match="args"):
        Job.deserialize({"job_class": qualname, "args": 12345})

    # 5. data is not a dict
    with pytest.raises(JobSerializationError, match="data"):
        Job.deserialize({"job_class": qualname, "data": "not_a_dict"})


@pytest.mark.asyncio
async def test_worker_survives_poison_messages_and_subsequent_good_job_runs(
    redis_params: dict[str, object],
) -> None:
    """
    Worker processes poison payloads (kwargs=5, tries="abc", attempts={}, truncated JSON,
    non-UTF-8 bytes, huge payload), survives, pushes them to DLQ, and executes subsequent good job.
    """
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    res_key = f"res_{uuid.uuid4().hex[:6]}"
    dispatcher = QueueDispatcher(**redis_params)  # type: ignore[arg-type]

    try:
        qualname = f"{GoodJob.__module__}:{GoodJob.__qualname__}"

        poison_payloads: list[str | bytes] = [
            # 1. kwargs = 5
            json.dumps({"job_class": qualname, "kwargs": 5}),
            # 2. tries = "abc"
            json.dumps({"job_class": qualname, "tries": "abc"}),
            # 3. attempts = {}
            json.dumps({"job_class": qualname, "attempts": {}}),
            # 4. Truncated JSON
            '{"job_class": "' + qualname + '", "args": [',
            # 5. Non-UTF-8 bytes
            b'{"job_class": "' + qualname.encode("utf-8") + b'", "invalid": \xff\xfe\xfd}',
            # 6. Huge malformed payload (1 MB of random garbage)
            "A" * 1_000_000,
        ]

        client = await dispatcher.get_client()
        for p in poison_payloads:
            await client.rpush(dispatcher._queue_key(q_name), p)

        # Enqueue a valid GoodJob after the poison messages
        good_job = GoodJob(key=res_key, value="completed_after_poison")
        await dispatcher.push(good_job, queue=q_name)

        # Total jobs in queue = 6 poison + 1 good = 7
        assert await dispatcher.size(q_name) == 7

        worker = QueueWorker(
            queues=q_name,
            dispatcher=dispatcher,
            sleep=0.02,
            stop_when_empty=True,
            worker_id="poison-resilient-worker",
        )
        await worker.work()

        # Worker must have processed the good job
        results = await client.lrange(res_key, 0, -1)
        assert results == ["completed_after_poison"]
        assert worker.jobs_processed == 1
        assert worker.jobs_failed == 6

        # DLQ must contain the 6 failed records
        assert await dispatcher.failed_size(q_name) == 6
        failed_records = await dispatcher.get_failed_jobs(q_name)
        assert len(failed_records) == 6
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()
