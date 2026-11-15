from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from swiftpy.cache.drivers.memory import MemoryDriver
from swiftpy.cache.drivers.redis import RedisDriver


async def test_concurrent_writes_to_same_key_memory_driver() -> None:
    """
    Test: 200 simultaneous writes to the same key complete successfully,
    and the final cached value matches one complete submitted payload without corruption (MemoryDriver).
    """
    driver = MemoryDriver(prefix="stress_mem:", default_ttl=3600)
    key = "concurrent_key"
    concurrency_count = 200

    payloads: list[dict[str, Any]] = [
        {
            "worker_id": i,
            "token": uuid.uuid4().hex,
            "data": "M" * (i + 10),
            "valid": True,
        }
        for i in range(concurrency_count)
    ]

    results = await asyncio.gather(
        *(driver.set(key, payloads[i], ttl=60) for i in range(concurrency_count))
    )

    assert len(results) == concurrency_count
    assert all(res is True for res in results)
    assert await driver.has(key) is True

    final_value = await driver.get(key)
    assert isinstance(final_value, dict)
    assert "worker_id" in final_value
    assert "token" in final_value
    assert "data" in final_value
    assert final_value["valid"] is True

    worker_id = final_value["worker_id"]
    assert 0 <= worker_id < concurrency_count
    assert final_value["data"] == "M" * (worker_id + 10)
    assert final_value == payloads[worker_id]

    await driver.close()


async def test_concurrent_writes_to_same_key_redis_driver() -> None:
    """
    Test: 200 simultaneous writes to the same key complete successfully,
    and the final cached value matches one complete submitted payload without corruption (RedisDriver).
    """
    prefix = f"stress_redis_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(prefix=prefix, default_ttl=3600, max_connections=50)
    key = "concurrent_redis_key"
    concurrency_count = 200

    payloads: list[dict[str, Any]] = [
        {
            "worker_id": i,
            "token": uuid.uuid4().hex,
            "data": "R" * (i + 20),
            "seq": i * 10,
        }
        for i in range(concurrency_count)
    ]

    try:
        results = await asyncio.gather(
            *(driver.set(key, payloads[i], ttl=120) for i in range(concurrency_count))
        )

        assert len(results) == concurrency_count
        assert all(res is True for res in results)
        assert await driver.has(key) is True

        final_value = await driver.get(key)
        assert isinstance(final_value, dict)
        assert "worker_id" in final_value
        assert "token" in final_value
        assert "data" in final_value
        assert "seq" in final_value

        worker_id = final_value["worker_id"]
        assert 0 <= worker_id < concurrency_count
        assert final_value["data"] == "R" * (worker_id + 20)
        assert final_value["seq"] == worker_id * 10
        assert final_value == payloads[worker_id]

    finally:
        await driver.flush()
        await driver.close()


async def test_concurrent_add_only_one_wins_memory() -> None:
    """
    Verify atomic check-and-set for add(): when 200 concurrent tasks race
    to insert the same previously missing key, exactly one wins.
    """
    driver = MemoryDriver(prefix="atomic_add_mem:")
    concurrency_count = 200

    try:
        results = await asyncio.gather(
            *(driver.add("shared_add", f"value_{i}") for i in range(concurrency_count))
        )
        assert sum(results) == 1
        assert await driver.has("shared_add") is True
        value = await driver.get("shared_add")
        assert value in {f"value_{i}" for i in range(concurrency_count)}
    finally:
        await driver.close()


async def test_concurrent_add_only_one_wins_redis() -> None:
    """
    Verify atomic SET NX for add() in Redis: when 200 concurrent tasks race
    to insert the same previously missing key, exactly one wins.
    """
    prefix = f"atomic_add_red_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(prefix=prefix, default_ttl=3600, max_connections=50)
    concurrency_count = 200

    try:
        results = await asyncio.gather(
            *(
                driver.add("shared_redis_add", f"val_{i}")
                for i in range(concurrency_count)
            )
        )
        assert sum(results) == 1
        assert await driver.has("shared_redis_add") is True
        value = await driver.get("shared_redis_add")
        assert value in {f"val_{i}" for i in range(concurrency_count)}
    finally:
        await driver.flush()
        await driver.close()


async def test_concurrent_increments_memory_driver() -> None:
    """
    Verify that 200 concurrent increments on MemoryDriver are atomic and do not lose updates.
    """
    driver = MemoryDriver(prefix="atomic_inc_mem:")
    concurrency_count = 200

    try:
        results = await asyncio.gather(
            *(driver.increment("counter", 1) for _ in range(concurrency_count))
        )
        assert len(results) == concurrency_count
        # All returned values must be unique integers from 1 to 200
        assert sorted(results) == list(range(1, concurrency_count + 1))
        assert await driver.get("counter") == concurrency_count
    finally:
        await driver.close()


async def test_concurrent_increments_redis_driver() -> None:
    """
    Verify that 200 concurrent INCRBY operations on RedisDriver are atomic and do not lose updates.
    """
    prefix = f"atomic_inc_red_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(prefix=prefix, default_ttl=3600, max_connections=50)
    concurrency_count = 200

    try:
        results = await asyncio.gather(
            *(driver.increment("redis_counter", 1) for _ in range(concurrency_count))
        )
        assert len(results) == concurrency_count
        assert sorted(results) == list(range(1, concurrency_count + 1))
        assert await driver.get("redis_counter") == concurrency_count
    finally:
        await driver.flush()
        await driver.close()


async def test_concurrent_mixed_increments_decrements() -> None:
    """
    Verify concurrent mixed increments and decrements: 100 workers increment by 3 (+300),
    100 workers decrement by 1 (-100). Net final result must be exactly 200.
    """
    prefix = f"mixed_counter_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(prefix=prefix, default_ttl=3600, max_connections=50)

    try:
        inc_tasks = [driver.increment("net_counter", 3) for _ in range(100)]
        dec_tasks = [driver.decrement("net_counter", 1) for _ in range(100)]
        all_tasks = inc_tasks + dec_tasks
        # Interleave execution
        await asyncio.gather(*all_tasks)

        assert await driver.get("net_counter") == 200
    finally:
        await driver.flush()
        await driver.close()


async def test_concurrent_remember_stampede_protection() -> None:
    """
    Verify stampede protection: when 200 simultaneous tasks call remember()
    on an uncached key, the callback executes exactly once and all 200 workers
    receive the identical result without duplicate execution.
    """
    driver = MemoryDriver(prefix="stampede_mem:", default_ttl=3600)
    key = "stampede_key"
    concurrency_count = 200
    execution_count = 0

    async def expensive_computation() -> dict[str, Any]:
        nonlocal execution_count
        execution_count += 1
        await asyncio.sleep(0.02)
        return {
            "status": "computed",
            "execution": execution_count,
            "secret": "stable_hash",
        }

    results = await asyncio.gather(
        *(
            driver.remember(key, 60, expensive_computation)
            for _ in range(concurrency_count)
        )
    )

    assert execution_count == 1
    expected = {
        "status": "computed",
        "execution": 1,
        "secret": "stable_hash",
    }
    assert len(results) == concurrency_count
    for res in results:
        assert res == expected

    await driver.close()


async def test_cross_instance_redis_stampede_protection() -> None:
    """
    Verify cross-instance Redis stampede protection: Two independent RedisDriver
    instances accessing the same key prefix simultaneously through 100 tasks each (200 total).
    The callback must execute exactly once globally across both instances.
    """
    shared_prefix = f"cross_inst_{uuid.uuid4().hex[:8]}:"
    driver_a = RedisDriver(prefix=shared_prefix, default_ttl=3600, max_connections=25)
    driver_b = RedisDriver(prefix=shared_prefix, default_ttl=3600, max_connections=25)
    key = "cross_instance_key"

    execution_count = 0
    event = asyncio.Event()

    async def expensive_cross_compute() -> dict[str, Any]:
        nonlocal execution_count
        execution_count += 1
        # Set event to signal first runner entered
        event.set()
        await asyncio.sleep(0.05)
        return {"winner": "first_lock_holder", "exec": execution_count}

    try:
        # 100 workers on Driver A, 100 workers on Driver B
        tasks_a = [
            driver_a.remember(key, 60, expensive_cross_compute) for _ in range(100)
        ]
        tasks_b = [
            driver_b.remember(key, 60, expensive_cross_compute) for _ in range(100)
        ]

        all_results = await asyncio.gather(*tasks_a, *tasks_b)

        # Globally executed exactly once
        assert execution_count == 1
        expected = {"winner": "first_lock_holder", "exec": 1}
        assert len(all_results) == 200
        for res in all_results:
            assert res == expected

    finally:
        await driver_a.flush()
        await driver_a.close()
        await driver_b.close()


async def test_remember_callback_exception_releases_lock_and_retries() -> None:
    """
    Verify that if a remember() callback raises an exception, the distributed
    lock is cleanly released and subsequent calls can recompute without deadlocking.
    """
    prefix = f"rem_exc_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(prefix=prefix, default_ttl=3600, max_connections=10)
    key = "failing_key"
    attempts = 0

    async def faulty_callback() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Temporary upstream database timeout")
        return "success_on_retry"

    try:
        # First call fails
        with pytest.raises(RuntimeError, match="Temporary upstream database timeout"):
            await driver.remember(key, 60, faulty_callback)

        assert attempts == 1
        assert await driver.has(key) is False

        # Second call immediately succeeds without hanging on orphaned lock
        retry_val = await driver.remember(key, 60, faulty_callback)
        assert retry_val == "success_on_retry"
        assert attempts == 2
        assert await driver.get(key) == "success_on_retry"

    finally:
        await driver.flush()
        await driver.close()


async def test_remember_callback_caches_none_result() -> None:
    """
    Verify that if a remember() callback intentionally returns None,
    None is cached and returned on subsequent calls without re-executing callback.
    """
    prefix = f"rem_none_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(prefix=prefix, default_ttl=3600, max_connections=10)
    key = "none_key"
    call_count = 0

    def returns_none() -> None:
        nonlocal call_count
        call_count += 1

    try:
        val1 = await driver.remember(key, 60, returns_none)
        assert val1 is None
        assert call_count == 1
        assert await driver.has(key) is True

        val2 = await driver.remember(key, 60, returns_none)
        assert val2 is None
        assert call_count == 1

    finally:
        await driver.flush()
        await driver.close()


async def test_concurrent_writes_to_distinct_keys_memory() -> None:
    """
    Verify that 200 concurrent tasks writing to distinct keys in MemoryDriver succeed without collision.
    """
    driver = MemoryDriver(prefix="distinct_mem:", default_ttl=3600)
    concurrency_count = 200

    try:
        set_results = await asyncio.gather(
            *(
                driver.set(f"distinct_key_{i}", f"val_{i}", ttl=60)
                for i in range(concurrency_count)
            )
        )
        assert all(res is True for res in set_results)

        get_results = await asyncio.gather(
            *(driver.get(f"distinct_key_{i}") for i in range(concurrency_count))
        )
        for i in range(concurrency_count):
            assert get_results[i] == f"val_{i}"

    finally:
        await driver.close()


async def test_concurrent_writes_to_distinct_keys_redis() -> None:
    """
    Verify that 200 concurrent tasks writing to distinct keys in RedisDriver succeed without collision.
    """
    prefix = f"distinct_red_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(prefix=prefix, default_ttl=3600, max_connections=50)
    concurrency_count = 200

    try:
        set_results = await asyncio.gather(
            *(
                driver.set(f"distinct_key_{i}", f"val_{i}", ttl=60)
                for i in range(concurrency_count)
            )
        )
        assert all(res is True for res in set_results)

        get_results = await asyncio.gather(
            *(driver.get(f"distinct_key_{i}") for i in range(concurrency_count))
        )
        for i in range(concurrency_count):
            assert get_results[i] == f"val_{i}"

    finally:
        await driver.flush()
        await driver.close()
