from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
import redis.asyncio as aioredis

from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.events.redis_bus import RedisEventBus


@pytest.fixture
def redis_host() -> str:
    return os.getenv("REDIS_HOST", "127.0.0.1")


@pytest.fixture
def redis_port() -> int:
    return int(os.getenv("REDIS_PORT", "6379"))


async def test_redis_bus_dual_instance_delivery(
    redis_host: str, redis_port: int
) -> None:
    """
    Test: Two separate RedisEventBus instances communicate via Redis pub/sub.
    Bus A publishes an event and Bus B receives it.
    """
    prefix = f"test_dual_bus_{uuid.uuid4().hex[:8]}:"

    bus_a = RedisEventBus(host=redis_host, port=redis_port, channel_prefix=prefix)
    bus_b = RedisEventBus(host=redis_host, port=redis_port, channel_prefix=prefix)

    received_b: list[Any] = []
    received_event_b = asyncio.Event()

    def on_event_b(payload: Any) -> None:
        received_b.append(payload)
        received_event_b.set()

    bus_b.subscribe("invoice.paid", on_event_b)

    async with bus_a, bus_b:
        payload = {"invoice_id": 1001, "amount": 250.75, "currency": "USD"}
        subs_count = await bus_a.publish("invoice.paid", payload)

        assert subs_count >= 1
        await asyncio.wait_for(received_event_b.wait(), timeout=5.0)

        assert len(received_b) == 1
        assert received_b[0] == payload


async def test_redis_bus_ignore_self(redis_host: str, redis_port: int) -> None:
    """
    Test: RedisEventBus ignores messages published by itself when ignore_self=True,
    preventing duplicate execution in the publisher worker.
    """
    prefix = f"test_ignore_self_{uuid.uuid4().hex[:8]}:"

    bus_publisher = RedisEventBus(
        host=redis_host,
        port=redis_port,
        channel_prefix=prefix,
        ignore_self=True,
    )
    bus_worker = RedisEventBus(
        host=redis_host,
        port=redis_port,
        channel_prefix=prefix,
        ignore_self=True,
    )

    publisher_calls: list[Any] = []
    worker_calls: list[Any] = []
    worker_event = asyncio.Event()

    def on_publisher_call(p: Any) -> None:
        publisher_calls.append(p)

    def on_worker_call(p: Any) -> None:
        worker_calls.append(p)
        worker_event.set()

    bus_publisher.subscribe("task.dispatched", on_publisher_call)
    bus_worker.subscribe("task.dispatched", on_worker_call)

    async with bus_publisher, bus_worker:
        await bus_publisher.publish("task.dispatched", {"task": "cleanup"})
        await asyncio.wait_for(worker_event.wait(), timeout=5.0)

        # Worker received the event from publisher
        assert len(worker_calls) == 1
        assert worker_calls[0] == {"task": "cleanup"}

        # Publisher did NOT invoke its own handler via pub/sub because ignore_self=True
        await asyncio.sleep(0.05)
        assert len(publisher_calls) == 0


async def test_redis_bus_forwards_to_event_dispatcher(
    redis_host: str, redis_port: int
) -> None:
    """
    Test: RedisEventBus automatically routes received cross-process events
    into the worker's local EventDispatcher.
    """
    prefix = f"test_forward_{uuid.uuid4().hex[:8]}:"

    disp_worker = EventDispatcher()
    bus_worker = RedisEventBus(
        dispatcher=disp_worker,
        host=redis_host,
        port=redis_port,
        channel_prefix=prefix,
    )

    bus_sender = RedisEventBus(
        host=redis_host,
        port=redis_port,
        channel_prefix=prefix,
    )

    dispatcher_received: list[Any] = []
    ready = asyncio.Event()

    async def async_dispatcher_handler(payload: Any) -> None:
        dispatcher_received.append(payload)
        ready.set()

    # Listener registered in local EventDispatcher, NOT directly in bus
    disp_worker.listen("cache.evict", async_dispatcher_handler)

    async with bus_sender, bus_worker:
        payload = {"keys": ["session:abc", "user:42"]}
        await bus_sender.publish("cache.evict", payload)

        await asyncio.wait_for(ready.wait(), timeout=5.0)
        assert dispatcher_received == [payload]


async def test_redis_bus_payload_serialization(
    redis_host: str, redis_port: int
) -> None:
    """
    Test: Payloads containing datetimes, UUIDs, nested structures, and lists
    are correctly serialized over Redis and reconstructed.
    """
    prefix = f"test_serial_{uuid.uuid4().hex[:8]}:"

    bus_sender = RedisEventBus(host=redis_host, port=redis_port, channel_prefix=prefix)
    bus_receiver = RedisEventBus(
        host=redis_host, port=redis_port, channel_prefix=prefix
    )

    received_data: list[Any] = []
    received_event = asyncio.Event()

    def on_complex_data(p: Any) -> None:
        received_data.append(p)
        received_event.set()

    bus_receiver.subscribe("complex.data", on_complex_data)

    now = datetime(2026, 10, 25, 20, 0, 0, tzinfo=UTC)
    uid = uuid.uuid4()

    complex_payload = {
        "id": 123,
        "uuid": uid,
        "created_at": now,
        "items": [1, 2, "three"],
        "metadata": {"active": True, "ratio": 3.14},
    }

    async with bus_sender, bus_receiver:
        await bus_sender.publish("complex.data", complex_payload)
        await asyncio.wait_for(received_event.wait(), timeout=5.0)

        assert len(received_data) == 1
        rx = received_data[0]
        assert rx["id"] == 123
        assert rx["uuid"] == str(uid)
        assert rx["created_at"] == now.isoformat()
        assert rx["items"] == [1, 2, "three"]
        assert rx["metadata"] == {"active": True, "ratio": 3.14}


async def test_redis_bus_idempotent_close(redis_host: str, redis_port: int) -> None:
    """Test: Calling close() repeatedly is safe and idempotent."""
    bus = RedisEventBus(host=redis_host, port=redis_port)
    await bus.start_listening()
    await bus.wait_until_ready()

    await bus.close()
    # Repeated close should succeed without errors
    await bus.close()
    await bus.close()


async def test_redis_bus_caller_client_ownership(
    redis_host: str, redis_port: int
) -> None:
    """
    Test: When an external Redis client is passed to RedisEventBus, closing the bus
    does NOT close the caller's client (_owns_client=False).
    """
    client = aioredis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    try:
        bus = RedisEventBus(redis_client=client)
        assert bus._owns_client is False

        async with bus:
            await bus.wait_until_ready()

        # After bus closes on context exit, client must still be active and usable
        pong = await client.ping()
        assert pong is True
    finally:
        await client.aclose()


async def test_redis_bus_prefix_isolation(redis_host: str, redis_port: int) -> None:
    """Test: Two buses with different channel prefixes are isolated from each other."""
    prefix_1 = f"prefix_a_{uuid.uuid4().hex[:8]}:"
    prefix_2 = f"prefix_b_{uuid.uuid4().hex[:8]}:"

    bus_1 = RedisEventBus(host=redis_host, port=redis_port, channel_prefix=prefix_1)
    bus_2 = RedisEventBus(host=redis_host, port=redis_port, channel_prefix=prefix_2)

    received_2: list[Any] = []
    bus_2.subscribe("isolated.event", lambda p: received_2.append(p))

    async with bus_1, bus_2:
        await bus_1.publish("isolated.event", {"target": "prefix_1"})
        await asyncio.sleep(0.1)

        # bus_2 should NOT have received event published on prefix_1
        assert len(received_2) == 0


async def test_redis_bus_serialization_strictness_and_timezones(
    redis_host: str, redis_port: int
) -> None:
    """
    Test: Unsupported types and circular structures raise TypeError on publish.
    Timezone offsets and date objects serialize properly.
    """
    bus_sender = RedisEventBus(host=redis_host, port=redis_port)
    bus_receiver = RedisEventBus(host=redis_host, port=redis_port)

    # 1. Unsupported type (set) raises TypeError
    with pytest.raises(TypeError, match="Unsupported payload type"):
        await bus_sender.publish("unsupported.set", {"tags": {"admin", "editor"}})

    # 2. Circular reference raises TypeError
    circular_dict: dict[str, Any] = {"name": "loop"}
    circular_dict["self"] = circular_dict
    with pytest.raises(TypeError, match="Circular reference"):
        await bus_sender.publish("circular.event", circular_dict)

    # 3. Timezone with non-UTC offset and date objects
    tz_ist = timezone(timedelta(hours=5, minutes=30))
    dt_with_offset = datetime(2026, 10, 25, 14, 30, 0, tzinfo=tz_ist)
    pure_date = date(2026, 10, 25)

    rx_payload: dict[str, Any] = {}
    done_event = asyncio.Event()

    def on_tz_event(p: Any) -> None:
        rx_payload.update(p)
        done_event.set()

    bus_receiver.subscribe("tz.event", on_tz_event)

    async with bus_sender, bus_receiver:
        await bus_sender.publish(
            "tz.event",
            {"dt": dt_with_offset, "d": pure_date, "uuid_list": [uuid.uuid4()]},
        )
        await asyncio.wait_for(done_event.wait(), timeout=5.0)

        assert rx_payload["dt"] == dt_with_offset.isoformat()
        assert "+05:30" in rx_payload["dt"]
        assert rx_payload["d"] == pure_date.isoformat()
        assert len(rx_payload["uuid_list"]) == 1


async def test_redis_bus_resilience_to_malformed_messages_and_handler_errors(
    redis_host: str, redis_port: int
) -> None:
    """
    Test: The Redis listener loop survives malformed raw messages, invalid envelopes,
    and exceptions thrown by subscriber handlers without crashing or stopping.
    """
    prefix = f"resilience_{uuid.uuid4().hex[:8]}:"
    raw_client = aioredis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    bus = RedisEventBus(host=redis_host, port=redis_port, channel_prefix=prefix)
    sender_bus = RedisEventBus(host=redis_host, port=redis_port, channel_prefix=prefix)

    succeeded_event = asyncio.Event()
    succeeded_payloads: list[Any] = []

    def failing_handler(p: Any) -> None:
        raise RuntimeError("Handler failed unexpectedly!")

    def healthy_handler(p: Any) -> None:
        succeeded_payloads.append(p)
        succeeded_event.set()

    bus.subscribe("failing.action", failing_handler)
    bus.subscribe("healthy.action", healthy_handler)

    try:
        async with bus:
            # 1. Publish malformed non-JSON data directly to Redis channel
            channel_failing = f"{prefix}failing.action"

            await raw_client.publish(channel_failing, "NOT_VALID_JSON{:::<><>")

            # 2. Publish invalid envelope (non-dict or missing event)
            await raw_client.publish(channel_failing, json.dumps("string_envelope"))
            await raw_client.publish(
                channel_failing, json.dumps({"only_sender": "123"})
            )

            # 3. Publish to failing handler (handler raises exception)
            await sender_bus.publish("failing.action", {"step": 1})

            # 4. Publish to healthy handler (verify the listener loop survived and continues)
            await sender_bus.publish("healthy.action", {"step": 2, "status": "ok"})

            await asyncio.wait_for(succeeded_event.wait(), timeout=5.0)
            assert succeeded_payloads == [{"step": 2, "status": "ok"}]
    finally:
        await sender_bus.close()
        await raw_client.aclose()


async def test_cross_process_event_delivery_via_redis_bus(
    redis_host: str, redis_port: int
) -> None:
    """
    Test: An event fired in one worker process is received by a listener
    registered in a DIFFERENT worker process, via the Redis bus.
    """
    prefix = f"xprocess_{uuid.uuid4().hex[:8]}:"
    ready_key = f"{prefix}worker_b_ready"
    result_key = f"{prefix}worker_b_result"

    client = aioredis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    await client.delete(ready_key, result_key)

    worker_script = f"""
import asyncio
import json
import os
import redis.asyncio as aioredis
from swiftpy.events.redis_bus import RedisEventBus

async def main():
    host = "{redis_host}"
    port = {redis_port}
    prefix = "{prefix}"
    ready_key = "{ready_key}"
    result_key = "{result_key}"

    client = aioredis.Redis(host=host, port=port, decode_responses=True)
    bus = RedisEventBus(redis_client=client, channel_prefix=prefix)

    async def on_event(payload):
        await client.set(result_key, json.dumps(payload))
        await client.set(f"{{prefix}}completed", "1")

    bus.subscribe("order.cross_process.shipped", on_event)
    await bus.start_listening()
    await bus.wait_until_ready()

    # Notify test runner that Worker B is ready and listening
    await client.set(ready_key, "1")

    # Keep worker alive until event is handled or timeout
    for _ in range(60):
        done = await client.get(f"{{prefix}}completed")
        if done == "1":
            break
        await asyncio.sleep(0.1)

    await bus.close()
    await client.aclose()

if __name__ == "__main__":
    asyncio.run(main())
"""

    # Spawn Worker B as an independent OS process
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        worker_script,
        env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    bus_a = RedisEventBus(host=redis_host, port=redis_port, channel_prefix=prefix)

    try:
        # Wait for Worker B to declare readiness
        worker_ready = False
        for _ in range(50):
            val = await client.get(ready_key)
            if val == "1":
                worker_ready = True
                break
            await asyncio.sleep(0.1)

        if not worker_ready:
            stdout_data, stderr_data = await proc.communicate()
            raise AssertionError(
                f"Worker B failed to become ready: stdout={stdout_data.decode()}, stderr={stderr_data.decode()}"
            )

        expected_payload = {
            "order_id": 98765,
            "status": "shipped",
            "carrier": "SwiftPost",
            "tracking_number": "TRK-0019283",
        }

        subscribers = await bus_a.publish(
            "order.cross_process.shipped", expected_payload
        )
        assert subscribers >= 1

        # Poll Redis for Worker B's recorded receipt
        received_payload: dict[str, Any] | None = None
        for _ in range(50):
            res_str = await client.get(result_key)
            if res_str is not None:
                received_payload = json.loads(res_str)
                break
            await asyncio.sleep(0.1)

        assert received_payload is not None, "Worker B did not receive the event"
        assert received_payload == expected_payload

        # Ensure Worker B exited cleanly with code 0
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        assert proc.returncode == 0
    finally:
        await bus_a.close()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                proc.kill()
        await client.delete(ready_key, result_key, f"{prefix}completed")
        await client.aclose()
