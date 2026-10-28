from __future__ import annotations

import json
import logging
import os
import time
import traceback
import uuid
from typing import Any, ClassVar, Self

import redis.asyncio as aioredis

from swiftpy.queue.job import Job, set_dispatch_handler
from swiftpy.support.redis_config import resolve_redis_params

logger = logging.getLogger("swiftpy.queue")


class QueueDispatcher:
    """
    Queue dispatcher responsible for pushing jobs onto Redis-backed queues,
    managing delayed job scheduling, and handling dead letter queues.
    """

    _instance: ClassVar[QueueDispatcher | None] = None

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        connection_pool: aioredis.ConnectionPool | None = None,
        redis_url: str | None = None,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
        database: int = 0,
        prefix: str | None = None,
        max_connections: int | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client: aioredis.Redis | None = redis_client
        self._pool: aioredis.ConnectionPool | None = connection_pool
        self._redis_url = redis_url
        self.host = host
        self.port = port
        self.password = password
        self.database = database
        self.prefix = (
            prefix
            if prefix is not None
            else os.getenv("QUEUE_PREFIX", "swiftpy_queue:")
        )
        self.max_connections = max_connections or 50
        self.timeout = timeout
        self._owns_client = redis_client is None

    @classmethod
    def get_instance(cls) -> QueueDispatcher:
        """Retrieve the global or fallback QueueDispatcher instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, dispatcher: QueueDispatcher | None) -> None:
        """Override or reset the global QueueDispatcher instance."""
        cls._instance = dispatcher

    async def get_client(self) -> aioredis.Redis:
        """Lazily initialize and return the async Redis client."""
        if self._client is None:
            if self._redis_url:
                self._client = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                )
            else:
                if self._pool is None:
                    params = resolve_redis_params(
                        section="queue",
                        host=self.host,
                        port=self.port,
                        password=self.password,
                        database=self.database if self.database != 0 else None,
                    )

                    self._pool = aioredis.BlockingConnectionPool(
                        host=params["host"],
                        port=params["port"],
                        db=params["database"],
                        password=params["password"] or None,
                        decode_responses=True,
                        max_connections=self.max_connections,
                        timeout=self.timeout,
                    )
                self._client = aioredis.Redis(
                    connection_pool=self._pool,
                    decode_responses=True,
                )
            self._owns_client = True
        return self._client

    def _queue_key(self, queue: str) -> str:
        return f"{self.prefix}{queue}"

    def _delayed_key(self, queue: str) -> str:
        return f"{self.prefix}{queue}:delayed"

    def _failed_key(self, queue: str) -> str:
        return f"{self.prefix}{queue}:failed"

    async def push(
        self,
        job: Job,
        queue: str | None = None,
        delay: float = 0,
    ) -> str:
        """
        Serialize a Job instance and push it onto the Redis queue.
        If delay > 0, schedules it in the delayed sorted set.
        """
        client = await self.get_client()
        target_queue = queue or job.queue or "default"
        job.queue = target_queue
        job.pushed_at = time.time()

        payload_dict = job.serialize()
        payload_str = json.dumps(payload_dict, ensure_ascii=False)

        if delay > 0:
            score = time.time() + float(delay)
            await client.zadd(self._delayed_key(target_queue), {payload_str: score})
            logger.debug(
                "Scheduled job %s on %s with delay %ss",
                job.job_id,
                target_queue,
                delay,
            )
        else:
            await client.rpush(self._queue_key(target_queue), payload_str)
            logger.debug("Pushed job %s to %s", job.job_id, target_queue)

        return job.job_id

    async def push_raw(
        self,
        queue: str,
        raw_payload: str,
        delay: float = 0,
    ) -> None:
        """
        Push a raw JSON string envelope onto the target queue or delayed set.
        """
        client = await self.get_client()
        if delay > 0:
            score = time.time() + float(delay)
            await client.zadd(self._delayed_key(queue), {raw_payload: score})
        else:
            await client.rpush(self._queue_key(queue), raw_payload)

    async def push_failed(
        self,
        job: Job | None,
        exception: BaseException,
        queue: str = "default",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """
        Push a failed job record to the Dead Letter Queue (DLQ).
        """
        client = await self.get_client()
        target_queue = queue or (job.queue if job else "default")

        job_id = (
            job.job_id
            if job
            else (payload.get("job_id") if payload else uuid.uuid4().hex)
        )
        job_class = (
            f"{job.__class__.__module__}:{job.__class__.__qualname__}"
            if job
            else (payload.get("job_class") if payload else "unknown")
        )

        record: dict[str, Any] = {
            "job_id": job_id,
            "job_class": job_class,
            "queue": target_queue,
            "payload": payload
            if payload is not None
            else (job.serialize() if job else {}),
            "exception": {
                "type": type(exception).__name__,
                "message": str(exception),
                "traceback": traceback.format_exc(),
            },
            "failed_at": time.time(),
            "attempts": job.attempts
            if job
            else (payload.get("attempts", 0) if payload else 0),
            "tries": job.tries if job else (payload.get("tries", 0) if payload else 0),
        }

        record_str = json.dumps(record, ensure_ascii=False)
        await client.rpush(self._failed_key(target_queue), record_str)
        logger.error(
            "Moved job %s [%s] to Dead Letter Queue (%s)",
            job_id,
            job_class,
            self._failed_key(target_queue),
        )

    async def size(self, queue: str = "default") -> int:
        """Return the number of pending jobs in the active queue."""
        client = await self.get_client()
        return int(await client.llen(self._queue_key(queue)))

    async def delayed_size(self, queue: str = "default") -> int:
        """Return the number of scheduled jobs in the delayed set."""
        client = await self.get_client()
        return int(await client.zcard(self._delayed_key(queue)))

    async def failed_size(self, queue: str = "default") -> int:
        """Return the number of failed jobs in the Dead Letter Queue."""
        client = await self.get_client()
        return int(await client.llen(self._failed_key(queue)))

    async def get_failed_jobs(
        self,
        queue: str = "default",
        start: int = 0,
        stop: int = -1,
    ) -> list[dict[str, Any]]:
        """Retrieve recorded jobs from the Dead Letter Queue."""
        client = await self.get_client()
        items = await client.lrange(self._failed_key(queue), start, stop)
        results: list[dict[str, Any]] = []
        for raw in items:
            try:
                results.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError, KeyError):
                results.append({"raw": raw})
        return results

    async def clear(self, queue: str = "default") -> None:
        """Clear active, delayed, and failed queues for the given queue name."""
        client = await self.get_client()
        await client.delete(
            self._queue_key(queue),
            self._delayed_key(queue),
            self._failed_key(queue),
        )

    async def close(self) -> None:
        """Close the Redis client and connection pool if owned."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None

    async def aclose(self) -> None:
        """Alias for close()."""
        await self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


async def push(
    job: Job,
    queue: str | None = None,
    delay: float = 0,
) -> str:
    """
    Push a Job onto the default QueueDispatcher.
    """
    return await QueueDispatcher.get_instance().push(job, queue=queue, delay=delay)


# Wire the Job.dispatch() hook to delegate to QueueDispatcher.get_instance().push
set_dispatch_handler(
    lambda j, q, d: QueueDispatcher.get_instance().push(j, queue=q, delay=d)
)
