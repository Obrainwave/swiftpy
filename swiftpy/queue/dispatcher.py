from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
import uuid
from typing import Any, ClassVar, Self

import redis.asyncio as aioredis
from redis.exceptions import NoScriptError, RedisError

from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import get_container
from swiftpy.queue.job import Job, set_dispatch_handler
from swiftpy.support.keys import validate_signing_key
from swiftpy.support.redis_config import resolve_redis_params

logger = logging.getLogger("swiftpy.queue")

_POP_AND_LEASE_LUA = """
local function sanitize_utf8(s)
    local n = #s
    local has_non_ascii = false
    for k = 1, n do
        if string.byte(s, k) >= 128 then
            has_non_ascii = true
            break
        end
    end
    if not has_non_ascii then
        return s
    end
    local i = 1
    local out = nil
    local last = 1
    local repl = string.char(239, 191, 189)
    while i <= n do
        local b1 = string.byte(s, i)
        local seq_len = 0
        if b1 < 128 then
            seq_len = 1
        elseif b1 >= 194 and b1 <= 223 and i + 1 <= n then
            local b2 = string.byte(s, i + 1)
            if b2 >= 128 and b2 <= 191 then
                seq_len = 2
            end
        elseif b1 >= 224 and b1 <= 239 and i + 2 <= n then
            local b2, b3 = string.byte(s, i + 1, i + 2)
            if b2 >= 128 and b2 <= 191 and b3 >= 128 and b3 <= 191 then
                if not (b1 == 224 and b2 < 160) and not (b1 == 237 and b2 > 159) then
                    seq_len = 3
                end
            end
        elseif b1 >= 240 and b1 <= 244 and i + 3 <= n then
            local b2, b3, b4 = string.byte(s, i + 1, i + 3)
            if b2 >= 128 and b2 <= 191 and b3 >= 128 and b3 <= 191 and b4 >= 128 and b4 <= 191 then
                if not (b1 == 240 and b2 < 144) and not (b1 == 244 and b2 > 143) then
                    seq_len = 4
                end
            end
        end
        if seq_len > 0 then
            i = i + seq_len
        else
            if not out then
                out = {}
            end
            if i > last then
                out[#out + 1] = string.sub(s, last, i - 1)
            end
            out[#out + 1] = repl
            i = i + 1
            last = i
        end
    end
    if not out then
        return s
    end
    if last <= n then
        out[#out + 1] = string.sub(s, last)
    end
    return table.concat(out)
end

local job = redis.call('lpop', KEYS[1])
if job then
    job = sanitize_utf8(job)
    redis.call('rpush', KEYS[2], job)
    local member = ARGV[1] .. string.char(31) .. job
    redis.call('zadd', KEYS[3], ARGV[2], member)
    return job
end
return nil
"""

_ACK_JOB_LUA = """
local removed = redis.call('lrem', KEYS[1], 1, ARGV[1])
local zremoved = redis.call('zrem', KEYS[2], ARGV[2])
if removed > 0 or zremoved > 0 then
    return 1
end
return 0
"""

_RELEASE_JOB_LUA = """
local removed = redis.call('lrem', KEYS[1], 1, ARGV[1])
local zremoved = redis.call('zrem', KEYS[2], ARGV[2])
if removed == 0 and zremoved == 0 then
    return 0
end
local delay = tonumber(ARGV[4]) or 0
if delay > 0 then
    local score = tonumber(ARGV[5]) + delay
    redis.call('zadd', KEYS[3], score, ARGV[3])
else
    redis.call('lpush', KEYS[3], ARGV[3])
end
return 1
"""

_REAP_LEASES_LUA = """
local function update_top_attempts(raw_json, new_attempts)
    local n = #raw_json
    local i = 1
    local depth = 0
    local in_str = false
    local escaped = false
    while i <= n do
        local b = string.byte(raw_json, i)
        if in_str then
            if escaped then
                escaped = false
            elseif b == 92 then
                escaped = true
            elseif b == 34 then
                in_str = false
            end
            i = i + 1
        else
            if b == 34 then
                local j = i + 1
                local esc = false
                while j <= n do
                    local c = string.byte(raw_json, j)
                    if esc then
                        esc = false
                    elseif c == 92 then
                        esc = true
                    elseif c == 34 then
                        break
                    end
                    j = j + 1
                end
                local key_name = string.sub(raw_json, i + 1, j - 1)
                local k = j + 1
                while k <= n do
                    local ws = string.byte(raw_json, k)
                    if ws == 32 or ws == 9 or ws == 10 or ws == 13 then
                        k = k + 1
                    else
                        break
                    end
                end
                if depth == 1 and key_name == 'attempts' and string.byte(raw_json, k) == 58 then
                    local v_start = k + 1
                    while v_start <= n do
                        local ws = string.byte(raw_json, v_start)
                        if ws == 32 or ws == 9 or ws == 10 or ws == 13 then
                            v_start = v_start + 1
                        else
                            break
                        end
                    end
                    local v_end = v_start
                    while v_end <= n do
                        local vc = string.byte(raw_json, v_end)
                        if vc == 44 or vc == 125 or vc == 32 or vc == 9 or vc == 10 or vc == 13 then
                            break
                        end
                        v_end = v_end + 1
                    end
                    return string.sub(raw_json, 1, v_start - 1) .. tostring(new_attempts) .. string.sub(raw_json, v_end)
                end
                i = j + 1
            else
                if b == 123 or b == 91 then
                    depth = depth + 1
                elseif b == 125 or b == 93 then
                    depth = depth - 1
                end
                i = i + 1
            end
        end
    end
    local rev = string.reverse(raw_json)
    local pos = string.find(rev, '}')
    if pos then
        local close_idx = n - pos + 1
        local inner = string.sub(raw_json, 1, close_idx - 1)
        if string.match(inner, '^{%s*$') then
            return inner .. '"attempts":' .. tostring(new_attempts) .. string.sub(raw_json, close_idx)
        end
        return inner .. ',"attempts":' .. tostring(new_attempts) .. string.sub(raw_json, close_idx)
    end
    return raw_json
end

local expired = redis.call('zrangebyscore', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, tonumber(ARGV[2]) or 50)
local max_failed = tonumber(ARGV[4]) or 0
local count = 0
for i, member in ipairs(expired) do
    redis.call('zrem', KEYS[1], member)
    local sep = string.find(member, string.char(31))
    if sep then
        local worker_id = string.sub(member, 1, sep - 1)
        local raw_payload = string.sub(member, sep + 1)
        local proc_key = ARGV[3] .. worker_id
        redis.call('lrem', proc_key, 1, raw_payload)

        local ok, data = pcall(cjson.decode, raw_payload)
        if ok and type(data) == 'table' then
            local attempts = (tonumber(data['attempts']) or 0) + 1
            local tries = tonumber(data['tries']) or 3
            local updated_raw = update_top_attempts(raw_payload, attempts)
            if attempts >= tries then
                local record_json = '{"job_id":' .. cjson.encode(tostring(data['job_id'] or 'unknown'))
                    .. ',"job_class":' .. cjson.encode(tostring(data['job_class'] or 'unknown'))
                    .. ',"queue":' .. cjson.encode(tostring(data['queue'] or 'default'))
                    .. ',"payload":' .. updated_raw
                    .. ',"exception":{"type":"LeaseExpiredError","message":'
                    .. cjson.encode('Job lease expired and max retry attempts (' .. tries .. ') reached')
                    .. ',"traceback":""},"failed_at":' .. ARGV[1]
                    .. ',"attempts":' .. attempts
                    .. ',"tries":' .. tries .. '}'
                redis.call('rpush', KEYS[3], record_json)
                if max_failed > 0 then
                    redis.call('ltrim', KEYS[3], -max_failed, -1)
                end
            else
                redis.call('lpush', KEYS[2], updated_raw)
            end
        else
            redis.call('lpush', KEYS[2], raw_payload)
        end
        count = count + 1
    end
end
return count
"""


def _default_queue_prefix() -> str:
    try:
        cfg = Config.get_instance()
        val = cfg.get("queue.connections.redis.prefix") or cfg.get("queue.prefix")
        if val and isinstance(val, str):
            return val
    except ConfigurationError:
        logger.debug("Falling back to default queue prefix", exc_info=True)
    return "swiftpy_queue:"


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
        database: int | None = None,
        prefix: str | None = None,
        max_connections: int | None = None,
        timeout: float = 30.0,
        failed_max_size: int = 1000,
    ) -> None:
        self._client: aioredis.Redis | None = redis_client
        self._pool: aioredis.ConnectionPool | None = connection_pool
        self._redis_url = redis_url
        self.host = host
        self.port = port
        self.password = password
        self._explicit_database = database
        self.database = database if database is not None else 0
        self.prefix = prefix if prefix is not None else _default_queue_prefix()
        self.max_connections = max_connections or 50
        self.timeout = timeout
        self.failed_max_size = max(0, int(failed_max_size))
        self._owns_client = redis_client is None
        self._owns_pool = connection_pool is None and redis_client is None
        self._client_lock = asyncio.Lock()
        self._script_shas: dict[str, str] = {}

    @classmethod
    def get_instance(cls) -> QueueDispatcher:
        """Retrieve the active application's QueueDispatcher from the container, or fallback instance."""
        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
            inst = cls()
            container.instance(cls, inst)
            return inst
        except RuntimeError:
            pass

        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, dispatcher: QueueDispatcher | None) -> None:
        """Override or reset the QueueDispatcher instance in the active container and fallback."""
        cls._instance = dispatcher
        try:
            container = get_container()
            if container.has(cls):
                if dispatcher is not None:
                    container.instance(cls, dispatcher)
                else:
                    container.singleton(cls, lambda _: cls())
        except RuntimeError:
            pass

    async def get_client(self) -> aioredis.Redis:
        """Lazily initialize and return the async Redis client."""
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            if self._redis_url:
                self._client = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    encoding_errors="replace",
                )
            else:
                if self._pool is None:
                    params = resolve_redis_params(
                        section="queue",
                        host=self.host,
                        port=self.port,
                        password=self.password,
                        database=self._explicit_database,
                    )

                    self._pool = aioredis.BlockingConnectionPool(
                        host=params["host"],
                        port=params["port"],
                        db=params["database"],
                        password=params["password"] or None,
                        decode_responses=True,
                        encoding_errors="replace",
                        max_connections=self.max_connections,
                        timeout=self.timeout,
                    )
                self._client = aioredis.Redis(
                    connection_pool=self._pool,
                    decode_responses=True,
                    encoding_errors="replace",
                )
            self._owns_client = True
            return self._client

    def _queue_key(self, queue: str) -> str:
        return f"{self.prefix}{queue}"

    def _delayed_key(self, queue: str) -> str:
        return f"{self.prefix}{queue}:delayed"

    def _failed_key(self, queue: str) -> str:
        return f"{self.prefix}{queue}:failed"

    def _leases_key(self, queue: str) -> str:
        return f"{self.prefix}{queue}:leases"

    def _processing_key(self, queue: str, worker_id: str) -> str:
        return f"{self.prefix}{queue}:processing:{worker_id}"

    def _processing_prefix(self, queue: str) -> str:
        return f"{self.prefix}{queue}:processing:"

    async def _eval_lua(
        self,
        client: aioredis.Redis,
        script: str,
        numkeys: int,
        *keys_and_args: str | float,
    ) -> Any:
        """Execute a Lua script with cached EVALSHA and transparent NoScriptError reload."""
        sha = self._script_shas.get(script)
        if sha is None and hasattr(client, "script_load"):
            try:
                sha = await client.script_load(script)
                self._script_shas[script] = sha
            except (RedisError, ConnectionError, OSError):
                sha = None

        if sha is not None and hasattr(client, "evalsha"):
            try:
                return await client.evalsha(sha, numkeys, *keys_and_args)
            except NoScriptError:
                sha = await client.script_load(script)
                self._script_shas[script] = sha
                return await client.evalsha(sha, numkeys, *keys_and_args)

        return await client.eval(script, numkeys, *keys_and_args)

    async def pop_and_lease(
        self,
        queue: str,
        worker_id: str,
        lease_duration: float = 60.0,
    ) -> str | None:
        """
        Atomically move a job from the ready queue to this worker's processing list
        and register a lease in the leases sorted set.
        """
        client = await self.get_client()
        deadline = time.time() + float(lease_duration)
        res = await self._eval_lua(
            client,
            _POP_AND_LEASE_LUA,
            3,
            self._queue_key(queue),
            self._processing_key(queue, worker_id),
            self._leases_key(queue),
            worker_id,
            str(deadline),
        )
        if isinstance(res, (bytes, bytearray)):
            return res.decode("utf-8")
        return str(res) if res is not None else None

    async def ack(
        self,
        queue: str,
        worker_id: str,
        raw_payload: str,
    ) -> bool:
        """
        Atomically acknowledge a completed job: removes it from worker's processing list
        and deletes its lease from the leases set.
        """
        client = await self.get_client()
        member = f"{worker_id}\x1f{raw_payload}"
        res = await self._eval_lua(
            client,
            _ACK_JOB_LUA,
            2,
            self._processing_key(queue, worker_id),
            self._leases_key(queue),
            raw_payload,
            member,
        )
        return bool(res)

    async def release(
        self,
        queue: str,
        worker_id: str,
        raw_payload: str,
        delay: float = 0.0,
        new_payload: str | None = None,
    ) -> bool:
        """
        Atomically release an in-flight job: removes it from processing list and leases set,
        and re-queues it onto the ready queue (at the head) or the delayed set if delay > 0.
        """
        client = await self.get_client()
        payload_to_store = new_payload if new_payload is not None else raw_payload
        target_key = (
            self._delayed_key(queue) if delay > 0 else self._queue_key(queue)
        )
        member = f"{worker_id}\x1f{raw_payload}"
        res = await self._eval_lua(
            client,
            _RELEASE_JOB_LUA,
            3,
            self._processing_key(queue, worker_id),
            self._leases_key(queue),
            target_key,
            raw_payload,
            member,
            payload_to_store,
            str(delay),
            str(time.time()),
        )
        return bool(res)

    async def reap_expired_leases(
        self,
        queue: str,
        batch_size: int = 50,
    ) -> int:
        """
        Scan for expired leases, remove them from crashed worker processing lists,
        increment their attempt count, and either move to DLQ (if tries reached)
        or re-queue at the head of the ready queue.
        """
        client = await self.get_client()
        res = await self._eval_lua(
            client,
            _REAP_LEASES_LUA,
            3,
            self._leases_key(queue),
            self._queue_key(queue),
            self._failed_key(queue),
            str(time.time()),
            str(batch_size),
            self._processing_prefix(queue),
            str(self.failed_max_size),
        )
        return int(res) if res is not None else 0

    async def processing_size(
        self,
        queue: str = "default",
        worker_id: str | None = None,
    ) -> int:
        """Return the number of in-flight jobs being processed."""
        client = await self.get_client()
        if worker_id is not None:
            return int(
                await client.llen(self._processing_key(queue, worker_id))
            )

        total = 0
        async for key in client.scan_iter(
            match=f"{self._processing_prefix(queue)}*"
        ):
            total += int(await client.llen(key))
        return total

    async def leases_size(self, queue: str = "default") -> int:
        """Return the number of active leases recorded for the given queue."""
        client = await self.get_client()
        return int(await client.zcard(self._leases_key(queue)))

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
        failed_key = self._failed_key(target_queue)
        await client.rpush(failed_key, record_str)
        if self.failed_max_size > 0:
            await client.ltrim(failed_key, -self.failed_max_size, -1)
        logger.error(
            "Moved job %s [%s] to Dead Letter Queue (%s)",
            job_id,
            job_class,
            failed_key,
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

    async def retry_failed(
        self,
        queue: str = "default",
        job_id: str | None = None,
    ) -> int:
        """
        Requeue failed jobs from the Dead Letter Queue back into the active ready queue.
        If job_id is None or 'all', retries all failed jobs.
        """
        client = await self.get_client()
        failed_key = self._failed_key(queue)
        items = await client.lrange(failed_key, 0, -1)
        retried_count = 0

        for raw in items:
            raw_str = (
                raw.decode("utf-8", errors="replace")
                if isinstance(raw, (bytes, bytearray))
                else str(raw)
            )
            try:
                record = json.loads(raw_str)
            except (json.JSONDecodeError, TypeError, KeyError):
                logger.debug("Skipping unparseable failed job record", exc_info=True)
                continue

            rec_id = record.get("job_id")
            if job_id in (None, "all") or rec_id == job_id:
                payload = record.get("payload")
                parsed_payload: dict[str, Any] | None = None
                if isinstance(payload, dict):
                    parsed_payload = dict(payload)
                elif isinstance(payload, str):
                    try:
                        maybe_dict = json.loads(payload)
                        if isinstance(maybe_dict, dict):
                            parsed_payload = maybe_dict
                    except (json.JSONDecodeError, TypeError):
                        parsed_payload = None

                if parsed_payload is not None:
                    parsed_payload["attempts"] = 0
                    if "job_class" in parsed_payload:
                        key = Job.get_app_key()
                        if key is not None and str(key).strip():
                            validate_signing_key(str(key), purpose="APP_KEY")
                            parsed_payload["signature"] = Job._compute_signature(
                                parsed_payload, str(key)
                            )
                        elif not Job.get_allow_unsigned():
                            raise ConfigurationError(
                                "APP_KEY (>=32 bytes) is required to re-sign retried queue jobs."
                            )
                    requeue_str = json.dumps(parsed_payload, ensure_ascii=False)
                elif isinstance(payload, str):
                    requeue_str = payload
                else:
                    requeue_str = raw_str

                await client.rpush(self._queue_key(queue), requeue_str)
                await client.lrem(failed_key, 1, raw_str)
                retried_count += 1
                if job_id not in (None, "all"):
                    break

        return retried_count

    async def flush_failed(self, queue: str = "default") -> int:
        """Clear all failed records from the Dead Letter Queue for the given queue name."""
        client = await self.get_client()
        failed_key = self._failed_key(queue)
        count = int(await client.llen(failed_key))
        await client.delete(failed_key)
        return count

    async def clear(self, queue: str = "default") -> None:
        """Clear active, delayed, failed, lease, and processing queues for the given queue name."""
        client = await self.get_client()
        keys_to_delete = [
            self._queue_key(queue),
            self._delayed_key(queue),
            self._failed_key(queue),
            self._leases_key(queue),
        ]
        async for key in client.scan_iter(
            match=f"{self._processing_prefix(queue)}*"
        ):
            keys_to_delete.append(key)

        if keys_to_delete:
            await client.delete(*keys_to_delete)

    async def close(self) -> None:
        """Close the Redis client and connection pool if owned."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._owns_pool and self._pool is not None:
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
