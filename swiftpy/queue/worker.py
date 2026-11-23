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
from swiftpy.scheduling.events import (
    ScheduledTaskCompleted,
    ScheduledTaskFailed,
    dispatch_scheduler_event,
)
from swiftpy.scheduling.models import OccurrenceStatus, utc_now
from swiftpy.scheduling.store import (
    RedisScheduleStore,
    ScheduleStore,
    resolve_active_schedule_store,
)
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
        schedule_store: ScheduleStore | None = None,
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
        self.schedule_store: ScheduleStore | None = schedule_store
        self._owned_schedule_stores: dict[str, RedisScheduleStore] = {}
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

    def _resolve_schedule_store(
        self, raw_data: dict[str, Any]
    ) -> ScheduleStore | None:
        if self.schedule_store is not None:
            return self.schedule_store

        active_store = resolve_active_schedule_store()
        if active_store is not None:
            raw_prefix = raw_data.get("_swiftpy_schedule_prefix")
            active_prefix = getattr(active_store, "prefix", None)
            if (
                not isinstance(raw_prefix, str)
                or not raw_prefix
                or not isinstance(active_prefix, str)
                or raw_prefix == active_prefix
            ):
                return active_store

        raw_prefix = raw_data.get("_swiftpy_schedule_prefix")
        prefix = (
            raw_prefix
            if isinstance(raw_prefix, str) and raw_prefix
            else "swiftpy_schedule:"
        )
        cached = self._owned_schedule_stores.get(prefix)
        if cached is not None:
            return cached

        if not isinstance(self.dispatcher, QueueDispatcher):
            return None

        if self.dispatcher._client is not None:
            store = RedisScheduleStore(
                redis_client=self.dispatcher._client,
                prefix=prefix,
            )
        else:
            store = RedisScheduleStore(
                redis_url=self.dispatcher._redis_url,
                host=self.dispatcher.host,
                port=self.dispatcher.port,
                password=self.dispatcher.password,
                database=self.dispatcher._explicit_database,
                prefix=prefix,
            )
        self._owned_schedule_stores[prefix] = store
        return store

    @staticmethod
    def _extract_schedule_meta(
        raw_data: dict[str, Any],
    ) -> tuple[str | None, str | None, str | None]:
        occ_raw = raw_data.get("_swiftpy_schedule_occurrence")
        lock_key_raw = raw_data.get("_swiftpy_schedule_lock_key")
        lock_token_raw = raw_data.get("_swiftpy_schedule_lock_token")
        occ_id = occ_raw if isinstance(occ_raw, str) and occ_raw else None
        lock_key = (
            lock_key_raw if isinstance(lock_key_raw, str) and lock_key_raw else None
        )
        lock_token = (
            lock_token_raw
            if isinstance(lock_token_raw, str) and lock_token_raw
            else None
        )
        return occ_id, lock_key, lock_token

    async def _mark_schedule_running(self, job_data: dict[str, Any]) -> bool:
        """Record the running state; return True if the occurrence already completed (duplicate delivery)."""
        occ_id, _, _ = self._extract_schedule_meta(job_data)
        if occ_id is None:
            return False
        try:
            store = self._resolve_schedule_store(job_data)
            if store is not None:
                occ = await store.mark_running(occ_id, now=utc_now())
                return occ is not None and occ.status == OccurrenceStatus.COMPLETED
        except Exception:
            logger.warning(
                "Failed to record running state for scheduled occurrence '%s'; continuing execution",
                occ_id,
                exc_info=True,
            )
        return False

    async def _finalize_scheduled_occurrence(
        self,
        job_data: dict[str, Any],
        *,
        completed: bool,
        error: str | None = None,
    ) -> None:
        occ_id, lock_key, lock_token = self._extract_schedule_meta(job_data)
        if occ_id is None:
            return
        state_label = "completed" if completed else "failed"
        try:
            store = self._resolve_schedule_store(job_data)
            if store is None:
                return
            now = utc_now()
            occ = (
                await store.mark_completed(occ_id, now=now)
                if completed
                else await store.mark_failed(occ_id, error=error or "", now=now)
            )
            if occ is None and lock_key is not None and lock_token is not None:
                await store.release_overlap_lock(lock_key, lock_token)
            if occ is not None:
                event: ScheduledTaskCompleted | ScheduledTaskFailed = (
                    ScheduledTaskCompleted(occurrence=occ)
                    if completed
                    else ScheduledTaskFailed(occurrence=occ, error=error or "")
                )
                await dispatch_scheduler_event(event)
        except Exception:
            logger.warning(
                "Failed to finalize %s state for scheduled occurrence '%s'",
                state_label,
                occ_id,
                exc_info=True,
            )

    async def _mark_schedule_completed(self, job_data: dict[str, Any]) -> None:
        await self._finalize_scheduled_occurrence(job_data, completed=True)

    async def _mark_schedule_failed(
        self, job_data: dict[str, Any], error: str
    ) -> None:
        await self._finalize_scheduled_occurrence(
            job_data, completed=False, error=error
        )

    @staticmethod
    def _schedule_lock_ttl(job_data: dict[str, Any]) -> float | None:
        raw_ttl = job_data.get("_swiftpy_schedule_lock_ttl")
        if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, (int, float)):
            return None
        return float(raw_ttl) if raw_ttl > 0 else None

    async def _extend_schedule_lock(
        self, job_data: dict[str, Any], ttl_seconds: float
    ) -> None:
        _, lock_key, lock_token = self._extract_schedule_meta(job_data)
        if lock_key is None or lock_token is None or ttl_seconds <= 0:
            return
        try:
            store = self._resolve_schedule_store(job_data)
            if store is not None:
                await store.extend_overlap_lock(lock_key, lock_token, ttl_seconds)
        except Exception:
            logger.warning(
                "Failed to extend overlap lock '%s'", lock_key, exc_info=True
            )

    async def _lock_heartbeat(self, job_data: dict[str, Any], ttl: float) -> None:
        interval = max(0.5, ttl / 3.0)
        while True:
            await asyncio.sleep(interval)
            await self._extend_schedule_lock(job_data, ttl)

    def _start_lock_heartbeat(
        self, job_data: dict[str, Any]
    ) -> asyncio.Task[None] | None:
        _, lock_key, lock_token = self._extract_schedule_meta(job_data)
        ttl = self._schedule_lock_ttl(job_data)
        if lock_key is None or lock_token is None or ttl is None:
            return None
        task = asyncio.create_task(self._lock_heartbeat(job_data, ttl))

        def _on_done(done: asyncio.Task[None]) -> None:
            if not done.cancelled() and done.exception() is not None:
                logger.error(
                    "Overlap lock heartbeat failed", exc_info=done.exception()
                )

        task.add_done_callback(_on_done)
        return task

    async def _execute_job(self, job: Job, timeout: float | None) -> None:
        """Run ``job.handle()`` with its timeout, keeping any overlap lock alive meanwhile."""
        heartbeat = self._start_lock_heartbeat(job.data)
        try:
            if timeout is not None and timeout > 0:
                await asyncio.wait_for(job.handle(), timeout=timeout)
            else:
                await job.handle()
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _close_schedule_stores(self) -> None:
        stores = list(self._owned_schedule_stores.values())
        self._owned_schedule_stores.clear()
        for store in stores:
            try:
                await store.close()
            except Exception:
                logger.warning("Error closing owned schedule store", exc_info=True)

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
            raw_job_data = data.get("data")
            if isinstance(raw_job_data, dict):
                await self._mark_schedule_failed(raw_job_data, str(exc))
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
        if await self._mark_schedule_running(job.data):
            logger.warning(
                "Worker %s skipping duplicate delivery of job %s: scheduled occurrence already completed",
                self.worker_id,
                job.job_id,
            )
            await self.dispatcher.ack(queue, self.worker_id, raw_payload)
            return True

        try:
            await self._execute_job(job, effective_timeout)
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
            await self._mark_schedule_completed(job.data)
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
            lock_ttl = self._schedule_lock_ttl(job.data)
            if lock_ttl is not None:
                # Keep the overlap lock alive across the backoff delay until the retry re-arms it.
                await self._extend_schedule_lock(job.data, lock_ttl + delay)
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
            await self._mark_schedule_failed(job.data, str(exc))
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
            await self._close_schedule_stores()
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
