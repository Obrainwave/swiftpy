from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError, ResponseError

from swiftpy.cache.driver import _SENTINEL, CacheDriver
from swiftpy.support.redis_config import resolve_redis_params

logger = logging.getLogger("swiftpy.cache")


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
        self.max_connections = max_connections
        self.timeout = timeout
        self._owns_client = redis_client is None

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
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _deserialize(self, raw: str | bytes | None, default: Any) -> Any:
        if raw is None:
            return default
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
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
        cursor = 0
        keys_to_delete: list[str] = []

        while True:
            cursor, keys = await client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                keys_to_delete.extend(
                    k.decode("utf-8") if isinstance(k, bytes) else str(k) for k in keys
                )
            if cursor == 0:
                break

        if keys_to_delete:
            await client.delete(*keys_to_delete)
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

    async def increment(self, key: str, value: int = 1) -> int:
        client = await self.get_client()
        full_key = self._prefixed_key(key)
        try:
            new_val = await client.incrby(full_key, value)
            return int(new_val)
        except ResponseError as exc:
            raise TypeError(
                f"Cannot increment non-numeric cached value: {exc}"
            ) from exc

    async def decrement(self, key: str, value: int = 1) -> int:
        return await self.increment(key, -value)

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
        """
        # Fast path
        cached = await self.get(key, _SENTINEL)
        if cached is not _SENTINEL:
            return cached

        client = await self.get_client()
        lock_key = f"{self.prefix}__lock__:{key}"
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

        # Fallback if timeout elapsed
        return await self.get(key, None)

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        if self._pool is not None:
            await self._pool.disconnect()
            self._pool = None
