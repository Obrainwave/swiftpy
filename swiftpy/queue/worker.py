from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import signal
import time
import uuid
from typing import Any

from redis.exceptions import NoScriptError, RedisError

from swiftpy.core.config import ConfigurationError
from swiftpy.core.container import get_container
from swiftpy.database.pool import DatabasePool
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job, JobSerializationError
from swiftpy.support.keys import validate_signing_key

logger = logging.getLogger("swiftpy.queue")

_QUEUE_NAME_REGEX = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

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

    Queue delivery operates with at-least-once semantics; job handlers must be idempotent.
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
        concurrency: int = 1,
        promote_interval: float = 0.1,
        allow_unsigned: bool | None = None,
    ) -> None:
        if isinstance(queues, str):
            raw_queues = [q.strip() for q in queues.split(",") if q.strip()]
        else:
            raw_queues = list(queues) if queues else []

        if not raw_queues:
            raise ValueError("Invalid queue name: queue name cannot be empty")

        for q in raw_queues:
            if not _QUEUE_NAME_REGEX.fullmatch(q):
                raise ValueError(
                    f"Invalid queue name '{q}': must match ^[A-Za-z0-9_.-]{{1,64}}$"
                )
        self.queues = raw_queues

        self._owns_dispatcher = dispatcher is None
        self.dispatcher: QueueDispatcher = (
            dispatcher if dispatcher is not None else QueueDispatcher()
        )
        self.sleep = max(0.01, float(sleep))
        self.max_jobs = max_jobs
        self.stop_when_empty = stop_when_empty
        self.timeout = timeout
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.concurrency = max(1, int(concurrency))
        self.promote_interval = max(0.01, float(promote_interval))
        self.allow_unsigned = allow_unsigned
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._original_handlers: dict[signal.Signals, Any] = {}

        self._should_stop: bool = False
        self._is_working: bool = False
        self.jobs_processed: int = 0
        self.jobs_failed: int = 0
        self._promote_script_sha: str | None = None
        self._last_promoted_at: float = 0.0

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
                    self._original_handlers[sig] = signal.getsignal(sig)
                except (ValueError, AttributeError):
                    pass
                try:
                    loop.add_signal_handler(sig, self.stop)
                except (NotImplementedError, RuntimeError):
                    signal.signal(sig, lambda s, f: self.stop())
        except (RuntimeError, ValueError):
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    self._original_handlers[sig] = signal.getsignal(sig)
                except (ValueError, AttributeError):
                    pass
                signal.signal(sig, lambda s, f: self.stop())

    def _restore_signal_handlers(self) -> None:
        """Restore original signal handlers on worker exit."""
        for sig, handler in self._original_handlers.items():
            try:
                loop = asyncio.get_running_loop()
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            try:
                if handler is not None:
                    signal.signal(sig, handler)
            except (ValueError, AttributeError):
                pass
        self._original_handlers.clear()

    async def _promote_delayed_jobs(self, queue: str) -> int:
        """
        Atomically move scheduled jobs with score <= now from delayed ZSET to active queue list.
        Uses SHA-cached EVALSHA for efficiency.
        """
        if not hasattr(self.dispatcher, "get_client"):
            return 0
        client = await self.dispatcher.get_client()
        delayed_key = self.dispatcher._delayed_key(queue)
        queue_key = self.dispatcher._queue_key(queue)
        now_str = str(time.time())

        try:
            if self._promote_script_sha is None:
                try:
                    self._promote_script_sha = await client.script_load(_PROMOTE_DELAYED_LUA)
                except (RedisError, ConnectionError, OSError):
                    logger.debug("Could not pre-load promote script SHA", exc_info=True)

            if self._promote_script_sha is not None:
                try:
                    res = await client.evalsha(
                        self._promote_script_sha,
                        2,
                        delayed_key,
                        queue_key,
                        now_str,
                    )
                except NoScriptError:
                    self._promote_script_sha = await client.script_load(_PROMOTE_DELAYED_LUA)
                    res = await client.evalsha(
                        self._promote_script_sha,
                        2,
                        delayed_key,
                        queue_key,
                        now_str,
                    )
            else:
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
        Check all monitored queues in order, throttled delayed-job promotion & lease reaping,
        and atomically pop & lease the first available job payload.
        """
        now = time.monotonic()
        if (
            self._last_promoted_at <= 0.0
            or (now - self._last_promoted_at) >= self.promote_interval
        ):
            self._last_promoted_at = now
            for queue in self.queues:
                await self._promote_delayed_jobs(queue)
                await self.dispatcher.reap_expired_leases(queue)

        for queue in self.queues:
            lease_duration = (self.timeout + 30.0) if self.timeout else 60.0
            raw_payload = await self.dispatcher.pop_and_lease(
                queue,
                self.worker_id,
                lease_duration=lease_duration,
            )
            if raw_payload is not None:
                return queue, raw_payload
        return None

    async def _all_queues_empty(self) -> bool:
        """
        Check whether all monitored active queues, delayed queues, and in-flight processing lists have zero jobs.
        """
        for queue in self.queues:
            active_size = await self.dispatcher.size(queue)
            if active_size > 0:
                return False
            delayed_size = await self.dispatcher.delayed_size(queue)
            if delayed_size > 0:
                return False
            proc_size = await self.dispatcher.processing_size(queue)
            if proc_size > 0:
                return False
        return True

    async def process_job(self, queue: str, raw_payload: str) -> bool:
        """
        Deserialize and process a single queued job payload.
        Returns True if handled, False on failure.
        """
        # 1. Parse JSON envelope
        try:
            data = json.loads(raw_payload)
            if not isinstance(data, dict):
                raise JobSerializationError(
                    f"Invalid job envelope: expected dict, got {type(data).__name__}"
                )
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
            JobSerializationError,
        ) as exc:
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
            await self.dispatcher.ack(queue, self.worker_id, raw_payload)
            self.jobs_failed += 1
            return False

        # 2. Reconstruct Job instance
        try:
            job = Job.deserialize(data, allow_unsigned=self.allow_unsigned)
        except (
            JobSerializationError,
            TypeError,
            ValueError,
            KeyError,
            AttributeError,
        ) as exc:
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
            await self.dispatcher.ack(queue, self.worker_id, raw_payload)
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
            # Worker task was cancelled mid-execution: preserve job and attempts by re-queuing at head
            logger.warning(
                "Worker %s cancelled while processing job %s; re-queueing",
                self.worker_id,
                job.job_id,
            )
            requeue_payload = json.dumps(
                job.serialize(allow_unsigned=self.allow_unsigned),
                ensure_ascii=False,
            )
            await self.dispatcher.release(
                queue,
                self.worker_id,
                raw_payload,
                delay=0,
                new_payload=requeue_payload,
            )
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
            await self.dispatcher.ack(queue, self.worker_id, raw_payload)
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
            requeue_payload = json.dumps(
                job.serialize(allow_unsigned=self.allow_unsigned),
                ensure_ascii=False,
            )
            await self.dispatcher.release(
                queue,
                self.worker_id,
                raw_payload,
                delay=delay,
                new_payload=requeue_payload,
            )
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
            await self.dispatcher.ack(queue, self.worker_id, raw_payload)
            self.jobs_failed += 1

    async def _run_job_with_semaphore(self, queue: str, raw_payload: str) -> None:
        """Run job inside concurrency semaphore."""
        try:
            await self.process_job(queue, raw_payload)
        finally:
            self._semaphore.release()

    async def work(self) -> None:
        """
        Run the worker loop pulling and executing jobs.
        """
        effective_key = Job.get_app_key()
        unsigned_allowed = (
            self.allow_unsigned
            if self.allow_unsigned is not None
            else Job.get_allow_unsigned()
        )
        if effective_key is None or not str(effective_key).strip():
            if not unsigned_allowed:
                raise ConfigurationError(
                    "APP_KEY (>=32 bytes) is required to run QueueWorker. "
                    "Configure APP_KEY or pass allow_unsigned=True for local tests."
                )
        else:
            validate_signing_key(str(effective_key), purpose="APP_KEY")

        self._setup_signal_handlers()
        self._is_working = True

        # Initialize DatabasePool if available in the application container
        try:
            container = get_container()
            if container.has(DatabasePool):
                pool = container.resolve(DatabasePool)
                if not getattr(pool, "is_initialized", False):
                    await pool.init()
        except RuntimeError:
            pass

        logger.info(
            "Worker %s started monitoring queues: %s (concurrency=%d)",
            self.worker_id,
            ", ".join(self.queues),
            self.concurrency,
        )

        try:
            while not self._should_stop:
                # Check maximum job limit including in-flight tasks
                if self.max_jobs is not None:
                    completed = self.jobs_processed + self.jobs_failed
                    if completed >= self.max_jobs:
                        logger.info(
                            "Worker %s reached max_jobs limit (%d)",
                            self.worker_id,
                            self.max_jobs,
                        )
                        break
                    if completed + len(self._active_tasks) >= self.max_jobs:
                        await asyncio.wait(
                            self._active_tasks,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        continue

                acquired = False
                if self.concurrency > 1:
                    await self._semaphore.acquire()
                    acquired = True
                try:
                    job_data = await self._pop_next_job()
                except BaseException:
                    if acquired:
                        self._semaphore.release()
                    raise

                if job_data is not None:
                    queue, raw_payload = job_data
                    if self.concurrency > 1:
                        try:
                            task = asyncio.create_task(
                                self._run_job_with_semaphore(queue, raw_payload)
                            )
                        except BaseException:
                            if acquired:
                                self._semaphore.release()
                            raise
                        self._active_tasks.add(task)

                        def _on_done(t: asyncio.Task[Any]) -> None:
                            self._active_tasks.discard(t)
                            if not t.cancelled() and t.exception() is not None:
                                logger.exception(
                                    "Queue worker task failed",
                                    exc_info=t.exception(),
                                )

                        task.add_done_callback(_on_done)
                    else:
                        try:
                            await self.process_job(queue, raw_payload)
                        except Exception:
                            logger.exception(
                                "Worker %s encountered unexpected error processing job on queue '%s'",
                                self.worker_id,
                                queue,
                            )
                    continue

                if acquired:
                    self._semaphore.release()

                # No active job available
                if self.stop_when_empty and await self._all_queues_empty():
                    if self._active_tasks:
                        await asyncio.wait(
                            self._active_tasks,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        continue
                    logger.info(
                        "Worker %s stopping because queues are empty",
                        self.worker_id,
                    )
                    break

                await asyncio.sleep(self.sleep)
        finally:
            self._is_working = False
            if self._active_tasks:
                await asyncio.gather(*self._active_tasks, return_exceptions=True)
            self._restore_signal_handlers()
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
