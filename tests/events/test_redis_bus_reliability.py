from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any
from unittest.mock import patch

import pytest
import redis.asyncio as aioredis

from swiftpy.cache.drivers.redis import RedisDriver
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.events.redis_bus import RedisEventBus
from swiftpy.queue.dispatcher import QueueDispatcher


@pytest.fixture
def redis_host() -> str:
    return os.getenv("REDIS_HOST", "127.0.0.1")


@pytest.fixture
def redis_port() -> int:
    return int(os.getenv("REDIS_PORT", "6379"))


@pytest.mark.asyncio
async def test_shared_pool_not_closed_by_components(
    redis_host: str, redis_port: int
) -> None:
    """Ensure that components with owns_pool=False do NOT close shared external pools."""
    pool = aioredis.BlockingConnectionPool(
        host=redis_host,
        port=redis_port,
        decode_responses=True,
        max_connections=10,
    )

    # 1. QueueDispatcher with external pool
    dispatcher = QueueDispatcher(connection_pool=pool)
    client_d = await dispatcher.get_client()
    await client_d.ping()
    await dispatcher.close()

    # Pool should still be open
    test_client = aioredis.Redis(connection_pool=pool, decode_responses=True)
    assert await test_client.ping()

    # 2. RedisDriver with external pool
    driver = RedisDriver(connection_pool=pool)
    client_c = await driver.get_client()
    await client_c.ping()
    await driver.close()

    # Pool should still be open
    assert await test_client.ping()

    # 3. RedisEventBus with external pool
    bus = RedisEventBus(connection_pool=pool)
    client_b = await bus.get_client()
    await client_b.ping()
    await bus.close()

    # Pool should still be open
    assert await test_client.ping()
    await test_client.aclose()
    await pool.disconnect()


@pytest.mark.asyncio
async def test_redis_bus_hmac_signing(
    redis_host: str, redis_port: int
) -> None:
    """Ensure HMAC-SHA256 signs bus messages, drops unsigned/tampered ones, and handles key rotation."""
    key = "secret_key_1"
    RedisEventBus.set_app_key(key)
    RedisEventBus.set_previous_keys(["old_key_rot"])

    prefix = f"test_hmac_{int(time.time() * 1000)}:"
    bus = RedisEventBus(
        host=redis_host,
        port=redis_port,
        channel_prefix=prefix,
        ignore_self=False,
    )

    received: list[dict[str, Any]] = []

    def on_event(payload: Any) -> None:
        received.append(payload)

    bus.subscribe("signed.test", on_event)

    async with bus:
        client = await bus.get_client()
        channel = f"{prefix}signed.test"

        # 1. Publish valid message via bus (auto-signed)
        await bus.publish("signed.test", {"msg": "valid"})

        # Wait for delivery
        for _ in range(30):
            if len(received) == 1:
                break
            await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0] == {"msg": "valid"}

        # 2. Publish unsigned raw message directly to Redis
        unsigned_envelope = {
            "event": "signed.test",
            "payload": {"msg": "unsigned"},
            "sender_id": "other",
            "timestamp": time.time(),
        }
        await client.publish(channel, json.dumps(unsigned_envelope))
        await asyncio.sleep(0.2)
        # Should be dropped
        assert len(received) == 1

        # 3. Publish tampered message
        tampered_envelope = dict(unsigned_envelope)
        tampered_envelope["signature"] = "invalid_fake_signature"
        await client.publish(channel, json.dumps(tampered_envelope))
        await asyncio.sleep(0.2)
        # Should be dropped
        assert len(received) == 1

        # 4. Publish message signed with previous key (key rotation)
        rot_envelope = {
            "event": "signed.test",
            "payload": {"msg": "rotated"},
            "sender_id": "other",
            "timestamp": time.time(),
        }
        rot_sig = RedisEventBus._compute_signature(rot_envelope, "old_key_rot")
        rot_envelope["signature"] = rot_sig
        await client.publish(channel, json.dumps(rot_envelope))

        for _ in range(30):
            if len(received) == 2:
                break
            await asyncio.sleep(0.05)
        assert len(received) == 2
        assert received[1] == {"msg": "rotated"}

    RedisEventBus.set_app_key(None)
    RedisEventBus.set_previous_keys([])


@pytest.mark.asyncio
async def test_redis_bus_slow_listener_does_not_block_next_message(
    redis_host: str, redis_port: int
) -> None:
    """Ensure each message is dispatched concurrently so slow listeners do not delay consumption."""
    prefix = f"test_concurrency_{int(time.time() * 1000)}:"
    bus = RedisEventBus(
        host=redis_host,
        port=redis_port,
        channel_prefix=prefix,
        ignore_self=False,
    )

    completed_order: list[str] = []
    slow_started = asyncio.Event()

    async def slow_handler(payload: Any) -> None:
        slow_started.set()
        await asyncio.sleep(0.3)
        completed_order.append("slow")

    def fast_handler(payload: Any) -> None:
        completed_order.append("fast")

    bus.subscribe("test.slow", slow_handler)
    bus.subscribe("test.fast", fast_handler)

    async with bus:
        await bus.publish("test.slow", {"n": 1})
        await asyncio.wait_for(slow_started.wait(), timeout=5.0)

        # Publish fast event while slow handler is still sleeping
        await bus.publish("test.fast", {"n": 2})

        # Fast handler should complete before slow handler finishes its 0.3s sleep
        for _ in range(20):
            if "fast" in completed_order:
                break
            await asyncio.sleep(0.02)

        assert "fast" in completed_order
        assert completed_order[0] == "fast"

        # Wait for slow to finish
        for _ in range(40):
            if len(completed_order) == 2:
                break
            await asyncio.sleep(0.02)
        assert completed_order == ["fast", "slow"]


@pytest.mark.asyncio
async def test_redis_bus_listener_task_crashed_logged(
    redis_host: str, redis_port: int, caplog: pytest.LogCaptureFixture
) -> None:
    """Ensure listener task crash invokes done callback and logs exception."""
    bus = RedisEventBus(host=redis_host, port=redis_port)

    with caplog.at_level(logging.ERROR, logger="swiftpy.events"):
        async def failing_loop(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Fatal listener loop crash")

        with patch.object(bus, "_listener_loop", failing_loop):
            task = await bus.start_listening()
            try:
                await task
            except RuntimeError:
                pass
            await asyncio.sleep(0.05)

        assert any("crashed" in record.message for record in caplog.records)
        await bus.close()


def test_event_dispatcher_caching_and_removal() -> None:
    """Test EventDispatcher signature caching, listener removal, forget, and indexing."""
    dispatcher = EventDispatcher()

    call_counts: dict[str, int] = {"h1": 0, "h2": 0, "wild": 0}

    def handler_one(payload: Any) -> None:
        call_counts["h1"] += 1

    def handler_two(event: str, payload: Any) -> None:
        call_counts["h2"] += 1

    def handler_wild(payload: Any) -> None:
        call_counts["wild"] += 1

    dispatcher.listen("user.created", handler_one)
    dispatcher.listen("user.created", handler_two)
    dispatcher.listen("user.*", handler_wild)

    # Verify indexing and listeners
    listeners = dispatcher.get_listeners("user.created")
    assert len(listeners) == 3

    # Dispatch
    asyncio.run(dispatcher.dispatch("user.created", {"id": 1}))
    assert call_counts["h1"] == 1
    assert call_counts["h2"] == 1
    assert call_counts["wild"] == 1

    # Remove specific listener using remove_listener
    removed = dispatcher.remove_listener("user.created", handler_one)
    assert removed is True
    assert len(dispatcher.get_listeners("user.created")) == 2

    # Dispatch again
    asyncio.run(dispatcher.dispatch("user.created", {"id": 2}))
    assert call_counts["h1"] == 1  # unchanged
    assert call_counts["h2"] == 2
    assert call_counts["wild"] == 2

    # Forget specific event
    dispatcher.forget("user.created")
    assert len(dispatcher.get_listeners("user.created")) == 1  # only wildcard remains

    # Forget all
    dispatcher.forget()
    assert len(dispatcher.get_listeners("user.created")) == 0
