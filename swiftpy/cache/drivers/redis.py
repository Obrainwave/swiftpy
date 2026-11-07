from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from redis.exceptions import RedisError, ResponseError

from swiftpy.cache.driver import _SENTINEL, CacheDriver
from swiftpy.support.redis_config import resolve_redis_params

logger = logging.getLogger("swiftpy.cache")

_INCR_WITH_TTL_LUA = """
local current = redis.call('incrby', KEYS[1], ARGV[1])
local ttl = tonumber(ARGV[2])
if ttl and ttl > 0 then
    local pttl = redis.call('pttl', KEYS[1])
    if pttl < 0 then
        redis.call('pexpire', KEYS[1], math.floor(ttl * 1000))
    end
end
return current
"""


class CacheSerializer(ABC):
    """Abstract interface for pluggable cache serializers."""

    @abstractmethod
    def serialize(self, value: Any) -> str:
        """Convert a Python value into a string representation for caching."""
        ...

    @abstractmethod
    def deserialize(self, raw: str) -> Any:
        """Reconstruct a Python value from its cached string representation."""
        ...


def _cache_json_default(obj: Any) -> Any:
    if isinstance(obj, (bytes, bytearray, memoryview)):
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
    if isinstance(obj, datetime):
        return {"__swiftpy_type__": "datetime", "val": obj.isoformat()}
    if isinstance(obj, date):
        return {"__swiftpy_type__": "date", "val": obj.isoformat()}
    if isinstance(obj, dt_time):
        return {"__swiftpy_type__": "time", "val": obj.isoformat()}
    if isinstance(obj, UUID):
        return {"__swiftpy_type__": "uuid", "val": str(obj)}
    if isinstance(obj, Decimal):
        return {"__swiftpy_type__": "decimal", "val": str(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        raw_dict = obj.to_dict()
        if isinstance(raw_dict, dict):
            hidden = set(getattr(obj, "__hidden__", ()) or ())
            if hidden:
                return {k: v for k, v in raw_dict.items() if k not in hidden}
            return raw_dict
        return raw_dict
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _cache_json_object_hook(dct: dict[str, Any]) -> Any:
    if "__swiftpy_type__" in dct and "val" in dct and len(dct) == 2:
        t = dct["__swiftpy_type__"]
        v = dct["val"]
        if t == "datetime":
            return datetime.fromisoformat(v)
        if t == "date":
            return date.fromisoformat(v)
        if t == "time":
            return dt_time.fromisoformat(v)
        if t == "uuid":
            return UUID(v)
        if t == "decimal":
            return Decimal(v)
    return dct


class JsonCacheSerializer(CacheSerializer):
    """Default JSON serializer supporting datetime, UUID, Decimal, and models."""

    def serialize(self, value: Any) -> str:
        return json.dumps(
            value,
            default=_cache_json_default,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def deserialize(self, raw: str) -> Any:
        return json.loads(raw, object_hook=_cache_json_object_hook)


class RedisDriver(CacheDriver):
    """
    Redis cache driver utilizing redis.asyncio connection pooling.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        connection_pool: aioredis.ConnectionPool | None = None,
        redis_url: str | None = None,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
        database: int = 0,
        prefix: str = "swiftpy_cache:",
        default_ttl: float | None = None,
        max_connections: int | None = None,
        timeout: float = 30.0,
        serializer: CacheSerializer | None = None,
    ) -> None:
        super().__init__()
        self._client = redis_client
        self._pool = connection_pool
        self._redis_url = redis_url
        self.host = host
        self.port = port
        self.password = password
        self.database = database
        self.prefix = prefix
        self.default_ttl = default_ttl
        self.serializer = (
            serializer if serializer is not None else JsonCacheSerializer()
        )
        self.max_connections = max_connections
        self.timeout = timeout
        self._owns_client = redis_client is None
        self._owns_pool = connection_pool is None and redis_client is None
        self._client_lock = asyncio.Lock()

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
                )
            else:
                if self._pool is None:
                    params = resolve_redis_params(
                        section="cache",
                        host=self.host,
                        port=self.port,
                        password=self.password,
                        database=self.database,
                    )
                    max_conns = self.max_connections or 50

                    self._pool = aioredis.BlockingConnectionPool(
                        host=params["host"],
                        port=params["port"],
                        db=params["database"],
                        password=params["password"] or None,
                        decode_responses=True,
                        max_connections=max_conns,
                        timeout=self.timeout,
                    )
                self._client = aioredis.Redis(
                    connection_pool=self._pool,
                    decode_responses=True,
                )
            self._owns_client = True
            return self._client

    def _prefixed_key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def _serialize(self, value: Any) -> str:
        return self.serializer.serialize(value)

    def _deserialize(self, raw: str | bytes | None, default: Any) -> Any:
        if raw is None:
            return default
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return self.serializer.deserialize(raw)
        except (json.JSONDecodeError, TypeError, ValueError, UnicodeDecodeError):
            return raw

    async def get(self, key: str, default: Any = None) -> Any:
        client = await self.get_client()
        raw = await client.get(self._prefixed_key(key))
        return self._deserialize(raw, default)

    async def set(
        self,
        key: str,
        value: Any,
        ttl: float | None = None,
    ) -> bool:
        client = await self.get_client()
        full_key = self._prefixed_key(key)
        effective_ttl = ttl if ttl is not None else self.default_ttl

        if effective_ttl is not None and effective_ttl <= 0:
            await client.delete(full_key)
            return True

        serialized = self._serialize(value)
        if effective_ttl is not None:
            if isinstance(effective_ttl, float) and not effective_ttl.is_integer():
                await client.set(full_key, serialized, px=int(effective_ttl * 1000))
            else:
                await client.set(full_key, serialized, ex=int(effective_ttl))
        else:
            await client.set(full_key, serialized)

        return True

    async def forget(self, key: str) -> bool:
        client = await self.get_client()
        count = await client.delete(self._prefixed_key(key))
        return bool(count > 0)

    async def has(self, key: str) -> bool:
        client = await self.get_client()
        count = await client.exists(self._prefixed_key(key))
        return bool(count > 0)

    async def flush(self) -> bool:
        client = await self.get_client()
        pattern = f"{self.prefix}*"
        batch: list[str] = []
        async for key in client.scan_iter(match=pattern, count=500):
            batch.append(key.decode("utf-8") if isinstance(key, bytes) else str(key))
            if len(batch) >= 500:
                await client.unlink(*batch)
                batch.clear()

        if batch:
            await client.unlink(*batch)
        return True

    async def add(
        self,
        key: str,
        value: Any,
        ttl: float | None = None,
    ) -> bool:
        client = await self.get_client()
        full_key = self._prefixed_key(key)
        effective_ttl = ttl if ttl is not None else self.default_ttl

        if effective_ttl is not None and effective_ttl <= 0:
            exists = await client.exists(full_key)
            return not bool(exists)

        serialized = self._serialize(value)
        if effective_ttl is not None:
            if isinstance(effective_ttl, float) and not effective_ttl.is_integer():
                res = await client.set(
                    full_key,
                    serialized,
                    px=int(effective_ttl * 1000),
                    nx=True,
                )
            else:
                res = await client.set(
                    full_key,
                    serialized,
                    ex=int(effective_ttl),
                    nx=True,
                )
        else:
            res = await client.set(full_key, serialized, nx=True)

        return bool(res)

    async def increment(
        self, key: str, value: int = 1, ttl: float | None = None
    ) -> int:
        client = await self.get_client()
        full_key = self._prefixed_key(key)
        effective_ttl = ttl if ttl is not None else self.default_ttl
        try:
            if effective_ttl is not None and effective_ttl > 0:
                res = await client.eval(
                    _INCR_WITH_TTL_LUA, 1, full_key, value, effective_ttl
                )
                return int(res)
            new_val = await client.incrby(full_key, value)
            return int(new_val)
        except ResponseError as exc:
            raise TypeError(
                f"Cannot increment non-numeric cached value: {exc}"
            ) from exc

    async def decrement(
        self, key: str, value: int = 1, ttl: float | None = None
    ) -> int:
        return await self.increment(key, -value, ttl)

    async def remember(
        self,
        key: str,
        ttl: float | None,
        callback: Callable[[], Any] | Callable[[], Awaitable[Any]],
        lock_timeout: float = 10.0,
    ) -> Any:
        """
        Get an item from Redis, or execute callback with cross-instance distributed lock protection.

        Uses Redis SET NX to coordinate cross-task and cross-instance execution so that
        concurrent requests across multiple drivers or processes only invoke callback once.
        If the lock times out, executes callback directly without caching and logs a warning.
        """
        # Fast path
        cached = await self.get(key, _SENTINEL)
        if cached is not _SENTINEL:
            return cached

        client = await self.get_client()
        lock_key = f"{self.prefix}lock:{key}"
        token = uuid.uuid4().hex
        deadline = time.monotonic() + lock_timeout

        while time.monotonic() < deadline:
            # Try to acquire distributed lock
            acquired = await client.set(
                lock_key,
                token,
                ex=int(lock_timeout) if lock_timeout >= 1 else 1,
                nx=True,
            )
            if acquired:
                try:
                    # Double-checked locking
                    cached = await self.get(key, _SENTINEL)
                    if cached is not _SENTINEL:
                        return cached

                    if inspect.iscoroutinefunction(callback):
                        result = await callback()
                    else:
                        res = callback()
                        result = await res if inspect.isawaitable(res) else res

                    await self.set(key, result, ttl)
                    return result
                finally:
                    # Safe release: release only if token matches
                    lua_release = (
                        "if redis.call('get', KEYS[1]) == ARGV[1] then "
                        "return redis.call('del', KEYS[1]) "
                        "else return 0 end"
                    )
                    try:
                        await client.eval(lua_release, 1, lock_key, token)
                    except RedisError as exc:
                        logger.debug("Failed to release lock %s: %s", lock_key, exc)

            # Wait and poll for value to be populated
            await asyncio.sleep(0.02)
            cached = await self.get(key, _SENTINEL)
            if cached is not _SENTINEL:
                return cached

        # Fallback if timeout elapsed: compute directly without caching and log warning
        logger.warning(
            "Cache lock timed out waiting for key '%s'; computing value directly without caching",
            key,
        )
        if inspect.iscoroutinefunction(callback):
            return await callback()
        res = callback()
        return await res if inspect.isawaitable(res) else res

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        if self._owns_pool and self._pool is not None:
            await self._pool.disconnect()
            self._pool = None
