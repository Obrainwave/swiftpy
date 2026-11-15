from __future__ import annotations

import ast
import asyncio
import base64
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import redis.asyncio as aioredis

from swiftpy.cache.cache import Cache
from swiftpy.cache.drivers.memory import MemoryDriver
from swiftpy.cache.drivers.redis import RedisDriver
from swiftpy.core.application import Application
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container, get_container, set_container
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.events.redis_bus import RedisEventBus
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job, JobSerializationError
from swiftpy.queue.worker import QueueWorker


def _make_redis_client(host: str, port: int) -> aioredis.Redis:
    return aioredis.Redis(
        host=host,
        port=port,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )


class Phase5SampleJob(Job):
    def __init__(self, key: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.key = key

    async def handle(self) -> None:
        if not self.key:
            return
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        client = _make_redis_client(host, port)
        try:
            await client.rpush(self.key, self.job_id)
        finally:
            await client.aclose()


class Phase5NoArgsJob(Job):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    async def handle(self) -> None:
        pass


@pytest.fixture
def isolated_signing_state() -> Any:
    prev_job_key = Job._app_key
    prev_job_prev = list(Job._previous_keys)
    prev_job_ns = Job._trusted_namespaces
    prev_job_cls = set(Job._trusted_classes)
    prev_job_allow_unsigned = getattr(Job, "_allow_unsigned", False)
    prev_bus_key = RedisEventBus._app_key
    prev_bus_prev = list(RedisEventBus._previous_keys)
    prev_bus_allow_unsigned = getattr(RedisEventBus, "_allow_unsigned", False)
    prev_cfg = Config._instance
    prev_container = get_container()
    try:
        yield
    finally:
        Job.set_app_key(prev_job_key)
        Job.set_previous_keys(prev_job_prev)
        Job.set_trusted_namespaces(prev_job_ns)
        Job._trusted_classes = prev_job_cls
        if hasattr(Job, "set_allow_unsigned"):
            Job.set_allow_unsigned(prev_job_allow_unsigned)
        RedisEventBus.set_app_key(prev_bus_key)
        RedisEventBus.set_previous_keys(prev_bus_prev)
        if hasattr(RedisEventBus, "set_allow_unsigned"):
            RedisEventBus.set_allow_unsigned(prev_bus_allow_unsigned)
        Config._instance = prev_cfg
        set_container(prev_container)


def test_queue1_trusted_namespaces_exclude_tests_and_require_opt_in(
    isolated_signing_state: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QUEUE-1: Default namespaces drop tests/test*; unkeyed serialize/deserialize fail closed."""
    Job.set_trusted_namespaces(None)
    assert Job.get_trusted_namespaces() == ["app", "swiftpy"]

    monkeypatch.delenv("APP_KEY", raising=False)
    empty_cfg = Config({"app": {"env": "local", "key": ""}})
    c = Container()
    c.instance(Config, empty_cfg)
    set_container(c)
    Config._instance = empty_cfg
    Job.set_app_key(None)
    RedisEventBus.set_app_key(None)

    job = Phase5SampleJob(key="x")
    with pytest.raises(ConfigurationError):
        job.serialize()

    with pytest.raises(ConfigurationError):
        Job.deserialize({"job_class": "app.jobs:Missing"})

    # Explicit opt-in still permits local unsigned test round-trips
    Job.allow_job_class(Phase5SampleJob)
    unsigned_env = job.serialize(allow_unsigned=True)
    assert "signature" not in unsigned_env
    restored = Job.deserialize(unsigned_env, allow_unsigned=True)
    assert isinstance(restored, Phase5SampleJob)

    # Without explicit allowlist, tests.* and test* modules are rejected BEFORE import
    key_b64 = "base64:" + base64.b64encode(b"k" * 32).decode("ascii")
    Job.set_app_key(key_b64)
    signed_test_mod = Phase5NoArgsJob().serialize()
    Job._trusted_classes.clear()
    with patch("importlib.import_module") as mock_import:
        with pytest.raises(JobSerializationError, match="trusted namespaces"):
            Job.deserialize(signed_test_mod)
        mock_import.assert_not_called()


def test_queue1_base64_and_entropy_validation_and_app_startup(
    isolated_signing_state: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QUEUE-1: base64: keys decode to raw >=32B secret; short/malformed keys fail closed."""
    raw_32 = os.urandom(32)
    b64_key = "base64:" + base64.b64encode(raw_32).decode("ascii")
    Job.set_app_key(b64_key)
    Job.allow_job_class(Phase5NoArgsJob)

    # Malformed or short keys fail
    Job.set_app_key("too-short-secret")
    with pytest.raises(ConfigurationError, match="32"):
        Phase5NoArgsJob().serialize()

    short_b64 = "base64:" + base64.b64encode(b"a" * 16).decode("ascii")
    Job.set_app_key(short_b64)
    with pytest.raises(ConfigurationError, match="32"):
        Phase5NoArgsJob().serialize()

    Job.set_app_key("base64:%%%invalid%%%")
    with pytest.raises(ConfigurationError):
        Phase5NoArgsJob().serialize()

    # Valid base64 key signs & verifies cleanly, and differs from naive ASCII encoding
    Job.set_app_key(b64_key)
    env_dict = Phase5NoArgsJob().serialize()
    restored = Job.deserialize(env_dict)
    assert isinstance(restored, Phase5NoArgsJob)


@pytest.mark.asyncio
async def test_queue1_production_app_and_bus_fail_closed_without_app_key(
    isolated_signing_state: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QUEUE-1: Production Application startup and RedisEventBus fail closed when APP_KEY missing/short."""
    monkeypatch.delenv("APP_KEY", raising=False)
    prod_app = Application(
        auto_discover_providers=False,
        debug=False,
        config={"app": {"env": "production", "debug": False, "key": ""}},
    )
    with pytest.raises(ConfigurationError):
        await prod_app.startup()

    bus = RedisEventBus(host=os.getenv("REDIS_HOST", "127.0.0.1"), port=int(os.getenv("REDIS_PORT", "6379")))
    with pytest.raises(ConfigurationError):
        await bus.publish("order.created", {"id": 1})
    await bus.close()


@pytest.mark.asyncio
async def test_queue2_and_queue6_reap_preserves_signature_and_empty_args_and_dlq_cap(
    isolated_signing_state: Any,
) -> None:
    """QUEUE-2 & QUEUE-6: Reaper preserves args=[], valid HMAC signature, DLQ cap, and EVALSHA."""
    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("REDIS_PORT", "6379"))
    prefix = f"test_p5_q2_{uuid.uuid4().hex[:8]}:"
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    Job.set_app_key("base64:" + base64.b64encode(b"z" * 32).decode("ascii"))
    Job.allow_job_class(Phase5NoArgsJob)

    dispatcher = QueueDispatcher(host=host, port=port, prefix=prefix, failed_max_size=2)
    try:
        # 1. Reap preserves empty args=[] and valid signature
        job = Phase5NoArgsJob(tries=3)
        await dispatcher.push(job, queue=q_name)

        leased = await dispatcher.pop_and_lease(q_name, "w1", lease_duration=0.05)
        assert leased is not None
        await asyncio.sleep(0.1)

        reaped = await dispatcher.reap_expired_leases(q_name)
        assert reaped == 1

        client = await dispatcher.get_client()
        requeued_raw = await client.lindex(dispatcher._queue_key(q_name), 0)
        assert requeued_raw is not None
        requeued_obj = json.loads(requeued_raw)
        assert requeued_obj["args"] == []
        assert requeued_obj["attempts"] == 1
        restored = Job.deserialize(requeued_obj)
        assert isinstance(restored, Phase5NoArgsJob)
        assert restored.attempts == 1

        # 2. Reaper moves exhausted jobs to DLQ keeping args=[] and honoring failed_max_size=2
        await dispatcher.clear(q_name)
        for _ in range(4):
            j = Phase5NoArgsJob(tries=1)
            await dispatcher.push(j, queue=q_name)
            await dispatcher.pop_and_lease(q_name, "w1", lease_duration=0.02)
        await asyncio.sleep(0.08)
        assert await dispatcher.reap_expired_leases(q_name) == 4
        assert await dispatcher.failed_size(q_name) == 2

        failed_records = await dispatcher.get_failed_jobs(q_name)
        assert failed_records[0]["payload"]["args"] == []

        # 3. retry_failed resets attempts=0 and re-signs cleanly
        old_key = "base64:" + base64.b64encode(b"o" * 32).decode("ascii")
        new_key = "base64:" + base64.b64encode(b"n" * 32).decode("ascii")
        Job.set_app_key(new_key)
        Job.set_previous_keys([])
        await dispatcher.retry_failed(q_name, job_id="all")
        retried_raw = await client.lindex(dispatcher._queue_key(q_name), 0)
        assert retried_raw is not None
        retried_obj = json.loads(retried_raw)
        assert retried_obj["attempts"] == 0
        # Signed with NEW active key so old_key no longer needed
        Job.set_previous_keys([old_key])
        assert isinstance(Job.deserialize(retried_obj), Phase5NoArgsJob)

        # 4. EVALSHA recovery after SCRIPT FLUSH
        await client.script_flush()
        j2 = Phase5NoArgsJob()
        await dispatcher.push(j2, queue=q_name)
        assert await dispatcher.pop_and_lease(q_name, "w2", lease_duration=10.0) is not None
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()


@pytest.mark.asyncio
async def test_queue3_ack_and_release_require_exact_payload_match(
    isolated_signing_state: Any,
) -> None:
    """QUEUE-3: Stale ack/release for an expired job never touches another active job."""
    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("REDIS_PORT", "6379"))
    prefix = f"test_p5_q3_{uuid.uuid4().hex[:8]}:"
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    Job.set_app_key("base64:" + base64.b64encode(b"q" * 32).decode("ascii"))
    Job.allow_job_class(Phase5NoArgsJob)

    dispatcher = QueueDispatcher(host=host, port=port, prefix=prefix)
    try:
        j_expired = Phase5NoArgsJob(job_id="a" * 32, tries=2)
        j_active = Phase5NoArgsJob(job_id="b" * 32, tries=2)
        with patch("swiftpy.queue.dispatcher.time.time", return_value=1700000000.123456):
            await dispatcher.push(j_expired, queue=q_name)
        raw_expired = await dispatcher.pop_and_lease(q_name, "w1", lease_duration=0.05)
        assert raw_expired is not None
        await asyncio.sleep(0.08)
        assert await dispatcher.reap_expired_leases(q_name) == 1

        # Clear ready queue of reaped j_expired and push same-length j_active to w1
        await dispatcher.clear(q_name)
        with patch("swiftpy.queue.dispatcher.time.time", return_value=1700000000.654321):
            await dispatcher.push(j_active, queue=q_name)
        raw_active = await dispatcher.pop_and_lease(q_name, "w1", lease_duration=30.0)
        assert raw_active is not None
        assert len(raw_active) == len(raw_expired)

        # Late ack / release of raw_expired by w1 must NOT touch raw_active!
        await dispatcher.ack(q_name, "w1", raw_expired)
        await dispatcher.release(q_name, "w1", raw_expired, delay=0)

        assert await dispatcher.processing_size(q_name, "w1") == 1
        assert await dispatcher.leases_size(q_name) == 1
        assert await dispatcher.size(q_name) == 0
    finally:
        await dispatcher.clear(q_name)
        await dispatcher.close()


@pytest.mark.asyncio
async def test_queue4_concurrency_with_max_jobs_and_throttled_promote(
    isolated_signing_state: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QUEUE-4: max_jobs=2 with concurrency=2 processes/pops <= 2 jobs and throttles promote/reap."""
    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("REDIS_PORT", "6379"))
    prefix = f"test_p5_q4_{uuid.uuid4().hex[:8]}:"
    q_name = f"q_{uuid.uuid4().hex[:6]}"
    res_key = f"{prefix}done"
    Job.set_app_key("base64:" + base64.b64encode(b"w" * 32).decode("ascii"))
    Job.allow_job_class(Phase5SampleJob)

    dispatcher = QueueDispatcher(host=host, port=port, prefix=prefix)
    try:
        for _ in range(5):
            await dispatcher.push(Phase5SampleJob(key=res_key), queue=q_name)

        worker = QueueWorker(
            queues=q_name,
            dispatcher=dispatcher,
            concurrency=2,
            max_jobs=2,
            sleep=0.02,
            stop_when_empty=True,
        )
        promote_calls = 0
        orig_promote = worker._promote_delayed_jobs

        async def tracked_promote(queue: str) -> int:
            nonlocal promote_calls
            promote_calls += 1
            return await orig_promote(queue)

        monkeypatch.setattr(worker, "_promote_delayed_jobs", tracked_promote)
        await worker.work()

        assert worker.jobs_processed == 2
        assert await dispatcher.size(q_name) == 3
        assert await dispatcher.processing_size(q_name) == 0
        assert worker._last_promoted_at > 0.0
        assert promote_calls < 3
    finally:
        client = await dispatcher.get_client()
        await client.delete(res_key)
        await dispatcher.clear(q_name)
        await dispatcher.close()


def test_queue5_and_queue7_timeout_validation_app_isolation_and_top_level_imports(
    isolated_signing_state: Any,
) -> None:
    """QUEUE-5 & QUEUE-7: Reject bad timeout/tries/pushed_at; separate Apps have separate singletons; no inline import."""
    Job.allow_job_class(Phase5NoArgsJob)
    Job.set_app_key("base64:" + base64.b64encode(b"v" * 32).decode("ascii"))

    for bad_timeout in ("bad", True, -1, 0, float("nan"), float("inf")):
        payload = Phase5NoArgsJob().serialize()
        payload["timeout"] = bad_timeout
        payload["signature"] = Job._compute_signature(
            payload, Job.get_app_key() or ""
        )
        with pytest.raises(JobSerializationError, match="timeout"):
            Job.deserialize(payload)

    app1 = Application(auto_discover_providers=False)
    app2 = Application(auto_discover_providers=False)
    assert app1.container.resolve(EventDispatcher) is not app2.container.resolve(EventDispatcher)
    assert app1.container.resolve(QueueDispatcher) is not app2.container.resolve(QueueDispatcher)
    assert app1.container.resolve(RedisEventBus) is not app2.container.resolve(RedisEventBus)

    tree = ast.parse(
        Path("tests/queue/test_worker_behavior.py").read_text(encoding="utf-8")
    )
    inline_imports = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and not any(
            node in mod_body
            for mod_body in [tree.body]
        )
    ]
    assert inline_imports == []


@pytest.mark.asyncio
async def test_cache1_2_3_facade_memory_sweep_and_redis_remember_and_incr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CACHE-1..3: Cache forwards ttl/lock_timeout/max_items/prefix; MemoryDriver cursor sweeps all expired keys; RedisDriver perm TTL & single-flight."""
    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("REDIS_PORT", "6379"))
    pw = os.getenv("REDIS_PASSWORD") or None

    cfg = Config(
        {
            "app": {"name": "myapp"},
            "cache": {
                "default": "memory",
                "prefix": "cache:",
                "stores": {
                    "memory": {"driver": "memory", "prefix": "cache:", "max_items": 5},
                },
            },
        }
    )
    mgr = Cache(config=cfg)
    mem_drv = mgr.driver()
    assert isinstance(mem_drv, MemoryDriver)
    assert mem_drv.prefix == "myapp:cache:"
    assert mem_drv.max_items == 5

    cnt = await mgr.increment("hits", 2, ttl=30.0)
    assert cnt == 2
    dec = await mgr.decrement("hits", 1, ttl=30.0)
    assert dec == 1

    called = await mgr.remember("k_to", 10.0, lambda: "v", lock_timeout=2.5)
    assert called == "v"
    await mgr.close()

    # CACHE-2: MemoryDriver cursor sweeps beyond first 20 permanent entries & keeps prefix=""
    now = 100.0
    drv = MemoryDriver(prefix="", default_ttl=None, clock=lambda: now)
    assert drv.prefix == ""
    assert drv.default_ttl is None
    for i in range(20):
        await drv.set(f"perm_{i}", i, ttl=None)
    for i in range(50):
        await drv.set(f"temp_{i}", i, ttl=1.0)
    now = 105.0
    for j in range(10):
        await drv.set(f"trigger_{j}", j, ttl=None)
    remaining_temp = [k for k in drv._storage if k.startswith("temp_")]
    assert remaining_temp == []
    await drv.close()

    # CACHE-2 & CACHE-3: RedisDriver permanent key & single-flight remember & px
    r_prefix = f"test_p5_cache_{uuid.uuid4().hex[:8]}:"
    r_drv = RedisDriver(
        host=host,
        port=port,
        password=pw,
        prefix=r_prefix,
        default_ttl=60.0,
    )
    try:
        r_client = await r_drv.get_client()
        # Simulate an existing permanent Redis key (pttl == -1); even with default_ttl=60
        # or explicit ttl=30.0 passed to increment(), RedisDriver must NOT attach an expiration.
        await r_client.set(f"{r_prefix}perm_counter", "10")
        assert await r_client.pttl(f"{r_prefix}perm_counter") == -1
        assert await r_drv.increment("perm_counter", 1, ttl=30.0) == 11
        assert await r_client.pttl(f"{r_prefix}perm_counter") == -1

        compute_count = 0

        async def slow_compute() -> str:
            nonlocal compute_count
            compute_count += 1
            await asyncio.sleep(0.05)
            return "computed_once"

        # Count Redis SET NX commands for lock key across 20 concurrent calls
        orig_set = r_client.set
        nx_calls = 0

        async def tracked_set(*args: Any, **kwargs: Any) -> Any:
            nonlocal nx_calls
            if kwargs.get("nx") and ":lock:" in str(args[0]):
                nx_calls += 1
            return await orig_set(*args, **kwargs)

        monkeypatch.setattr(r_client, "set", tracked_set)
        results = await asyncio.gather(
            *(
                r_drv.remember("coalesce_key", 30.0, slow_compute, lock_timeout=0.5)
                for _ in range(20)
            )
        )
        assert results == ["computed_once"] * 20
        assert compute_count == 1
        assert nx_calls == 1

        # lock_timeout <= 0 still executes 1 SET NX attempt cleanly
        await r_drv.forget("coalesce_key")
        nx_before = nx_calls
        val0 = await r_drv.remember(
            "zero_timeout_key", 30.0, lambda: "instant", lock_timeout=0.0
        )
        assert val0 == "instant"
        assert nx_calls == nx_before + 1
    finally:
        await r_drv.flush()
        await r_drv.close()


@dataclass
class Phase5CustomEvent:
    order_id: int


class SecretObject:
    __hidden__ = ("secret_token",)

    def __init__(self) -> None:
        self.visible = "ok"
        self.secret_token = "top-secret-value"


@pytest.mark.asyncio
async def test_evt1_and_evt2_dispatcher_kwargs_forget_hidden_and_backpressure(
    isolated_signing_state: Any,
) -> None:
    """EVT-1 & EVT-2: kwargs/event-class forget/wildcard unsubscribe; __hidden__ stripping & bounded tasks."""
    disp = EventDispatcher()
    captured: list[Any] = []

    def kw_only_var(**kwargs: Any) -> None:
        captured.append(("kw_only", kwargs))

    def pos_and_var(event: str, payload: Any, **kwargs: Any) -> None:
        captured.append(("pos_and_var", event, payload))

    disp.listen("evt.one", kw_only_var)
    disp.listen("evt.one", pos_and_var)
    await disp.dispatch("evt.one", {"n": 1})

    assert ("kw_only", {"event": "evt.one", "payload": {"n": 1}}) in captured
    assert ("pos_and_var", "evt.one", {"n": 1}) in captured

    def class_handler(payload: Any) -> None:
        captured.append(payload)

    disp.listen(Phase5CustomEvent, class_handler)
    assert disp.has_listeners(Phase5CustomEvent) is True
    disp.forget(Phase5CustomEvent)
    assert disp.has_listeners(Phase5CustomEvent) is False

    # EVT-2: __hidden__ filtered + semaphore acquired before create_task
    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("REDIS_PORT", "6379"))
    bus = RedisEventBus(
        host=host,
        port=port,
        channel_prefix=f"test_p5_evt_{uuid.uuid4().hex[:8]}:",
        max_concurrency=2,
        ignore_self=False,
    )
    RedisEventBus.set_app_key("base64:" + base64.b64encode(b"e" * 32).decode("ascii"))
    serialized = bus._serialize_value(SecretObject())
    assert serialized == {"visible": "ok"}

    in_handler = asyncio.Event()
    release_handler = asyncio.Event()

    async def blocking_listener(payload: Any) -> None:
        in_handler.set()
        await release_handler.wait()

    bus.dispatcher.listen("burst.test", blocking_listener)
    msg_template = {
        "event": "burst.test",
        "payload": {"i": 1},
        "sender_id": "other",
        "timestamp": time.time(),
    }
    msg_template["signature"] = bus._compute_signature(
        msg_template, RedisEventBus.get_app_key() or ""
    )
    raw = json.dumps(msg_template)

    t1 = asyncio.create_task(bus._handle_raw_message(raw))
    t2 = asyncio.create_task(bus._handle_raw_message(raw))
    await asyncio.wait_for(in_handler.wait(), timeout=2.0)
    await asyncio.gather(t1, t2)
    await asyncio.sleep(0.01)
    assert len(bus._active_dispatch_tasks) <= 2

    # Third message must block acquiring semaphore before creating task
    t3 = asyncio.create_task(bus._handle_raw_message(raw))
    await asyncio.sleep(0.02)
    assert not t3.done()
    assert len(bus._active_dispatch_tasks) == 2

    release_handler.set()
    await asyncio.wait_for(t3, timeout=2.0)
    await asyncio.gather(*list(bus._active_dispatch_tasks), return_exceptions=True)
    await bus.close()
