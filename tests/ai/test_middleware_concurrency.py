from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis

from swiftpy.ai.exceptions import AIBudgetExceededError, AIRateLimitError
from swiftpy.ai.middleware import AIRateLimitMiddleware, TokenBudgetMiddleware
from swiftpy.ai.providers.base import LLMRequest, LLMResponse, Message, TokenUsage


@pytest.fixture
async def live_redis() -> AsyncIterator[aioredis.Redis]:
    host = os.environ.get("REDIS_HOST", "redis")
    password = os.environ.get("REDIS_PASSWORD", "secret")
    client = aioredis.Redis(
        host=host,
        port=6379,
        password=password,
        decode_responses=True,
    )
    try:
        await client.ping()
    except (aioredis.RedisError, OSError, ConnectionError) as exc:
        pytest.skip(f"Live Redis not available: {exc}")

    prefix = "test:ai:ratelimit:concurrency"
    keys = await client.keys(f"{prefix}:*")
    if keys:
        await client.delete(*keys)

    yield client

    keys = await client.keys(f"{prefix}:*")
    if keys:
        await client.delete(*keys)
    await client.aclose()


@pytest.mark.asyncio
async def test_redis_multi_instance_shared_rate_limiting(live_redis: aioredis.Redis) -> None:
    """Verify that multiple middleware instances (simulating workers) share one distributed rate limit."""
    prefix = "test:ai:ratelimit:concurrency:multi"

    worker_a = AIRateLimitMiddleware(
        requests_per_minute=5,
        window_seconds=10.0,
        redis_client=live_redis,
        storage="redis",
        key_prefix=prefix,
    )
    worker_b = AIRateLimitMiddleware(
        requests_per_minute=5,
        window_seconds=10.0,
        redis_client=live_redis,
        storage="redis",
        key_prefix=prefix,
    )

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok")

    req = LLMRequest(messages=[Message(role="user", content="hello")], extra={"user_id": "carol"})

    # Worker A accepts 3 requests
    for _ in range(3):
        await worker_a(req, next_fn)

    # Worker B accepts 2 requests (total 5)
    for _ in range(2):
        await worker_b(req, next_fn)

    # 6th request on Worker A must be rejected
    with pytest.raises(AIRateLimitError) as exc_a:
        await worker_a(req, next_fn)
    assert exc_a.value.retry_after is not None
    assert exc_a.value.retry_after > 0

    # 7th request on Worker B must also be rejected
    with pytest.raises(AIRateLimitError) as exc_b:
        await worker_b(req, next_fn)
    assert exc_b.value.retry_after is not None


@pytest.mark.asyncio
async def test_redis_concurrent_rate_limiting_spikes(live_redis: aioredis.Redis) -> None:
    """
    Verify atomic enforcement under concurrent burst: exactly N requests pass,
    and all excess requests are rejected with AIRateLimitError.
    """
    prefix = "test:ai:ratelimit:concurrency:burst"
    limit = 5
    mw = AIRateLimitMiddleware(
        requests_per_minute=limit,
        window_seconds=10.0,
        redis_client=live_redis,
        storage="redis",
        key_prefix=prefix,
    )

    async def next_fn(req: LLMRequest) -> LLMResponse:
        await asyncio.sleep(0.01)
        return LLMResponse(content="processed")

    req = LLMRequest(messages=[Message(role="user", content="hi")], extra={"user_id": "spiker"})

    results: list[LLMResponse | Exception] = []

    async def _send() -> None:
        try:
            resp = await mw(req, next_fn)
            results.append(resp)
        except (AIRateLimitError, RuntimeError, OSError) as exc:
            results.append(exc)

    # Fire 20 concurrent requests simultaneously
    tasks = [asyncio.create_task(_send()) for _ in range(20)]
    await asyncio.gather(*tasks)

    successes = [r for r in results if isinstance(r, LLMResponse)]
    rate_limited = [r for r in results if isinstance(r, AIRateLimitError)]

    assert len(successes) == limit, f"Expected exactly {limit} passes, got {len(successes)}"
    assert len(rate_limited) == 15, f"Expected 15 rejections, got {len(rate_limited)}"


@pytest.mark.asyncio
async def test_tenant_and_user_isolation_under_concurrency(live_redis: aioredis.Redis) -> None:
    """Verify tenants and users remain strictly isolated under concurrent load."""
    prefix = "test:ai:ratelimit:concurrency:iso"
    mw = AIRateLimitMiddleware(
        requests_per_minute=2,
        window_seconds=10.0,
        redis_client=live_redis,
        storage="redis",
        key_prefix=prefix,
    )

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok")

    req_t1_u1 = LLMRequest(messages=[Message(role="user", content="t")], extra={"tenant_id": "t1", "user_id": "u1"})
    req_t1_u2 = LLMRequest(messages=[Message(role="user", content="t")], extra={"tenant_id": "t1", "user_id": "u2"})
    req_t2_u1 = LLMRequest(messages=[Message(role="user", content="t")], extra={"tenant_id": "t2", "user_id": "u1"})

    # Exhaust t1:u1
    await mw(req_t1_u1, next_fn)
    await mw(req_t1_u1, next_fn)
    with pytest.raises(AIRateLimitError):
        await mw(req_t1_u1, next_fn)

    # t1:u2 is independent and must still succeed
    resp_u2 = await mw(req_t1_u2, next_fn)
    assert resp_u2.content == "ok"

    # t2:u1 has same user_id but different tenant and must succeed
    resp_t2 = await mw(req_t2_u1, next_fn)
    assert resp_t2.content == "ok"


@pytest.mark.asyncio
async def test_rate_limit_fail_closed_policy() -> None:
    """Verify fail_closed policy when Redis storage is offline or unreachable."""
    broken_redis = aioredis.Redis(host="127.0.0.99", port=9999, socket_connect_timeout=0.1)

    # 1. fail_closed=True raises AIRateLimitError
    mw_closed = AIRateLimitMiddleware(
        requests_per_minute=10,
        redis_client=broken_redis,
        storage="redis",
        fail_closed=True,
    )
    req = LLMRequest(messages=[Message(role="user", content="test")])

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok")

    with pytest.raises(AIRateLimitError) as exc_info:
        await mw_closed(req, next_fn)
    assert "Rate limit storage error (fail-closed)" in str(exc_info.value)

    # 2. fail_closed=False fails open
    mw_open = AIRateLimitMiddleware(
        requests_per_minute=10,
        redis_client=broken_redis,
        storage="redis",
        fail_closed=False,
    )
    resp = await mw_open(req, next_fn)
    assert resp.content == "ok"
    await broken_redis.aclose()


@pytest.mark.asyncio
async def test_hard_budget_concurrent_overspending_prevention() -> None:
    """
    Verify hard mode prevents concurrent overspending races via atomic reservation.
    When 10 concurrent requests each request 40 tokens on a remaining budget of 100,
    at most 2 requests can succeed (80 tokens), and the rest are rejected upfront.
    """
    mw = TokenBudgetMiddleware(
        daily_token_limit=100,
        per_user=True,
        mode="hard",
        default_reservation_tokens=40,
    )

    async def next_fn(req: LLMRequest) -> LLMResponse:
        await asyncio.sleep(0.01)
        return LLMResponse(
            content="ok",
            usage=TokenUsage(prompt_tokens=20, completion_tokens=20, total_tokens=40),
        )

    req = LLMRequest(
        messages=[Message(role="user", content="msg")],
        max_tokens=40,
        extra={"user_id": "budget_racer"},
    )

    results: list[LLMResponse | Exception] = []

    async def _call() -> None:
        try:
            resp = await mw(req, next_fn)
            results.append(resp)
        except (AIBudgetExceededError, RuntimeError, OSError) as exc:
            results.append(exc)

    tasks = [asyncio.create_task(_call()) for _ in range(10)]
    await asyncio.gather(*tasks)

    successes = [r for r in results if isinstance(r, LLMResponse)]
    rejections = [r for r in results if isinstance(r, AIBudgetExceededError)]

    # 2 requests * 40 tokens = 80 <= 100. 3rd request would need 120 > 100.
    assert len(successes) == 2, f"Expected 2 passes under hard limit, got {len(successes)}"
    assert len(rejections) == 8, f"Expected 8 budget rejections, got {len(rejections)}"


@pytest.mark.asyncio
async def test_hard_budget_provider_failure_reservation_rollback() -> None:
    """Verify that if the downstream provider throws an exception, reserved tokens are rolled back."""
    mw = TokenBudgetMiddleware(
        daily_token_limit=100,
        per_user=True,
        mode="hard",
        default_reservation_tokens=50,
    )

    async def failing_provider(req: LLMRequest) -> LLMResponse:
        await asyncio.sleep(0.005)
        raise RuntimeError("LLM provider 500 error")

    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        max_tokens=50,
        extra={"user_id": "rollback_user"},
    )

    # Request fails at provider layer
    with pytest.raises(RuntimeError, match="LLM provider 500 error"):
        await mw(req, failing_provider)

    # Usage must be 0 after rollback
    usage = mw.get_current_usage("rollback_user")
    assert usage["tokens"] == 0, f"Expected 0 tokens after rollback, got {usage['tokens']}"

    # A subsequent call can now succeed because budget was not consumed
    async def ok_provider(req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="ok",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )

    resp = await mw(req, ok_provider)
    assert resp.content == "ok"
    usage_after = mw.get_current_usage("rollback_user")
    assert usage_after["tokens"] == 20


@pytest.mark.asyncio
async def test_simultaneous_per_user_and_global_budgets() -> None:
    """Verify per-user and global budgets enforce independently and simultaneously."""
    mw = TokenBudgetMiddleware(
        daily_token_limit=100,  # Each user gets 100
        global_daily_token_limit=150,  # All users combined get 150
        per_user=True,
        mode="soft",
    )

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="ok",
            usage=TokenUsage(prompt_tokens=40, completion_tokens=40, total_tokens=80),
        )

    req_alice = LLMRequest(messages=[Message(role="user", content="a")], extra={"user_id": "alice_budget"})
    req_bob = LLMRequest(messages=[Message(role="user", content="b")], extra={"user_id": "bob_budget"})

    # Alice uses 80 (alice: 80 <= 100, global: 80 <= 150) -> passes
    await mw(req_alice, next_fn)

    # Bob uses 80 (bob: 0 <= 100, global: 80 <= 150) -> passes, global becomes 160
    await mw(req_bob, next_fn)

    # Alice tries again (alice: 80 <= 100, but global: 160 > 150) -> blocked by global budget!
    with pytest.raises(AIBudgetExceededError) as exc_info:
        await mw(req_alice, next_fn)
    assert "Global daily token budget of 150 exceeded" in str(exc_info.value)
