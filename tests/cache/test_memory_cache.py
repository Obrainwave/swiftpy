from __future__ import annotations

import asyncio
from typing import Any

import pytest

from swiftpy.cache.drivers.memory import MemoryDriver


@pytest.fixture
def memory_cache() -> MemoryDriver:
    return MemoryDriver(prefix="test_mem:", default_ttl=3600)


async def test_memory_cache_get_set(memory_cache: MemoryDriver) -> None:
    assert await memory_cache.get("missing") is None
    assert await memory_cache.get("missing", default="default_val") == "default_val"

    assert await memory_cache.set("foo", "bar") is True
    assert await memory_cache.get("foo") == "bar"

    # Overwrite value
    assert await memory_cache.set("foo", "baz") is True
    assert await memory_cache.get("foo") == "baz"


async def test_memory_cache_forget_and_has(memory_cache: MemoryDriver) -> None:
    assert await memory_cache.has("key1") is False
    await memory_cache.set("key1", "val1")
    assert await memory_cache.has("key1") is True

    # Forget existing key
    assert await memory_cache.forget("key1") is True
    assert await memory_cache.has("key1") is False
    assert await memory_cache.get("key1") is None

    # Forget non-existent key
    assert await memory_cache.forget("key1") is False


async def test_memory_cache_ttl_expiration() -> None:
    driver = MemoryDriver(prefix="ttl_mem:", default_ttl=None)

    # Set with short TTL (50 milliseconds)
    await driver.set("short_lived", "alive", ttl=0.05)
    assert await driver.has("short_lived") is True
    assert await driver.get("short_lived") == "alive"

    await asyncio.sleep(0.07)

    assert await driver.has("short_lived") is False
    assert await driver.get("short_lived") is None


async def test_memory_cache_controllable_clock() -> None:
    """
    Verify deterministic TTL expiration using an injected controllable clock (zero sleep).
    """
    simulated_time = 1000.0

    def fake_clock() -> float:
        return simulated_time

    driver = MemoryDriver(prefix="clock_mem:", default_ttl=60, clock=fake_clock)

    await driver.set("timed_key", "value", ttl=30)
    assert await driver.get("timed_key") == "value"

    # Advance 29 seconds: key must still be alive
    simulated_time += 29.0
    assert await driver.has("timed_key") is True
    assert await driver.get("timed_key") == "value"

    # Advance 2 more seconds (31s elapsed): key must expire
    simulated_time += 2.0
    assert await driver.has("timed_key") is False
    assert await driver.get("timed_key") is None


async def test_memory_cache_indefinite_ttl() -> None:
    """
    Verify that ttl=None with default_ttl=None persists indefinitely.
    """
    simulated_time = 5000.0

    def fake_clock() -> float:
        return simulated_time

    driver = MemoryDriver(prefix="indef_mem:", default_ttl=None, clock=fake_clock)
    await driver.set("eternal_key", "data")

    # Advance 1 year into the future
    simulated_time += 31536000.0
    assert await driver.has("eternal_key") is True
    assert await driver.get("eternal_key") == "data"


async def test_memory_cache_ttl_preservation_on_increment() -> None:
    """
    Verify that incrementing an existing key preserves its original expiration timestamp.
    """
    simulated_time = 100.0

    def fake_clock() -> float:
        return simulated_time

    driver = MemoryDriver(prefix="ttl_inc:", clock=fake_clock)
    await driver.set("counter", 10, ttl=50)

    # Advance 30 seconds and increment
    simulated_time += 30.0
    new_val = await driver.increment("counter", 5)
    assert new_val == 15
    assert await driver.get("counter") == 15

    # Advance 19 seconds (49s total from creation): still alive
    simulated_time += 19.0
    assert await driver.has("counter") is True
    assert await driver.get("counter") == 15

    # Advance 2 more seconds (51s total from creation): expired
    simulated_time += 2.0
    assert await driver.has("counter") is False
    assert await driver.get("counter") is None


async def test_memory_cache_ttl_on_overwrite() -> None:
    """
    Verify that overwriting an existing key with a new TTL updates its expiration.
    """
    simulated_time = 100.0

    def fake_clock() -> float:
        return simulated_time

    driver = MemoryDriver(prefix="overwrite_ttl:", clock=fake_clock)
    await driver.set("key", "val1", ttl=20)

    # Advance 15 seconds, then overwrite with ttl=60
    simulated_time += 15.0
    await driver.set("key", "val2", ttl=60)

    # Advance 15 more seconds (30s from start, but only 15s from overwrite)
    simulated_time += 15.0
    assert await driver.has("key") is True
    assert await driver.get("key") == "val2"


async def test_memory_cache_add_failure_does_not_modify_existing() -> None:
    """
    Verify that add() on an existing key returns False and does not alter value or TTL.
    """
    simulated_time = 100.0

    def fake_clock() -> float:
        return simulated_time

    driver = MemoryDriver(prefix="add_reject:", clock=fake_clock)
    await driver.set("key", "initial", ttl=30)

    # Attempt to add should fail
    assert await driver.add("key", "rejected", ttl=100) is False
    assert await driver.get("key") == "initial"

    # Must still expire at the original 30s boundary
    simulated_time += 31.0
    assert await driver.has("key") is False


async def test_memory_cache_prefix_isolated_flush() -> None:
    """
    Verify that flushing a driver instance only purges keys matching its configured prefix,
    leaving keys belonging to other prefixes intact in shared storage.
    """
    shared_storage: dict[str, tuple[Any, float | None]] = {}
    driver_a = MemoryDriver(prefix="store_a:")
    driver_a._storage = shared_storage
    driver_b = MemoryDriver(prefix="store_b:")
    driver_b._storage = shared_storage

    await driver_a.set("user_1", "Alice")
    await driver_b.set("user_1", "Bob")

    assert await driver_a.has("user_1") is True
    assert await driver_b.has("user_1") is True

    # Flush driver A
    await driver_a.flush()

    assert await driver_a.has("user_1") is False
    # Driver B's key must remain completely untouched
    assert await driver_b.has("user_1") is True
    assert await driver_b.get("user_1") == "Bob"


async def test_memory_cache_zero_or_negative_ttl() -> None:
    driver = MemoryDriver(prefix="zero_ttl:")

    # Setting with ttl=0 should expire/delete immediately
    await driver.set("temp", "value", ttl=0)
    assert await driver.has("temp") is False
    assert await driver.get("temp") is None

    # Setting with negative ttl should also expire immediately
    await driver.set("temp2", "value", ttl=-5)
    assert await driver.has("temp2") is False


async def test_memory_cache_deepcopy_isolation(memory_cache: MemoryDriver) -> None:
    original_dict: dict[str, Any] = {"nested": [1, 2, 3], "status": "active"}
    await memory_cache.set("dict_key", original_dict)

    # Mutate retrieved object
    retrieved = await memory_cache.get("dict_key")
    retrieved["nested"].append(4)
    retrieved["status"] = "mutated"

    # Verify cached item was isolated and unaffected
    fresh = await memory_cache.get("dict_key")
    assert fresh["nested"] == [1, 2, 3]
    assert fresh["status"] == "active"

    # Mutate original object
    original_dict["nested"].append(99)
    fresh2 = await memory_cache.get("dict_key")
    assert fresh2["nested"] == [1, 2, 3]


async def test_memory_cache_flush(memory_cache: MemoryDriver) -> None:
    await memory_cache.set("k1", "v1")
    await memory_cache.set("k2", "v2")
    assert await memory_cache.has("k1") is True
    assert await memory_cache.has("k2") is True

    assert await memory_cache.flush() is True
    assert await memory_cache.has("k1") is False
    assert await memory_cache.has("k2") is False


async def test_memory_cache_pull(memory_cache: MemoryDriver) -> None:
    await memory_cache.set("pull_key", "secret_token")

    # Pull existing
    val = await memory_cache.pull("pull_key")
    assert val == "secret_token"
    assert await memory_cache.has("pull_key") is False

    # Pull missing
    assert await memory_cache.pull("pull_key", default="fallback") == "fallback"


async def test_memory_cache_add(memory_cache: MemoryDriver) -> None:
    assert await memory_cache.add("add_key", "val1") is True
    assert await memory_cache.get("add_key") == "val1"

    # Second add should fail and retain existing value
    assert await memory_cache.add("add_key", "val2") is False
    assert await memory_cache.get("add_key") == "val1"


async def test_memory_cache_increment_decrement(memory_cache: MemoryDriver) -> None:
    # Initialize via increment
    new_val = await memory_cache.increment("counter", 5)
    assert new_val == 5
    assert await memory_cache.get("counter") == 5

    # Increment further
    assert await memory_cache.increment("counter", 3) == 8
    assert await memory_cache.get("counter") == 8

    # Decrement
    assert await memory_cache.decrement("counter", 2) == 6
    assert await memory_cache.get("counter") == 6

    # Decrement below zero
    assert await memory_cache.decrement("counter", 10) == -4
    assert await memory_cache.get("counter") == -4


async def test_memory_cache_increment_non_numeric_error(
    memory_cache: MemoryDriver,
) -> None:
    await memory_cache.set("text_key", "not_a_number")
    with pytest.raises(TypeError, match="Cannot increment non-numeric"):
        await memory_cache.increment("text_key", 1)


async def test_memory_cache_remember(memory_cache: MemoryDriver) -> None:
    call_count = 0

    def sync_compute() -> str:
        nonlocal call_count
        call_count += 1
        return f"result_{call_count}"

    # First call: executes callback
    val1 = await memory_cache.remember("remember_key", 60, sync_compute)
    assert val1 == "result_1"
    assert call_count == 1

    # Second call: returns cached value without executing callback
    val2 = await memory_cache.remember("remember_key", 60, sync_compute)
    assert val2 == "result_1"
    assert call_count == 1


async def test_memory_cache_remember_async(memory_cache: MemoryDriver) -> None:
    call_count = 0

    async def async_compute() -> dict[str, int]:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return {"count": call_count}

    val1 = await memory_cache.remember("async_rem_key", 60, async_compute)
    assert val1 == {"count": 1}
    assert call_count == 1

    val2 = await memory_cache.remember("async_rem_key", 60, async_compute)
    assert val2 == {"count": 1}
    assert call_count == 1
