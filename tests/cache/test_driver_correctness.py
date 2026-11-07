from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from swiftpy.cache.cache import Cache
from swiftpy.cache.drivers.memory import MemoryDriver
from swiftpy.cache.drivers.redis import RedisDriver
from swiftpy.core.config import Config


@pytest.fixture
def redis_host() -> str:
    return os.getenv("REDIS_HOST", "127.0.0.1")


@pytest.fixture
def redis_port() -> int:
    return int(os.getenv("REDIS_PORT", "6379"))


@pytest.mark.asyncio
async def test_remember_lock_timeout_computes_and_returns(
    redis_host: str, redis_port: int, caplog: pytest.LogCaptureFixture
) -> None:
    """When lock acquisition times out, remember executes the callback and returns the value without caching."""
    prefix = f"test_lock_to_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(host=redis_host, port=redis_port, prefix=prefix)

    try:
        client = await driver.get_client()
        lock_key = f"{prefix}lock:timeout_key"
        # Pre-acquire the lock with another client so remember times out
        await client.set(lock_key, "held_by_someone_else", ex=5)

        computed = False

        def slow_callback() -> str:
            nonlocal computed
            computed = True
            return "computed_fallback"

        with caplog.at_level(logging.WARNING, logger="swiftpy.cache"):
            val = await driver.remember(
                "timeout_key", ttl=10.0, callback=slow_callback, lock_timeout=0.1
            )

        assert val == "computed_fallback"
        assert computed is True
        assert any("timed out" in rec.message for rec in caplog.records)
        # Value should not be cached in Redis because lock timed out
        cached = await driver.get("timeout_key", None)
        assert cached is None
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_remember_locks_dict_cleanup(redis_host: str, redis_port: int) -> None:
    """Ensure _remember_locks does not leak entries after remember() calls complete."""
    driver = MemoryDriver()

    keys = [f"test_leak_key_{i}" for i in range(50)]

    async def compute_val(k: str) -> str:
        return f"val_{k}"

    tasks = [driver.remember(k, 60, lambda k=k: compute_val(k)) for k in keys]
    results = await asyncio.gather(*tasks)

    assert len(results) == 50
    # The lock dictionary must be completely cleaned up
    assert len(driver._remember_locks) == 0


@pytest.mark.asyncio
async def test_atomic_increment_with_ttl(redis_host: str, redis_port: int) -> None:
    """TTL is set on first increment and not extended on subsequent increments."""
    prefix = f"test_incr_ttl_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(host=redis_host, port=redis_port, prefix=prefix)

    try:
        client = await driver.get_client()
        key = "counter"

        val1 = await driver.increment(key, value=1, ttl=10.0)
        assert val1 == 1

        full_key = f"{prefix}{key}"
        pttl1 = await client.pttl(full_key)
        assert 0 < pttl1 <= 10000

        # Wait a small bit so TTL decrements
        await asyncio.sleep(0.05)

        # Subsequent increment should increment value without resetting TTL back to 10s
        val2 = await driver.increment(key, value=5, ttl=10.0)
        assert val2 == 6

        pttl2 = await client.pttl(full_key)
        assert pttl2 < pttl1
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_atomic_add(redis_host: str, redis_port: int) -> None:
    """Atomic add sets value only if key does not exist."""
    prefix = f"test_add_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(host=redis_host, port=redis_port, prefix=prefix)

    try:
        added1 = await driver.add("exclusive_key", "value1", ttl=60.0)
        assert added1 is True

        added2 = await driver.add("exclusive_key", "value2", ttl=60.0)
        assert added2 is False

        val = await driver.get("exclusive_key")
        assert val == "value1"
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_flush_scoped_by_prefix(redis_host: str, redis_port: int) -> None:
    """Flush scoped by prefix does not affect keys belonging to another app or prefix."""
    prefix_a = f"app_a_{uuid.uuid4().hex[:6]}:"
    prefix_b = f"app_b_{uuid.uuid4().hex[:6]}:"

    driver_a = RedisDriver(host=redis_host, port=redis_port, prefix=prefix_a)
    driver_b = RedisDriver(host=redis_host, port=redis_port, prefix=prefix_b)

    try:
        await driver_a.set("item1", "a_val")
        await driver_b.set("item1", "b_val")

        assert await driver_a.get("item1") == "a_val"
        assert await driver_b.get("item1") == "b_val"

        # Flush only app A
        await driver_a.flush()

        assert await driver_a.get("item1") is None
        assert await driver_b.get("item1") == "b_val"
    finally:
        await driver_a.close()
        await driver_b.close()


@pytest.mark.asyncio
async def test_datetime_uuid_decimal_roundtrip(
    redis_host: str, redis_port: int
) -> None:
    """Ensure datetime, UUID, and Decimal types round-trip correctly through Redis cache."""
    prefix = f"test_types_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(host=redis_host, port=redis_port, prefix=prefix)

    try:
        now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
        u = uuid.uuid4()
        d = Decimal("1234.56")

        await driver.set("my_datetime", now)
        await driver.set("my_uuid", u)
        await driver.set("my_decimal", d)

        got_dt = await driver.get("my_datetime")
        got_u = await driver.get("my_uuid")
        got_d = await driver.get("my_decimal")

        assert got_dt == now
        assert isinstance(got_dt, datetime)

        assert got_u == u
        assert isinstance(got_u, uuid.UUID)

        assert got_d == d
        assert isinstance(got_d, Decimal)
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_memory_driver_lru_eviction() -> None:
    """Ensure MemoryDriver evicts the least recently used keys when max_items is exceeded."""
    driver = MemoryDriver(max_items=3)

    await driver.set("k1", 1)
    await driver.set("k2", 2)
    await driver.set("k3", 3)

    # Access k1 to make it recently used (k2 becomes oldest)
    assert await driver.get("k1") == 1

    # Insert k4, exceeding max_items 3 -> k2 should be evicted
    await driver.set("k4", 4)

    assert await driver.get("k1") == 1
    assert await driver.get("k2") is None
    assert await driver.get("k3") == 3
    assert await driver.get("k4") == 4


@pytest.mark.asyncio
async def test_cache_shares_redis_connection_pool(
    redis_host: str, redis_port: int
) -> None:
    """Cache._resolve_driver reuses connection pools for stores sharing identical connection config."""
    cfg = Config()
    cfg.set("cache.stores.redis_one", {
        "driver": "redis",
        "host": redis_host,
        "port": redis_port,
        "max_connections": 15,
        "timeout": 20.0,
    })
    cfg.set("cache.stores.redis_two", {
        "driver": "redis",
        "host": redis_host,
        "port": redis_port,
        "max_connections": 15,
        "timeout": 20.0,
    })

    cache = Cache(config=cfg)
    try:
        driver1 = cache.store("redis_one")
        driver2 = cache.store("redis_two")

        assert isinstance(driver1, RedisDriver)
        assert isinstance(driver2, RedisDriver)

        # Connection pool must be identical object
        assert driver1._pool is not None
        assert driver1._pool is driver2._pool
    finally:
        await cache.close()
