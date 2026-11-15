import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
import redis.asyncio as aioredis

from swiftpy.queue.dispatcher import QueueDispatcher, push
from swiftpy.queue.job import Job, JobSerializationError
from swiftpy.queue.worker import QueueWorker


class SimpleTestJob(Job):
    """Simple job recording message into a Redis key."""

    def __init__(self, key: str, message: str, **kwargs: object) -> None:
        super().__init__(key, message, **kwargs)
        self.key = key
        self.message = message

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = aioredis.Redis(
            host=host,
            port=port,
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
        )
        try:
            await client.set(self.key, self.message)
        finally:
            await client.aclose()


class StressCounterJob(Job):
    """High-concurrency stress job recording index and worker PID into Redis set and list."""

    def __init__(self, key: str, index: int, **kwargs: object) -> None:
        super().__init__(key, index, **kwargs)
        self.key = key
        self.index = index

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = aioredis.Redis(
            host=host,
            port=port,
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
        )
        try:
            pipe = client.pipeline()
            pipe.rpush(f"{self.key}:list", str(self.index))
            pipe.sadd(f"{self.key}:set", str(self.index))
            pipe.sadd(f"{self.key}:pids", str(os.getpid()))
            await pipe.execute()
        finally:
            await client.aclose()


class FlakyJob(Job):
    """Job that fails fail_times before succeeding."""

    def __init__(
        self,
        key: str,
        fail_times: int = 2,
        tries: int = 3,
        backoff: float = 0.05,
        **kwargs: object,
    ) -> None:
        super().__init__(
            key,
            fail_times=fail_times,
            tries=tries,
            backoff=backoff,
            **kwargs,
        )
        self.key = key
        self.fail_times = fail_times

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = aioredis.Redis(
            host=host,
            port=port,
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
        )
        try:
            curr = await client.incr(f"{self.key}:attempts")
            if curr <= self.fail_times:
                raise ValueError(f"Flaky failure on attempt {curr}")
            await client.set(f"{self.key}:succeeded", str(curr))
        finally:
            await client.aclose()


class FailingJob(Job):
    """Job that always fails and triggers the failed() hook."""

    def __init__(
        self,
        key: str,
        reason: str = "Permanent error",
        tries: int = 2,
        backoff: float = 0.05,
        **kwargs: object,
    ) -> None:
        super().__init__(key, reason=reason, tries=tries, backoff=backoff, **kwargs)
        self.key = key
        self.reason = reason

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = aioredis.Redis(
            host=host,
            port=port,
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
        )
        try:
            await client.incr(f"{self.key}:attempts")
        finally:
            await client.aclose()
        raise RuntimeError(self.reason)

    async def failed(self, exception: BaseException) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = aioredis.Redis(
            host=host,
            port=port,
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
        )
        try:
            await client.set(
                f"{self.key}:failed_hook",
                f"{type(exception).__name__}: {exception}",
            )
        finally:
            await client.aclose()


class TimeoutJob(Job):
    """Job that sleeps longer than its timeout."""

    def __init__(
        self,
        sleep_duration: float = 1.0,
        timeout: float = 0.1,
        tries: int = 1,
        **kwargs: object,
    ) -> None:
        super().__init__(
            sleep_duration=sleep_duration,
            timeout=timeout,
            tries=tries,
            **kwargs,
        )
        self.sleep_duration = sleep_duration

    async def handle(self) -> None:
        await asyncio.sleep(self.sleep_duration)


class FailingHookJob(Job):
    """Job that fails and raises an exception inside its failed() hook."""

    def __init__(self, key: str, **kwargs: object) -> None:
        super().__init__(key, tries=1, **kwargs)
        self.key = key

    async def handle(self) -> None:
        raise RuntimeError("Primary job failure")

    async def failed(self, exception: BaseException) -> None:
        raise RuntimeError("Failed hook error")


class TimestampedFlakyJob(Job):
    """Job that records monotonic timestamps of each execution attempt."""

    def __init__(
        self,
        key: str,
        backoff: list[float] | float = 0.05,
        **kwargs: object,
    ) -> None:
        super().__init__(key, backoff=backoff, tries=3, **kwargs)
        self.key = key

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = aioredis.Redis(
            host=host,
            port=port,
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
        )
        try:
            curr = await client.incr(f"{self.key}:attempts")
            await client.rpush(f"{self.key}:timestamps", str(time.monotonic()))
            if curr < 3:
                raise ValueError(f"Failing on attempt {curr}")
            await client.set(f"{self.key}:success", "1")
        finally:
            await client.aclose()


class ZeroBackoffJob(Job):
    """Job with 0 backoff that retries immediately in active queue."""

    def __init__(self, key: str, **kwargs: object) -> None:
        super().__init__(key, backoff=0, tries=2, **kwargs)
        self.key = key

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = aioredis.Redis(
            host=host,
            port=port,
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
        )
        try:
            curr = await client.incr(f"{self.key}:attempts")
            if curr < 2:
                raise ValueError("First attempt fails")
            await client.set(f"{self.key}:success", "1")
        finally:
            await client.aclose()


class CancellableJob(Job):
    """Job that signals start and then sleeps waiting for cancellation."""

    def __init__(self, key: str, **kwargs: object) -> None:
        super().__init__(key, **kwargs)
        self.key = key

    async def handle(self) -> None:
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = aioredis.Redis(
            host=host,
            port=port,
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
        )
        try:
            await client.set(f"{self.key}:started", "1")
            await asyncio.sleep(10.0)
            await client.set(f"{self.key}:completed", "1")
        finally:
            await client.aclose()


@pytest.fixture(autouse=True)
def allow_test_queue_jobs() -> Generator[None, None, None]:
    prev_trusted = Job._trusted_classes.copy()
    for job_cls in (
        SimpleTestJob,
        StressCounterJob,
        FlakyJob,
        FailingJob,
        TimeoutJob,
        FailingHookJob,
        TimestampedFlakyJob,
        ZeroBackoffJob,
        CancellableJob,
    ):
        Job.allow_job_class(job_cls)
    try:
        yield
    finally:
        Job._trusted_classes = prev_trusted


def _write_worker_boot_module(tmp_path: Path) -> Path:
    boot_file = tmp_path / "worker_boot.py"
    boot_file.write_text(
        "from swiftpy.core.application import Application\n"
        "from swiftpy.queue.job import Job\n"
        "from tests.queue.test_queue import SimpleTestJob, StressCounterJob\n"
        "\n"
        "Job.allow_job_class(SimpleTestJob)\n"
        "Job.allow_job_class(StressCounterJob)\n"
        "app = Application(auto_discover_providers=False)\n",
        encoding="utf-8",
    )
    return boot_file


@pytest.fixture
def redis_host() -> str:
    return os.getenv("REDIS_HOST", "127.0.0.1")


@pytest.fixture
def redis_port() -> int:
    return int(os.getenv("REDIS_PORT", "6379"))


def test_job_serialization_and_deserialization() -> None:
    """Test standard job serialization and round-trip deserialization."""
    job = SimpleTestJob("test_key", "hello world", queue="emails", tries=5, backoff=2.5)
    serialized = job.serialize()

    assert serialized["job_class"] in (
        "test_queue:SimpleTestJob",
        "tests.queue.test_queue:SimpleTestJob",
    )
    assert serialized["queue"] == "emails"
    assert serialized["tries"] == 5
    assert serialized["backoff"] == 2.5
    assert serialized["args"] == ["test_key", "hello world"]
    assert serialized["job_id"] == job.job_id

    # Round trip
    reconstructed = Job.deserialize(serialized)
    assert isinstance(reconstructed, SimpleTestJob)
    assert reconstructed.key == "test_key"
    assert reconstructed.message == "hello world"
    assert reconstructed.queue == "emails"
    assert reconstructed.tries == 5
    assert reconstructed.backoff == 2.5
    assert reconstructed.job_id == job.job_id


def test_job_deserialization_errors() -> None:
    """Test deserialization error handling on malformed or non-job envelopes."""
    with pytest.raises(JobSerializationError, match="Failed to parse JSON"):
        Job.deserialize("invalid-json{", allow_unsigned=True)

    with pytest.raises(JobSerializationError, match="Invalid job envelope"):
        Job.deserialize(json.loads('["not-a-dict"]'), allow_unsigned=True)

    with pytest.raises(JobSerializationError, match="missing valid 'job_class'"):
        Job.deserialize({"job_id": "123"}, allow_unsigned=True)

    with pytest.raises(JobSerializationError, match="Invalid job class identifier"):
        Job.deserialize({"job_class": "NoDelimiters"}, allow_unsigned=True)

    with pytest.raises(JobSerializationError, match="not in trusted namespaces"):
        Job.deserialize({"job_class": "non_existent_module:SomeJob"}, allow_unsigned=True)

    prev_ns = Job._trusted_namespaces
    try:
        Job.set_trusted_namespaces(["app", "tests"])
        with pytest.raises(JobSerializationError, match="Could not import module"):
            Job.deserialize(
                {"job_class": "tests.non_existent_module:SomeJob"},
                allow_unsigned=True,
            )

        with pytest.raises(JobSerializationError, match="not found in module"):
            Job.deserialize(
                {"job_class": f"{__name__}:NonExistentJob"},
                allow_unsigned=True,
            )

        with pytest.raises(JobSerializationError, match="not a subclass of Job"):
            Job.deserialize(
                {"job_class": f"{__name__}:test_job_deserialization_errors"},
                allow_unsigned=True,
            )
    finally:
        Job.set_trusted_namespaces(prev_ns)


def test_backoff_delay_calculation() -> None:
    """Test scalar and sequential list backoff delay strategies."""
    # Scalar backoff
    job1 = SimpleTestJob("k", "m", backoff=3.5)
    job1.attempts = 1
    assert job1.get_backoff_delay() == 3.5
    job1.attempts = 5
    assert job1.get_backoff_delay() == 3.5

    # List backoff
    job2 = SimpleTestJob("k", "m", backoff=[1.0, 5.0, 15.0])
    job2.attempts = 1
    assert job2.get_backoff_delay() == 1.0
    job2.attempts = 2
    assert job2.get_backoff_delay() == 5.0
    job2.attempts = 3
    assert job2.get_backoff_delay() == 15.0
    job2.attempts = 10  # Clamps to last element
    assert job2.get_backoff_delay() == 15.0

    # Empty list backoff
    job3 = SimpleTestJob("k", "m", backoff=[])
    assert job3.get_backoff_delay() == 0.0


async def test_dispatcher_push_and_inspection(redis_host: str, redis_port: int) -> None:
    """Test pushing immediate, delayed, and failed jobs and inspecting queue metrics."""
    prefix = f"test_disp_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(
        host=redis_host,
        port=redis_port,
        prefix=prefix,
    )

    try:
        queue_name = "test_queue"
        job1 = SimpleTestJob("k1", "m1")
        job2 = SimpleTestJob("k2", "m2")

        # Push immediate
        job_id = await dispatcher.push(job1, queue=queue_name)
        assert job_id == job1.job_id
        assert await dispatcher.size(queue_name) == 1
        assert await dispatcher.delayed_size(queue_name) == 0

        # Push delayed via global push() helper
        QueueDispatcher.set_instance(dispatcher)
        await push(job2, queue=queue_name, delay=10.0)
        assert await dispatcher.size(queue_name) == 1
        assert await dispatcher.delayed_size(queue_name) == 1

        # Push failed
        await dispatcher.push_failed(
            job=job1,
            exception=ValueError("Manual failure"),
            queue=queue_name,
        )
        assert await dispatcher.failed_size(queue_name) == 1
        failed_jobs = await dispatcher.get_failed_jobs(queue=queue_name)
        assert len(failed_jobs) == 1
        assert failed_jobs[0]["job_id"] == job1.job_id
        assert failed_jobs[0]["exception"]["type"] == "ValueError"

        # Clear
        await dispatcher.clear(queue_name)
        assert await dispatcher.size(queue_name) == 0
        assert await dispatcher.delayed_size(queue_name) == 0
        assert await dispatcher.failed_size(queue_name) == 0
    finally:
        await dispatcher.close()


async def test_worker_handles_job_successfully(
    redis_host: str, redis_port: int
) -> None:
    """Test that a worker pulls and successfully executes an immediate job."""
    prefix = f"test_worker_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    queue_name = "emails"

    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    result_key = f"result_{uuid.uuid4().hex[:8]}"

    try:
        job = SimpleTestJob(result_key, "welcome_user_42", queue=queue_name)
        await dispatcher.push(job)

        worker = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            stop_when_empty=True,
            sleep=0.01,
        )
        await worker.work()

        assert worker.jobs_processed == 1
        assert worker.jobs_failed == 0

        # Verify Redis value set by job
        val = await client.get(result_key)
        assert val == "welcome_user_42"

        # Verify queue is empty
        assert await dispatcher.size(queue_name) == 0
    finally:
        await client.delete(result_key)
        await client.aclose()
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_worker_retry_with_backoff(redis_host: str, redis_port: int) -> None:
    """Test retry with backoff: job fails 2 times and succeeds on attempt 3."""
    prefix = f"test_retry_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    queue_name = "retry_queue"

    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    key = f"flaky_{uuid.uuid4().hex[:8]}"

    try:
        # Fails on attempts 1 and 2, succeeds on attempt 3
        job = FlakyJob(key=key, fail_times=2, tries=3, backoff=0.05, queue=queue_name)
        await dispatcher.push(job)

        worker = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            stop_when_empty=True,
            sleep=0.02,
        )
        await worker.work()

        # Job was attempted 3 times and succeeded on 3rd attempt
        attempts = await client.get(f"{key}:attempts")
        assert attempts == "3"

        succeeded = await client.get(f"{key}:succeeded")
        assert succeeded == "3"

        assert worker.jobs_processed == 1
        assert worker.jobs_failed == 0

        # DLQ should be empty
        assert await dispatcher.failed_size(queue_name) == 0
    finally:
        await client.delete(f"{key}:attempts", f"{key}:succeeded")
        await client.aclose()
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_worker_dead_letter_queue_on_exhaustion(
    redis_host: str, redis_port: int
) -> None:
    """Test that exhausted jobs invoke failed() hook and move to the Dead Letter Queue."""
    prefix = f"test_dlq_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    queue_name = "dead_letter_queue"

    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    key = f"failing_{uuid.uuid4().hex[:8]}"

    try:
        job = FailingJob(
            key=key,
            reason="Unrecoverable database failure",
            tries=2,
            backoff=0.05,
            queue=queue_name,
        )
        await dispatcher.push(job)

        worker = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            stop_when_empty=True,
            sleep=0.02,
        )
        await worker.work()

        assert worker.jobs_failed == 1

        # Verify failed() hook recorded in Redis
        hook_val = await client.get(f"{key}:failed_hook")
        assert hook_val is not None
        assert "RuntimeError: Unrecoverable database failure" in hook_val

        # Verify attempts
        attempts = await client.get(f"{key}:attempts")
        assert attempts == "2"

        # Verify Dead Letter Queue entry
        assert await dispatcher.failed_size(queue_name) == 1
        failed_records = await dispatcher.get_failed_jobs(queue_name)
        assert len(failed_records) == 1
        record = failed_records[0]
        assert record["job_id"] == job.job_id
        assert record["attempts"] == 2
        assert record["tries"] == 2
        assert record["exception"]["type"] == "RuntimeError"
        assert record["exception"]["message"] == "Unrecoverable database failure"
        assert "Traceback" in record["exception"]["traceback"]
    finally:
        await client.delete(f"{key}:attempts", f"{key}:failed_hook")
        await client.aclose()
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_worker_routes_malformed_and_unknown_payloads_to_dlq(
    redis_host: str, redis_port: int
) -> None:
    """Test that corrupt JSON or unknown job classes are safely routed to DLQ without crashing worker."""
    prefix = f"test_corrupt_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    queue_name = "corrupt_queue"

    try:
        # Push 1 corrupt JSON
        await dispatcher.push_raw(queue_name, "INVALID-JSON-PAYLOAD")

        # Push 1 valid JSON with unknown class
        unknown_envelope = json.dumps({"job_class": "non_existent_module:GhostJob"})
        await dispatcher.push_raw(queue_name, unknown_envelope)

        worker = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            stop_when_empty=True,
            sleep=0.01,
        )
        await worker.work()

        assert worker.jobs_failed == 2
        assert await dispatcher.failed_size(queue_name) == 2
    finally:
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_delayed_job_automatic_promotion(
    redis_host: str, redis_port: int
) -> None:
    """Test that delayed jobs are promoted and processed when their schedule arrives."""
    prefix = f"test_delayed_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    queue_name = "scheduled_queue"

    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    key = f"delayed_{uuid.uuid4().hex[:8]}"

    try:
        job = SimpleTestJob(key, "delayed_payload", queue=queue_name)
        # Push with 0.15s delay
        await dispatcher.push(job, delay=0.15)

        assert await dispatcher.size(queue_name) == 0
        assert await dispatcher.delayed_size(queue_name) == 1

        # Run worker with sleep=0.05
        worker = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            stop_when_empty=True,
            sleep=0.05,
        )
        await worker.work()

        assert worker.jobs_processed == 1
        assert await client.get(key) == "delayed_payload"
        assert await dispatcher.size(queue_name) == 0
        assert await dispatcher.delayed_size(queue_name) == 0
    finally:
        await client.delete(key)
        await client.aclose()
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_worker_timeout_handling(redis_host: str, redis_port: int) -> None:
    """Test that a job exceeding its configured timeout is cancelled and sent to DLQ."""
    prefix = f"test_timeout_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    queue_name = "timeout_queue"

    try:
        job = TimeoutJob(sleep_duration=0.5, timeout=0.05, tries=1, queue=queue_name)
        await dispatcher.push(job)

        worker = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            stop_when_empty=True,
            sleep=0.01,
        )
        await worker.work()

        assert worker.jobs_failed == 1
        assert await dispatcher.failed_size(queue_name) == 1
        failed_jobs = await dispatcher.get_failed_jobs(queue=queue_name)
        assert len(failed_jobs) == 1
        assert failed_jobs[0]["exception"]["type"] == "TimeoutError"
    finally:
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_concurrent_workers_500_jobs(
    redis_host: str, redis_port: int, tmp_path: Path
) -> None:
    """
    Stress & Concurrency Guarantee:
    Dispatch 500 jobs, run 3 worker processes concurrently, verify every job
    runs without dropped jobs or duplicates, and all 3 worker PIDs participated.
    """
    prefix = f"stress_500_{uuid.uuid4().hex[:8]}:"
    queue_name = "stress_queue"
    counter_key = f"counter_{uuid.uuid4().hex[:8]}"

    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    worker_procs: list[asyncio.subprocess.Process] = []

    try:
        total_jobs = 500
        # 1. Dispatch 500 jobs onto the Redis queue
        push_tasks = [
            dispatcher.push(
                StressCounterJob(counter_key, i, queue=queue_name),
                queue=queue_name,
            )
            for i in range(total_jobs)
        ]
        await asyncio.gather(*push_tasks)

        # Verify active queue size
        active_size = await dispatcher.size(queue_name)
        assert active_size == total_jobs

        # 2. Spawn 3 concurrent worker processes via CLI swiftpy queue:work
        _write_worker_boot_module(tmp_path)
        env = os.environ.copy()
        test_dir = os.path.dirname(os.path.abspath(__file__))
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{tmp_path}:{test_dir}:{current_pythonpath}"
            if current_pythonpath
            else f"{tmp_path}:{test_dir}"
        )
        env["QUEUE_PREFIX"] = prefix
        env["REDIS_HOST"] = redis_host
        env["REDIS_PORT"] = str(redis_port)

        cmd = [
            sys.executable,
            "-m",
            "swiftpy.console.cli",
            "queue:work",
            "--app",
            "worker_boot:app",
            "--queue",
            queue_name,
            "--prefix",
            prefix,
            "--stop-when-empty",
            "--sleep",
            "0.01",
        ]

        for _ in range(3):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            worker_procs.append(proc)

        # 3. Wait for all 3 worker processes while concurrently draining stdout/stderr to prevent pipe deadlock
        outputs = await asyncio.wait_for(
            asyncio.gather(*(p.communicate() for p in worker_procs)),
            timeout=60.0,
        )

        # Verify exit codes and report any process failure
        for idx, (stdout_data, stderr_data) in enumerate(outputs):
            returncode = worker_procs[idx].returncode
            if returncode != 0:
                raise AssertionError(
                    f"Worker process {idx} failed with exit code {returncode}:\n"
                    f"STDOUT: {stdout_data.decode()}\n"
                    f"STDERR: {stderr_data.decode()}"
                )

        # 4. Verify guarantees:
        # A) Total executed jobs is exactly 500 (list length)
        total_executed = await client.llen(f"{counter_key}:list")
        assert total_executed == total_jobs, (
            f"Expected {total_jobs} total executions, got {total_executed}"
        )

        # B) Every job ran exactly once without duplicate runs (set cardinality)
        unique_executed = await client.scard(f"{counter_key}:set")
        assert unique_executed == total_jobs, (
            f"Expected {total_jobs} unique jobs, got {unique_executed}"
        )

        # C) Every index from 0 to 499 was executed
        all_raw_indices = await client.lrange(f"{counter_key}:list", 0, -1)
        executed_indices = {int(x) for x in all_raw_indices}
        expected_indices = set(range(total_jobs))
        assert executed_indices == expected_indices, (
            "Some job indices were missed or dropped"
        )

        # D) Multi-worker participation: verify multiple distinct worker PIDs participated
        pids = await client.smembers(f"{counter_key}:pids")
        assert len(pids) >= 2, f"Expected multiple worker PIDs, got {pids}"

        # E) Zero jobs dropped, active queue empty, DLQ empty
        assert await dispatcher.size(queue_name) == 0
        assert await dispatcher.delayed_size(queue_name) == 0
        assert await dispatcher.failed_size(queue_name) == 0
    finally:
        for p in worker_procs:
            try:
                if p.returncode is None:
                    p.terminate()
                    try:
                        await asyncio.wait_for(p.wait(), timeout=2.0)
                    except TimeoutError:
                        p.kill()
                        await p.wait()
            except (ProcessLookupError, OSError):
                continue
        await client.delete(
            f"{counter_key}:list", f"{counter_key}:set", f"{counter_key}:pids"
        )
        await client.aclose()
        await dispatcher.clear(queue_name)
        await dispatcher.close()


def test_deserialization_trusted_namespaces() -> None:
    """Test that untrusted module namespaces are rejected before importing."""
    try:
        Job.set_trusted_namespaces(["app", "tests", "test_queue"])

        # Trusted namespace succeeds
        serialized = SimpleTestJob("k", "v").serialize()
        restored = Job.deserialize(serialized)
        assert isinstance(restored, SimpleTestJob)

        # Untrusted module rejected BEFORE importlib.import_module
        with pytest.raises(JobSerializationError, match="not in trusted namespaces"):
            Job.deserialize({"job_class": "os:system"}, allow_unsigned=True)

        with pytest.raises(JobSerializationError, match="not in trusted namespaces"):
            Job.deserialize({"job_class": "subprocess:Popen"}, allow_unsigned=True)
    finally:
        Job.set_trusted_namespaces(None)


async def test_multi_worker_delayed_promotion(
    redis_host: str, redis_port: int, tmp_path: Path
) -> None:
    """
    Test: 60 delayed jobs scheduled (with identical and staggered timestamps).
    3 concurrent worker processes compete to promote and process them.
    Verify zero duplicates and zero stranded delayed jobs.
    """
    prefix = f"multi_delayed_{uuid.uuid4().hex[:8]}:"
    queue_name = "delayed_multi_queue"
    counter_key = f"counter_delayed_{uuid.uuid4().hex[:8]}"

    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    worker_procs: list[asyncio.subprocess.Process] = []

    try:
        total_delayed = 60
        # Schedule jobs: 30 jobs with identical 0.1s delay, 30 jobs with staggered delays (0.15s - 0.25s)
        push_tasks = []
        for i in range(total_delayed):
            delay = 0.1 if i < 30 else 0.15 + (i % 5) * 0.02
            push_tasks.append(
                dispatcher.push(
                    StressCounterJob(counter_key, i, queue=queue_name),
                    queue=queue_name,
                    delay=delay,
                )
            )
        await asyncio.gather(*push_tasks)

        assert await dispatcher.delayed_size(queue_name) == total_delayed
        assert await dispatcher.size(queue_name) == 0

        # Start 3 worker processes
        _write_worker_boot_module(tmp_path)
        env = os.environ.copy()
        test_dir = os.path.dirname(os.path.abspath(__file__))
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{tmp_path}:{test_dir}:{current_pythonpath}"
            if current_pythonpath
            else f"{tmp_path}:{test_dir}"
        )
        env["QUEUE_PREFIX"] = prefix
        env["REDIS_HOST"] = redis_host
        env["REDIS_PORT"] = str(redis_port)

        cmd = [
            sys.executable,
            "-m",
            "swiftpy.console.cli",
            "queue:work",
            "--app",
            "worker_boot:app",
            "--queue",
            queue_name,
            "--prefix",
            prefix,
            "--stop-when-empty",
            "--sleep",
            "0.02",
        ]

        for _ in range(3):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            worker_procs.append(proc)

        outputs = await asyncio.wait_for(
            asyncio.gather(*(p.communicate() for p in worker_procs)),
            timeout=30.0,
        )

        for idx, (stdout_data, stderr_data) in enumerate(outputs):
            assert worker_procs[idx].returncode == 0, (
                f"Worker {idx} failed: {stderr_data.decode()}"
            )

        # Verify all 60 delayed jobs were promoted and executed
        total_executed = await client.llen(f"{counter_key}:list")
        assert total_executed == total_delayed
        unique_executed = await client.scard(f"{counter_key}:set")
        assert unique_executed == total_delayed

        # Queues should be completely empty
        assert await dispatcher.size(queue_name) == 0
        assert await dispatcher.delayed_size(queue_name) == 0
        assert await dispatcher.failed_size(queue_name) == 0
    finally:
        for p in worker_procs:
            try:
                if p.returncode is None:
                    p.terminate()
                    try:
                        await asyncio.wait_for(p.wait(), timeout=2.0)
                    except TimeoutError:
                        p.kill()
                        await p.wait()
            except (ProcessLookupError, OSError):
                continue
        await client.delete(
            f"{counter_key}:list", f"{counter_key}:set", f"{counter_key}:pids"
        )
        await client.aclose()
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_worker_list_backoff_schedule(redis_host: str, redis_port: int) -> None:
    """Test that a list-based backoff schedule enforces progressive delays between retries."""
    prefix = f"test_list_backoff_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    queue_name = "list_backoff_queue"

    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    key = f"timestamps_{uuid.uuid4().hex[:8]}"

    try:
        # Backoff: 0.05s for 1st retry, 0.1s for 2nd retry
        job = TimestampedFlakyJob(key=key, backoff=[0.05, 0.1], queue=queue_name)
        await dispatcher.push(job)

        worker = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            stop_when_empty=True,
            sleep=0.01,
        )
        await worker.work()

        assert await client.get(f"{key}:success") == "1"
        timestamps = [float(x) for x in await client.lrange(f"{key}:timestamps", 0, -1)]
        assert len(timestamps) == 3

        delay_attempt_1_to_2 = timestamps[1] - timestamps[0]
        delay_attempt_2_to_3 = timestamps[2] - timestamps[1]

        # Delay 1 should be at least ~0.04s, Delay 2 should be at least ~0.08s
        assert delay_attempt_1_to_2 >= 0.04
        assert delay_attempt_2_to_3 >= 0.08
    finally:
        await client.delete(f"{key}:attempts", f"{key}:timestamps", f"{key}:success")
        await client.aclose()
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_worker_immediate_retry_zero_delay(
    redis_host: str, redis_port: int
) -> None:
    """Test that backoff=0 re-queues immediately into active queue without entering delayed ZSET."""
    prefix = f"test_zero_backoff_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    queue_name = "zero_backoff_queue"

    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    key = f"zero_{uuid.uuid4().hex[:8]}"

    try:
        job = ZeroBackoffJob(key=key, queue=queue_name)
        await dispatcher.push(job)

        worker = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            stop_when_empty=True,
            sleep=0.01,
        )
        await worker.work()

        assert await client.get(f"{key}:success") == "1"
        assert int(await client.get(f"{key}:attempts") or 0) == 2
        # Delayed queue was never used
        assert await dispatcher.delayed_size(queue_name) == 0
    finally:
        await client.delete(f"{key}:attempts", f"{key}:success")
        await client.aclose()
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_worker_dead_letter_queue_when_failed_hook_raises(
    redis_host: str, redis_port: int
) -> None:
    """Test that even if a job's failed() hook raises an exception, the job is still safely saved to DLQ."""
    prefix = f"test_hook_err_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    queue_name = "failing_hook_queue"

    key = f"hook_err_{uuid.uuid4().hex[:8]}"

    try:
        job = FailingHookJob(key=key, queue=queue_name)
        await dispatcher.push(job)

        worker = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            stop_when_empty=True,
            sleep=0.01,
        )
        await worker.work()

        assert worker.jobs_failed == 1
        assert await dispatcher.failed_size(queue_name) == 1
        failed_jobs = await dispatcher.get_failed_jobs(queue=queue_name)
        assert len(failed_jobs) == 1
        # DLQ record captured the original primary exception
        assert failed_jobs[0]["exception"]["message"] == "Primary job failure"
    finally:
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_worker_in_flight_cancellation_requeues_job(
    redis_host: str, redis_port: int
) -> None:
    """Test that when a worker task is cancelled mid-execution, the in-flight job is safely re-queued."""
    prefix = f"test_cancel_{uuid.uuid4().hex[:8]}:"
    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    queue_name = "cancellable_queue"

    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    key = f"cancel_{uuid.uuid4().hex[:8]}"

    try:
        job = CancellableJob(key=key, queue=queue_name)
        await dispatcher.push(job)

        worker = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            sleep=0.01,
        )
        worker_task = asyncio.create_task(worker.work())

        # Wait until job signals that it started executing
        started = False
        for _ in range(50):
            if await client.get(f"{key}:started") == "1":
                started = True
                break
            await asyncio.sleep(0.05)
        assert started, "Job never started executing"

        # Cancel the worker task mid-job
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        # Job was not completed
        assert await client.get(f"{key}:completed") is None

        # Verify that the cancelled job was returned to the active queue!
        assert await dispatcher.size(queue_name) == 1

        # Another worker can now pull and finish it!
        # Reset the started flag to check second execution
        await client.delete(f"{key}:started")
        worker2 = QueueWorker(
            queues=[queue_name],
            dispatcher=dispatcher,
            stop_when_empty=True,
            sleep=0.01,
        )
        # Use a short timeout so worker2 can complete
        await dispatcher.clear(queue_name)
        # Push a simple job to verify worker2 can process normal jobs
        await dispatcher.push(SimpleTestJob(f"{key}:finish", "done", queue=queue_name))
        await worker2.work()
        assert await client.get(f"{key}:finish") == "done"
    finally:
        await client.delete(f"{key}:started", f"{key}:completed", f"{key}:finish")
        await client.aclose()
        await dispatcher.clear(queue_name)
        await dispatcher.close()


async def test_worker_cli_max_jobs(
    redis_host: str, redis_port: int, tmp_path: Path
) -> None:
    """Test that --max-jobs stops the worker after processing the specified job count."""
    prefix = f"test_max_jobs_{uuid.uuid4().hex[:8]}:"
    queue_name = "max_jobs_queue"

    dispatcher = QueueDispatcher(host=redis_host, port=redis_port, prefix=prefix)
    worker_procs: list[asyncio.subprocess.Process] = []

    try:
        # Push 10 jobs
        for i in range(10):
            await dispatcher.push(
                SimpleTestJob(f"k_{i}", f"val_{i}", queue=queue_name),
                queue=queue_name,
            )
        assert await dispatcher.size(queue_name) == 10

        # Run worker with --max-jobs 4
        _write_worker_boot_module(tmp_path)
        env = os.environ.copy()
        test_dir = os.path.dirname(os.path.abspath(__file__))
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{tmp_path}:{test_dir}:{current_pythonpath}"
            if current_pythonpath
            else f"{tmp_path}:{test_dir}"
        )
        env["QUEUE_PREFIX"] = prefix
        env["REDIS_HOST"] = redis_host
        env["REDIS_PORT"] = str(redis_port)

        cmd = [
            sys.executable,
            "-m",
            "swiftpy.console.cli",
            "queue:work",
            "--app",
            "worker_boot:app",
            "--queue",
            queue_name,
            "--prefix",
            prefix,
            "--max-jobs",
            "4",
            "--sleep",
            "0.01",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        worker_procs.append(proc)

        _stdout_data, stderr_data = await asyncio.wait_for(
            proc.communicate(), timeout=15.0
        )
        assert proc.returncode == 0, f"Worker failed: {stderr_data.decode()}"

        # Exactly 4 jobs processed, 6 remain in queue
        remaining = await dispatcher.size(queue_name)
        assert remaining == 6
    finally:
        for p in worker_procs:
            try:
                if p.returncode is None:
                    p.terminate()
                    await p.wait()
            except (ProcessLookupError, OSError):
                continue
        await dispatcher.clear(queue_name)
        await dispatcher.close()
