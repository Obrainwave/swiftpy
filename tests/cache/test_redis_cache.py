from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest

from swiftpy.cache.drivers.redis import RedisDriver


@pytest.fixture
async def redis_cache() -> AsyncGenerator[RedisDriver, None]:
    prefix = f"test_redis_{uuid.uuid4().hex[:8]}:"
    driver = RedisDriver(prefix=prefix, default_ttl=3600)
    try:
        yield driver
    finally:
        await driver.flush()
        await driver.close()


async def test_redis_cache_get_set(redis_cache: RedisDriver) -> None:
    assert await redis_cache.get("missing") is None
    assert await redis_cache.get("missing", default="default_val") == "default_val"

    assert await redis_cache.set("foo", "bar") is True
    assert await redis_cache.get("foo") == "bar"

    # Overwrite value
    assert await redis_cache.set("foo", "baz") is True
    assert await redis_cache.get("foo") == "baz"


async def test_redis_cache_data_types(redis_cache: RedisDriver) -> None:
    # Dictionary
    complex_dict = {
        "id": 1,
        "name": "SwiftPY",
        "nested": {"active": True, "scores": [95.5, 100]},
    }
    await redis_cache.set("data_dict", complex_dict)
    assert await redis_cache.get("data_dict") == complex_dict

    # List
    items = ["a", 1, True, None, {"k": "v"}]
    await redis_cache.set("data_list", items)
    assert await redis_cache.get("data_list") == items

    # Integers and Floats
    await redis_cache.set("int_val", 42)
    assert await redis_cache.get("int_val") == 42

    await redis_cache.set("float_val", 3.14159)
    assert await redis_cache.get("float_val") == 3.14159

    # Booleans
    await redis_cache.set("bool_true", True)
    assert await redis_cache.get("bool_true") is True
    await redis_cache.set("bool_false", False)
    assert await redis_cache.get("bool_false") is False

    # None / null
    await redis_cache.set("null_val", None)
    assert await redis_cache.get("null_val") is None
    assert await redis_cache.has("null_val") is True


async def test_redis_cache_forget_and_has(redis_cache: RedisDriver) -> None:
    assert await redis_cache.has("k1") is False
    await redis_cache.set("k1", "val1")
    assert await redis_cache.has("k1") is True

    # Forget existing
    assert await redis_cache.forget("k1") is True
    assert await redis_cache.has("k1") is False
    assert await redis_cache.get("k1") is None

    # Forget non-existent
    assert await redis_cache.forget("k1") is False


async def test_redis_cache_ttl_and_expiration(redis_cache: RedisDriver) -> None:
    client = await redis_cache.get_client()

    # Set key with 60 second TTL
    await redis_cache.set("ttl_key", "value", ttl=60)
    raw_key = redis_cache._prefixed_key("ttl_key")
    redis_ttl = await client.ttl(raw_key)
    assert 0 < redis_ttl <= 60

    # Set key with 100ms TTL and verify expiry
    await redis_cache.set("quick_expire", "alive", ttl=0.1)
    assert await redis_cache.has("quick_expire") is True
    assert await redis_cache.get("quick_expire") == "alive"

    await asyncio.sleep(0.15)
    assert await redis_cache.has("quick_expire") is False
    assert await redis_cache.get("quick_expire") is None


async def test_redis_cache_zero_or_negative_ttl(redis_cache: RedisDriver) -> None:
    # TTL <= 0 should delete key immediately
    await redis_cache.set("zero_key", "val", ttl=0)
    assert await redis_cache.has("zero_key") is False
    assert await redis_cache.get("zero_key") is None

    await redis_cache.set("neg_key", "val", ttl=-10)
    assert await redis_cache.has("neg_key") is False


async def test_redis_cache_prefix_isolated_flush() -> None:
    """
    Verify that flushing driver A only deletes keys matching its own prefix,
    leaving keys belonging to driver B completely intact in Redis.
    """
    prefix_a = f"scope_a_{uuid.uuid4().hex[:6]}:"
    prefix_b = f"scope_b_{uuid.uuid4().hex[:6]}:"

    driver_a = RedisDriver(prefix=prefix_a)
    driver_b = RedisDriver(prefix=prefix_b)

    try:
        await driver_a.set("item_1", "Alpha")
        await driver_b.set("item_1", "Beta")

        assert await driver_a.has("item_1") is True
        assert await driver_b.has("item_1") is True

        # Flush driver A only
        await driver_a.flush()

        # Driver A's key must be purged
        assert await driver_a.has("item_1") is False
        assert await driver_a.get("item_1") is None

        # Driver B's key must remain completely intact
        assert await driver_b.has("item_1") is True
        assert await driver_b.get("item_1") == "Beta"

    finally:
        await driver_b.flush()
        await driver_a.close()
        await driver_b.close()


async def test_redis_cache_unsupported_type_raises_type_error(
    redis_cache: RedisDriver,
) -> None:
    """
    Verify that attempting to cache non-JSON-serializable objects (like set)
    raises TypeError rather than silently coercing.
    """
    with pytest.raises(TypeError):
        await redis_cache.set("unsupported_set", {1, 2, 3})


async def test_redis_cache_malformed_json_fallback(redis_cache: RedisDriver) -> None:
    """
    Verify that if a key in Redis contains raw non-JSON text, get() returns
    the raw string gracefully without crashing.
    """
    client = await redis_cache.get_client()
    raw_key = redis_cache._prefixed_key("raw_text_key")
    await client.set(raw_key, "raw_unquoted_text_payload")

    # Driver should return the raw string
    result = await redis_cache.get("raw_text_key")
    assert result == "raw_unquoted_text_payload"


async def test_redis_cache_ttl_preservation_on_increment(
    redis_cache: RedisDriver,
) -> None:
    """
    Verify that incrementing an existing key preserves its expiration in Redis.
    """
    client = await redis_cache.get_client()
    raw_key = redis_cache._prefixed_key("ttl_counter")

    await redis_cache.set("ttl_counter", 100, ttl=120)
    initial_ttl = await client.ttl(raw_key)
    assert 0 < initial_ttl <= 120

    # Increment
    await redis_cache.increment("ttl_counter", 10)
    assert await redis_cache.get("ttl_counter") == 110

    # Verify TTL is still active and not wiped to -1 (indefinite)
    current_ttl = await client.ttl(raw_key)
    assert 0 < current_ttl <= 120


async def test_redis_cache_flush(redis_cache: RedisDriver) -> None:
    await redis_cache.set("a", 1)
    await redis_cache.set("b", 2)
    assert await redis_cache.has("a") is True
    assert await redis_cache.has("b") is True

    assert await redis_cache.flush() is True
    assert await redis_cache.has("a") is False
    assert await redis_cache.has("b") is False


async def test_redis_cache_pull(redis_cache: RedisDriver) -> None:
    await redis_cache.set("secret", "xyz")
    assert await redis_cache.pull("secret") == "xyz"
    assert await redis_cache.has("secret") is False
    assert await redis_cache.pull("secret", default="fallback") == "fallback"


async def test_redis_cache_add(redis_cache: RedisDriver) -> None:
    assert await redis_cache.add("unique_token", "first") is True
    assert await redis_cache.get("unique_token") == "first"

    # Second add should fail and retain existing value
    assert await redis_cache.add("unique_token", "second") is False
    assert await redis_cache.get("unique_token") == "first"


async def test_redis_cache_increment_decrement(redis_cache: RedisDriver) -> None:
    new_val = await redis_cache.increment("redis_counter", 10)
    assert new_val == 10
    assert await redis_cache.get("redis_counter") == 10

    assert await redis_cache.increment("redis_counter", 5) == 15
    assert await redis_cache.get("redis_counter") == 15

    assert await redis_cache.decrement("redis_counter", 7) == 8
    assert await redis_cache.get("redis_counter") == 8


async def test_redis_cache_remember(redis_cache: RedisDriver) -> None:
    call_count = 0

    def compute() -> str:
        nonlocal call_count
        call_count += 1
        return f"computed_{call_count}"

    val1 = await redis_cache.remember("remember_test", 60, compute)
    assert val1 == "computed_1"
    assert call_count == 1

    val2 = await redis_cache.remember("remember_test", 60, compute)
    assert val2 == "computed_1"
    assert call_count == 1


async def test_redis_cache_remember_async(redis_cache: RedisDriver) -> None:
    call_count = 0

    async def async_compute() -> list[str]:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return [f"item_{call_count}"]

    val1 = await redis_cache.remember("async_rem_test", 60, async_compute)
    assert val1 == ["item_1"]
    assert call_count == 1

    val2 = await redis_cache.remember("async_rem_test", 60, async_compute)
    assert val2 == ["item_1"]
    assert call_count == 1


async def test_redis_cache_idempotent_close(redis_cache: RedisDriver) -> None:
    """
    Verify that calling close() multiple times is safe and idempotent.
    """
    await redis_cache.close()
    await redis_cache.close()
