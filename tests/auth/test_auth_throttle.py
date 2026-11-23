from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Generator
from typing import Any, ClassVar

import pytest
from redis.exceptions import RedisError

from swiftpy import Auth, Hash, LoginThrottle, TestClient, TooManyRequestsError
from swiftpy.auth.events import Lockout
from swiftpy.cache.cache import Cache
from swiftpy.cache.drivers.memory import MemoryDriver
from swiftpy.cache.drivers.redis import RedisDriver
from swiftpy.core.application import Application
from swiftpy.core.container import bind_container
from swiftpy.core.context import Context
from swiftpy.database.orm.model import (
    Model,
    restore_model_registry,
    snapshot_model_registry,
)
from swiftpy.database.pool import DatabasePool
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.http.request import Request
from swiftpy.http.response import json_response


class ThrottleUser(Model):
    __table__ = "test_throttle_users"
    __primary_key__ = "id"
    __fillable__: ClassVar[list[str]] = ["name", "email", "password"]
    __hidden__: ClassVar[list[str]] = ["password"]
    __guarded__: ClassVar[list[str]] = []

    id: int
    name: str
    email: str
    password: str


@pytest.fixture(scope="module", autouse=True)
def _preserve_model_registry() -> Generator[None, None, None]:
    snap = snapshot_model_registry()
    yield
    restore_model_registry(snap)


@pytest.fixture(autouse=True)
def _clean_state() -> Generator[None, None, None]:
    Context.clear()
    EventDispatcher.set_instance(EventDispatcher())
    yield
    Context.clear()
    EventDispatcher.set_instance(None)


@pytest.fixture
async def throttle_app(ensure_db_pool: DatabasePool) -> Application:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS test_throttle_users CASCADE;
            CREATE TABLE test_throttle_users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL
            );
            """
        )

    pw_hash = Hash.make("correct-password", rounds=4)
    await ThrottleUser.create(
        {
            "name": "Alice",
            "email": "alice@example.com",
            "password": pw_hash,
        }
    )
    await ThrottleUser.create(
        {
            "name": "Bob",
            "email": "bob@example.com",
            "password": pw_hash,
        }
    )

    app = Application()
    app.config.set("auth.model", ThrottleUser)
    app.config.set("cache.default", "memory")
    app.config.set("cache.stores", {"memory": {"driver": "memory"}})
    app.config.set(
        "auth.throttle",
        {
            "enabled": True,
            "max_attempts": 5,
            "decay_seconds": 60,
            "lockout_seconds": 60,
            "prefix": "test:throttle",
        },
    )
    app.container.instance(DatabasePool, ensure_db_pool)
    app.container.instance(Cache, Cache(config=app.config, default_driver="memory"))
    return app


@pytest.mark.asyncio
async def test_default_lockout_and_events_and_reset(throttle_app: Application) -> None:
    lockouts: list[Lockout] = []
    EventDispatcher.get_instance().listen(Lockout, lambda ev: lockouts.append(ev))

    with bind_container(throttle_app.container):
        for idx in range(5):
            assert (
                await Auth.attempt(
                    {"email": "alice@example.com", "password": f"bad-{idx}"},
                    ip="203.0.113.10",
                )
                is False
            )

        assert (
            await Auth.too_many_attempts("Alice@Example.com", ip="203.0.113.10")
            is True
        )
        assert await Auth.available_in("alice@example.com", ip="203.0.113.10") >= 1

        # 6th attempt raises TooManyRequestsError even with valid password
        with pytest.raises(TooManyRequestsError) as exc_info:
            await Auth.attempt(
                {"email": "alice@example.com", "password": "correct-password"},
                ip="203.0.113.10",
            )
        assert exc_info.value.status_code == 429
        assert exc_info.value.headers is not None
        assert "Retry-After" in exc_info.value.headers
        assert len(lockouts) == 1
        assert lockouts[0].ip == "203.0.113.10"

        # Different IP or different identifier remains isolated
        assert (
            await Auth.attempt(
                {"email": "alice@example.com", "password": "correct-password"},
                ip="198.51.100.2",
            )
            is True
        )
        await Auth.logout()

        # Successful attempt clears previous failed counter
        assert (
            await Auth.attempt(
                {"email": "bob@example.com", "password": "wrong"},
                ip="203.0.113.10",
            )
            is False
        )
        assert (
            await Auth.attempt(
                {"email": "bob@example.com", "password": "wrong"},
                ip="203.0.113.10",
            )
            is False
        )
        assert (
            await Auth.attempt(
                {"email": "bob@example.com", "password": "correct-password"},
                ip="203.0.113.10",
            )
            is True
        )
        await Auth.logout()
        limiter = LoginThrottle()
        assert await limiter.retries_left("bob@example.com", "203.0.113.10") == 5


@pytest.mark.asyncio
async def test_http_route_returns_429_with_retry_after(
    throttle_app: Application,
) -> None:
    @throttle_app.post("/login")
    async def login_route(request: Request) -> Any:
        data = await request.json()
        ok = await Auth.attempt(
            {"email": data["email"], "password": data["password"]},
        )
        return json_response({"ok": ok}, status_code=200 if ok else 401)

    client = TestClient(throttle_app)
    for _ in range(5):
        resp = await client.post(
            "/login",
            json={"email": "http-user@example.com", "password": "bad"},
        )
        assert resp.status_code == 401

    locked_resp = await client.post(
        "/login",
        json={"email": "http-user@example.com", "password": "bad"},
    )
    assert locked_resp.status_code == 429
    assert "retry-after" in locked_resp.headers


@pytest.mark.asyncio
async def test_throttle_can_be_disabled(throttle_app: Application) -> None:
    throttle_app.config.set("auth.throttle.enabled", False)
    with bind_container(throttle_app.container):
        for _ in range(8):
            assert (
                await Auth.attempt(
                    {"email": "disabled@example.com", "password": "bad"},
                    ip="10.0.0.1",
                )
                is False
            )
        assert await Auth.too_many_attempts("disabled@example.com", ip="10.0.0.1") is False


@pytest.mark.asyncio
async def test_throttle_boundary_and_window_expiry(throttle_app: Application) -> None:
    throttle_app.config.set(
        "auth.throttle",
        {
            "enabled": True,
            "max_attempts": 3,
            "decay_seconds": 10,
            "lockout_seconds": 1,
            "prefix": "test:expiry",
        },
    )
    with bind_container(throttle_app.container):
        limiter = LoginThrottle()
        assert await limiter.attempts("edge@example.com", "10.0.0.5") == 0
        assert await limiter.retries_left("edge@example.com", "10.0.0.5") == 3

        assert await limiter.hit("edge@example.com", "10.0.0.5") == 1
        assert await limiter.attempts("edge@example.com", "10.0.0.5") == 1
        assert await limiter.retries_left("edge@example.com", "10.0.0.5") == 2
        assert await limiter.too_many_attempts("edge@example.com", "10.0.0.5") is False

        assert await limiter.hit("edge@example.com", "10.0.0.5") == 2
        assert await limiter.retries_left("edge@example.com", "10.0.0.5") == 1
        assert await limiter.too_many_attempts("edge@example.com", "10.0.0.5") is False

        # Reaching exact max_attempts triggers lockout immediately
        assert await limiter.hit("edge@example.com", "10.0.0.5") == 3
        assert await limiter.retries_left("edge@example.com", "10.0.0.5") == 0
        assert await limiter.attempts("edge@example.com", "10.0.0.5") == 3
        assert await limiter.too_many_attempts("edge@example.com", "10.0.0.5") is True
        assert await limiter.available_in("edge@example.com", "10.0.0.5") >= 1

        # After 1s lockout window expires (even with decay_seconds=10), new generation starts fresh
        await asyncio.sleep(1.15)
        assert await limiter.too_many_attempts("edge@example.com", "10.0.0.5") is False
        assert await limiter.available_in("edge@example.com", "10.0.0.5") == 0
        assert await limiter.attempts("edge@example.com", "10.0.0.5") == 0
        assert await limiter.retries_left("edge@example.com", "10.0.0.5") == 3
        # Even valid Auth.attempt() is blocked while locked, and succeeds after expiry
        for _ in range(3):
            await Auth.attempt(
                {"email": "alice@example.com", "password": "wrong"},
                ip="10.0.0.8",
            )
        with pytest.raises(TooManyRequestsError):
            await Auth.attempt(
                {"email": "alice@example.com", "password": "correct-password"},
                ip="10.0.0.8",
            )
        await asyncio.sleep(1.15)
        assert (
            await Auth.attempt(
                {"email": "alice@example.com", "password": "correct-password"},
                ip="10.0.0.8",
            )
            is True
        )
        assert await limiter.attempts("alice@example.com", "10.0.0.8") == 0
        await limiter.clear("edge@example.com", "10.0.0.5")
        await Auth.logout()


@pytest.mark.asyncio
async def test_throttle_concurrent_atomicity_memory_and_redis(
    throttle_app: Application,
) -> None:
    mem_driver = MemoryDriver(prefix="concurrent:")
    mem_throttle = LoginThrottle(
        max_attempts=5, decay_seconds=60, lockout_seconds=60, driver=mem_driver
    )

    # Exact threshold concurrency: 5 simultaneous hits must produce exact 1..5 atomic counts
    exact_counts = await asyncio.gather(
        *[mem_throttle.hit("exact-conc@example.com", "1.2.3.4") for _ in range(5)]
    )
    assert sorted(exact_counts) == [1, 2, 3, 4, 5]
    assert await mem_throttle.attempts("exact-conc@example.com", "1.2.3.4") == 5
    assert await mem_throttle.retries_left("exact-conc@example.com", "1.2.3.4") == 0
    assert await mem_throttle.too_many_attempts("exact-conc@example.com", "1.2.3.4") is True

    # Over-threshold burst concurrency: threshold 1..5 hit atomically; capped once locked
    counts = await asyncio.gather(
        *[mem_throttle.hit("conc@example.com", "1.2.3.4") for _ in range(7)]
    )
    assert {1, 2, 3, 4, 5}.issubset(set(counts))
    assert all(1 <= c <= 7 for c in counts)
    recorded = await mem_throttle.attempts("conc@example.com", "1.2.3.4")
    assert 5 <= recorded <= 7
    assert await mem_throttle.retries_left("conc@example.com", "1.2.3.4") == 0
    assert await mem_throttle.too_many_attempts("conc@example.com", "1.2.3.4") is True

    # Additional hit during active lockout does not reset or bypass lock
    assert await mem_throttle.hit("conc@example.com", "1.2.3.4") == recorded
    assert await mem_throttle.too_many_attempts("conc@example.com", "1.2.3.4") is True
    await mem_throttle.clear("conc@example.com", "1.2.3.4")
    await mem_throttle.clear("exact-conc@example.com", "1.2.3.4")
    assert await mem_throttle.attempts("conc@example.com", "1.2.3.4") == 0

    # Real Redis integration test when test Redis server is reachable
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    throttle_app.config.set(
        "cache.stores",
        {
            "redis": {
                "driver": "redis",
                "host": os.environ.get("REDIS_HOST", "127.0.0.1"),
                "port": redis_port,
                "password": os.environ.get("REDIS_PASSWORD"),
                "database": 15,
            }
        },
    )
    redis_cache = Cache(config=throttle_app.config, default_driver="redis")
    redis_driver = redis_cache.driver("redis")
    assert isinstance(redis_driver, RedisDriver)
    try:
        client = await redis_driver.get_client()
        await client.ping()
    except (ConnectionError, TimeoutError, OSError, RedisError) as exc:
        await redis_cache.close()
        pytest.skip(f"Redis integration server unavailable: {exc}")

    redis_throttle = LoginThrottle(
        max_attempts=3,
        decay_seconds=1,
        lockout_seconds=1,
        prefix="test:redis:throttle",
        driver=redis_driver,
    )
    await redis_throttle.clear("redis@example.com", "9.9.9.9")
    try:
        exact_redis = await asyncio.gather(
            *[redis_throttle.hit("redis-exact@example.com", "9.9.9.9") for _ in range(3)]
        )
        assert sorted(exact_redis) == [1, 2, 3]
        assert await redis_throttle.attempts("redis-exact@example.com", "9.9.9.9") == 3
        assert (
            await redis_throttle.too_many_attempts("redis-exact@example.com", "9.9.9.9")
            is True
        )

        results = await asyncio.gather(
            *[redis_throttle.hit("redis@example.com", "9.9.9.9") for _ in range(5)]
        )
        assert {1, 2, 3}.issubset(set(results))
        assert all(1 <= r <= 5 for r in results)
        redis_recorded = await redis_throttle.attempts("redis@example.com", "9.9.9.9")
        assert 3 <= redis_recorded <= 5
        assert await redis_throttle.retries_left("redis@example.com", "9.9.9.9") == 0
        assert (
            await redis_throttle.too_many_attempts("redis@example.com", "9.9.9.9")
            is True
        )

        # Verify TTL expiry resets lockout & counter cleanly in Redis
        await asyncio.sleep(1.15)
        assert (
            await redis_throttle.too_many_attempts("redis@example.com", "9.9.9.9")
            is False
        )
        assert await redis_throttle.attempts("redis@example.com", "9.9.9.9") == 0
        assert await redis_throttle.retries_left("redis@example.com", "9.9.9.9") == 3
    finally:
        await redis_throttle.clear("redis@example.com", "9.9.9.9")
        await redis_throttle.clear("redis-exact@example.com", "9.9.9.9")
        await redis_cache.close()


@pytest.mark.asyncio
async def test_auth_attempt_throttle_uses_trusted_client_ip_and_ignores_spoofing(
    throttle_app: Application,
) -> None:
    """
    Auth throttling must use Request.ip() rather than raw scope['client'] so:
    - Trusted reverse proxies isolate rate-limit buckets per real client IP.
    - Untrusted peers cannot bypass or poison buckets using spoofed forwarding headers.
    """
    throttle_app.config.set("http.trusted_proxies", ["172.30.0.0/24"])
    throttle_app.config.set(
        "auth.throttle",
        {
            "enabled": True,
            "max_attempts": 2,
            "decay_seconds": 60,
            "lockout_seconds": 60,
            "prefix": "test:proxy:throttle",
        },
    )

    @throttle_app.post("/login")
    async def login(request: Request) -> Any:
        ok = await Auth.attempt(
            {"email": str(request.get("email", "")), "password": str(request.get("password", ""))}
        )
        return json_response({"ok": ok, "ip": request.ip()}, status_code=200 if ok else 401)

    async def _post_login(
        *,
        peer_ip: str,
        email: str,
        password: str,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        payload = json.dumps({"email": email, "password": password}).encode("utf-8")
        raw_headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            *(headers or []),
        ]
        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": raw_headers,
            "query_string": b"",
            "client": (peer_ip, 51234),
            "server": ("127.0.0.1", 8000),
        }
        body_sent = False
        status = 0
        resp_body = bytearray()

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(msg: dict[str, Any]) -> None:
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
            elif msg["type"] == "http.response.body":
                resp_body.extend(msg.get("body", b""))

        await throttle_app(scope, receive, send)
        return status, json.loads(resp_body.decode("utf-8"))

    # 1. Behind trusted proxy (172.30.0.10), victim 198.51.100.1 gets locked out after 2 bad attempts
    for _ in range(2):
        status, data = await _post_login(
            peer_ip="172.30.0.10",
            email="alice@example.com",
            password="wrong",
            headers=[(b"x-forwarded-for", b"198.51.100.1")],
        )
        assert status == 401
        assert data["ip"] == "198.51.100.1"

    locked_status, _ = await _post_login(
        peer_ip="172.30.0.10",
        email="alice@example.com",
        password="correct-password",
        headers=[(b"x-forwarded-for", b"198.51.100.1")],
    )
    assert locked_status == 429

    # Different client behind the SAME trusted proxy IP is NOT locked out (isolated bucket!)
    other_status, other_data = await _post_login(
        peer_ip="172.30.0.10",
        email="alice@example.com",
        password="correct-password",
        headers=[(b"x-forwarded-for", b"198.51.100.2")],
    )
    assert other_status == 200
    assert other_data == {"ok": True, "ip": "198.51.100.2"}

    # 2. Untrusted direct peer CANNOT rotate/bypass throttle using spoofed XFF headers
    for spoof_idx in range(2):
        s_status, s_data = await _post_login(
            peer_ip="203.0.113.99",
            email="bob@example.com",
            password="wrong",
            headers=[
                (b"x-forwarded-for", f"198.51.100.{50 + spoof_idx}".encode("ascii")),
                (b"x-real-ip", f"198.51.100.{80 + spoof_idx}".encode("ascii")),
            ],
        )
        assert s_status == 401
        assert s_data["ip"] == "203.0.113.99"

    blocked_spoof_status, _ = await _post_login(
        peer_ip="203.0.113.99",
        email="bob@example.com",
        password="correct-password",
        headers=[(b"x-forwarded-for", b"198.51.100.200")],
    )
    assert blocked_spoof_status == 429



@pytest.mark.asyncio
async def test_throttle_buckets_ipv6_by_64_prefix_and_unwraps_ipv4_mapped() -> None:
    throttle = LoginThrottle(
        max_attempts=3,
        decay_seconds=60,
        lockout_seconds=60,
        driver=MemoryDriver(prefix="ipv6:"),
    )
    user = "ipv6@example.com"

    # Rotating addresses inside one /64 must not reset the attempt counter
    for host in ("2001:db8:1:2::1", "2001:db8:1:2::2", "2001:db8:1:2:ffff::9"):
        await throttle.hit(user, host)
    assert await throttle.too_many_attempts(user, "2001:db8:1:2::abcd") is True

    # A different /64 and an unrelated IPv4 remain independent
    assert await throttle.too_many_attempts(user, "2001:db8:1:3::1") is False
    assert await throttle.too_many_attempts(user, "203.0.113.5") is False

    # IPv4-mapped IPv6 shares the bucket with the plain IPv4 form
    for _ in range(3):
        await throttle.hit(user, "::ffff:203.0.113.5")
    assert await throttle.too_many_attempts(user, "203.0.113.5") is True


@pytest.mark.asyncio
async def test_throttle_generation_counter_expires() -> None:
    now = [1000.0]
    driver = MemoryDriver(prefix="gen:", clock=lambda: now[0])
    throttle = LoginThrottle(
        max_attempts=1, decay_seconds=10, lockout_seconds=10, driver=driver
    )
    base_key = throttle.make_key("gen@example.com", "198.51.100.1")
    gen_key = throttle._generation_key(base_key)

    await throttle.hit("gen@example.com", "198.51.100.1")
    assert await driver.get(gen_key) == 1

    now[0] += 41.0  # beyond 2 * (lockout + decay)
    assert await driver.get(gen_key) is None


@pytest.mark.asyncio
async def test_throttle_identifier_cap_spans_ips_and_is_off_by_default() -> None:
    capped = LoginThrottle(
        max_attempts=5,
        lockout_seconds=60,
        identifier_max_attempts=3,
        driver=MemoryDriver(prefix="idcap:"),
    )
    user = "victim@example.com"
    for idx in range(3):
        await capped.hit(user, f"198.51.100.{idx + 1}")

    assert await capped.too_many_attempts(user, "192.0.2.77") is True
    assert await capped.available_in(user, "192.0.2.77") > 0
    assert await capped.too_many_attempts("other@example.com", "192.0.2.77") is False

    await capped.clear(user, "192.0.2.77")
    assert await capped.too_many_attempts(user, "192.0.2.77") is False

    default = LoginThrottle(
        max_attempts=5, lockout_seconds=60, driver=MemoryDriver(prefix="idoff:")
    )
    for idx in range(20):
        await default.hit(user, f"198.51.100.{idx + 1}")
    assert await default.too_many_attempts(user, "192.0.2.77") is False
