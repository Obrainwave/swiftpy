from __future__ import annotations

import asyncio
import inspect
import json
import logging
import signal
import time
import uuid
from typing import Any

from redis.exceptions import RedisError

from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job, JobSerializationError

logger = logging.getLogger("swiftpy.queue")

# Atomic Lua script to migrate due delayed jobs into the active queue list
_PROMOTE_DELAYED_LUA = """
local due = redis.call('zrangebyscore', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 50)
for i, job in ipairs(due) do
    redis.call('zrem', KEYS[1], job)
    redis.call('rpush', KEYS[2], job)
end
return #due
"""


class QueueWorker:
    """
    Queue worker process responsible for consuming jobs from Redis queues,
    migrating scheduled delayed jobs, managing backoff retries, and recording
    failed jobs into the Dead Letter Queue.
    """

    def __init__(
        self,
        queues: str | list[str] = "default",
        dispatcher: QueueDispatcher | None = None,
        sleep: float = 1.0,
        max_jobs: int | None = None,
        stop_when_empty: bool = False,
        timeout: float | None = None,
        worker_id: str | None = None,
    ) -> None:
        if isinstance(queues, str):
            self.queues = [q.strip() for q in queues.split(",") if q.strip()]
        else:
            self.queues = list(queues) if queues else ["default"]

        self._owns_dispatcher = dispatcher is None
        self.dispatcher: QueueDispatcher = (
            dispatcher if dispatcher is not None else QueueDispatcher()
        )
        self.sleep = max(0.01, float(sleep))
        self.max_jobs = max_jobs
        self.stop_when_empty = stop_when_empty
        self.timeout = timeout
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"

        self._should_stop: bool = False
        self._is_working: bool = False
        self.jobs_processed: int = 0
        self.jobs_failed: int = 0
        self._promote_script_sha: str | None = None

    def stop(self) -> None:
        """Signal the worker process to gracefully stop."""
        logger.info("Worker %s received stop signal", self.worker_id)
        self._should_stop = True

    def _setup_signal_handlers(self) -> None:
        """Register SIGINT and SIGTERM handlers for graceful shutdown."""
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self.stop)
                except (NotImplementedError, RuntimeError):
                    signal.signal(sig, lambda s, f: self.stop())
        except (RuntimeError, ValueError):
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda s, f: self.stop())

    async def _promote_delayed_jobs(self, queue: str) -> int:
        """
        Atomically move scheduled jobs with score <= now from delayed ZSET to active queue list.
        """
        client = await self.dispatcher.get_client()
        delayed_key = self.dispatcher._delayed_key(queue)
        queue_key = self.dispatcher._queue_key(queue)
        now_str = str(time.time())

        try:
            res = await client.eval(
                _PROMOTE_DELAYED_LUA,
                2,
                delayed_key,
                queue_key,
                now_str,
            )
            return int(res) if res is not None else 0
        except (TimeoutError, RedisError, ConnectionError, OSError) as exc:
            logger.warning(
                "Error promoting delayed jobs for queue '%s': %s", queue, exc
            )
            return 0

    async def _pop_next_job(self) -> tuple[str, str] | None:
        """
        Check all monitored queues in order and pop the first available job payload.
        """
        client = await self.dispatcher.get_client()
        for queue in self.queues:
            # First promote any delayed jobs due for this queue
            await self._promote_delayed_jobs(queue)

            queue_key = self.dispatcher._queue_key(queue)
            raw_payload = await client.lpop(queue_key)
            if isinstance(raw_payload, str):
                return queue, raw_payload
            if isinstance(raw_payload, bytes):
                return queue, raw_payload.decode("utf-8")
        return None

    async def _all_queues_empty(self) -> bool:
        """
        Check whether all monitored active queues and delayed queues have zero jobs.
        """
        for queue in self.queues:
            active_size = await self.dispatcher.size(queue)
            if active_size > 0:
                return False
            delayed_size = await self.dispatcher.delayed_size(queue)
            if delayed_size > 0:
                return False
        return True

    async def process_job(self, queue: str, raw_payload: str) -> bool:
        """
        Deserialize and process a single queued job payload.
        Returns True if handled, False on failure.
        """
        # 1. Parse JSON envelope
        try:
            data: dict[str, Any] = json.loads(raw_payload)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            logger.error(
                "Worker %s encountered unparseable JSON payload: %s",
                self.worker_id,
                exc,
            )
            await self.dispatcher.push_failed(
                job=None,
                exception=exc,
                queue=queue,
                payload={"raw": raw_payload},
            )
            self.jobs_failed += 1
            return False

        # 2. Reconstruct Job instance
        try:
            job = Job.deserialize(data)
        except JobSerializationError as exc:
            logger.error(
                "Worker %s failed to deserialize job envelope: %s",
                self.worker_id,
                exc,
            )
            await self.dispatcher.push_failed(
                job=None,
                exception=exc,
                queue=queue,
                payload=data,
            )
            self.jobs_failed += 1
            return False

        # 3. Increment attempt count
        job.attempts += 1
        effective_timeout = job.timeout or self.timeout

        # 4. Execute Job.handle()
        logger.debug(
            "Worker %s executing job %s [%s] attempt %d/%d on queue '%s'",
            self.worker_id,
            job.job_id,
            job.__class__.__name__,
            job.attempts,
            job.tries,
            queue,
        )

        try:
            if effective_timeout is not None and effective_timeout > 0:
                await asyncio.wait_for(job.handle(), timeout=effective_timeout)
            else:
                await job.handle()
        except asyncio.CancelledError:
            # Worker task was cancelled mid-execution: preserve job by returning it to active queue
            logger.warning(
                "Worker %s cancelled while processing job %s; re-queueing",
                self.worker_id,
                job.job_id,
            )
            await self.dispatcher.push_raw(queue, raw_payload)
            raise
        except Exception as exc:  # noqa: BLE001 - user job handler can raise any standard Exception
            await self._handle_job_failure(job, queue, raw_payload, data, exc)
            return False
        else:
            logger.info(
                "Worker %s completed job %s [%s]",
                self.worker_id,
                job.job_id,
                job.__class__.__name__,
            )
            self.jobs_processed += 1
            return True

    async def _handle_job_failure(
        self,
        job: Job,
        queue: str,
        raw_payload: str,
        data: dict[str, Any],
        exc: BaseException,
    ) -> None:
        """
        Handle job failure: retry with backoff if attempts < tries, or move to DLQ.
        """
        logger.warning(
            "Job %s [%s] failed on attempt %d/%d: %s",
            job.job_id,
            job.__class__.__name__,
            job.attempts,
            job.tries,
            exc,
        )

        if job.attempts < job.tries:
            # Retry with backoff
            delay = job.get_backoff_delay()
            logger.info(
                "Retrying job %s [%s] in %.2fs (attempt %d/%d)",
                job.job_id,
                job.__class__.__name__,
                delay,
                job.attempts + 1,
                job.tries,
            )
            await self.dispatcher.push(job, queue=queue, delay=delay)
        else:
            # Retries exhausted -> Dead Letter Queue
            logger.error(
                "Job %s [%s] exhausted all %d attempts. Moving to Dead Letter Queue.",
                job.job_id,
                job.__class__.__name__,
                job.tries,
            )
            try:
                res = job.failed(exc)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                logger.exception(
                    "Error executing failed() hook on job %s",
                    job.job_id,
                )

            await self.dispatcher.push_failed(
                job=job,
                exception=exc,
                queue=queue,
                payload=data,
            )
            self.jobs_failed += 1

    async def work(self) -> None:
        """
        Run the worker loop pulling and executing jobs.
        """
        self._setup_signal_handlers()
        self._is_working = True
        logger.info(
            "Worker %s started monitoring queues: %s",
            self.worker_id,
            ", ".join(self.queues),
        )

        try:
            while not self._should_stop:
                # Check maximum job limit
                if (
                    self.max_jobs is not None
                    and (self.jobs_processed + self.jobs_failed) >= self.max_jobs
                ):
                    logger.info(
                        "Worker %s reached max_jobs limit (%d)",
                        self.worker_id,
                        self.max_jobs,
                    )
                    break

                job_data = await self._pop_next_job()
                if job_data is not None:
                    queue, raw_payload = job_data
                    await self.process_job(queue, raw_payload)
                    continue

                # No active job available
                if self.stop_when_empty and await self._all_queues_empty():
                    logger.info(
                        "Worker %s stopping because queues are empty",
                        self.worker_id,
                    )
                    break

                await asyncio.sleep(min(self.sleep, 0.1))
        finally:
            self._is_working = False
            if self._owns_dispatcher:
                await self.dispatcher.close()
            logger.info(
                "Worker %s stopped. (Processed: %d, Failed: %d)",
                self.worker_id,
                self.jobs_processed,
                self.jobs_failed,
            )

    async def run(self) -> None:
        """Alias for work()."""
        await self.work()
