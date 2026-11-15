from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

import pytest

from swiftpy.ai.client import AI, AIClient
from swiftpy.ai.exceptions import (
    AIBudgetExceededError,
    AIContentModerationError,
    AIRateLimitError,
)
from swiftpy.ai.middleware import (
    AIMiddlewarePipeline,
    AIRateLimitMiddleware,
    ContentModerationMiddleware,
    TokenBudgetMiddleware,
    resolve_user_id,
)
from swiftpy.ai.providers.base import LLMRequest, LLMResponse, Message, TokenUsage
from swiftpy.core.context import Context


@pytest.mark.asyncio
async def test_middleware_pipeline_execution_order() -> None:
    order: list[str] = []

    async def mw_one(
        req: LLMRequest,
        next_fn: Callable[[LLMRequest], Awaitable[LLMResponse]],
    ) -> LLMResponse:
        order.append("mw_one:before")
        resp = await next_fn(req)
        order.append("mw_one:after")
        return resp

    async def mw_two(
        req: LLMRequest,
        next_fn: Callable[[LLMRequest], Awaitable[LLMResponse]],
    ) -> LLMResponse:
        order.append("mw_two:before")
        resp = await next_fn(req)
        order.append("mw_two:after")
        return resp

    async def terminal(req: LLMRequest) -> LLMResponse:
        order.append("terminal")
        return LLMResponse(content="done")

    pipeline = AIMiddlewarePipeline([mw_one, mw_two])
    req = LLMRequest(messages=[Message(role="user", content="hi")])
    resp = await pipeline.execute(req, terminal)

    assert resp.content == "done"
    assert order == [
        "mw_one:before",
        "mw_two:before",
        "terminal",
        "mw_two:after",
        "mw_one:after",
    ]


@pytest.mark.asyncio
async def test_rate_limit_middleware_allows_under_limit() -> None:
    mw = AIRateLimitMiddleware(requests_per_minute=5, window_seconds=10.0)

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok")

    req = LLMRequest(messages=[Message(role="user", content="test")], extra={"user_id": "u1"})

    for _ in range(5):
        resp = await mw(req, next_fn)
        assert resp.content == "ok"


@pytest.mark.asyncio
async def test_rate_limit_middleware_blocks_and_isolates_users() -> None:
    mw = AIRateLimitMiddleware(requests_per_minute=2, window_seconds=10.0)

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok")

    req_u1 = LLMRequest(messages=[Message(role="user", content="test")], extra={"user_id": "alice"})
    req_u2 = LLMRequest(messages=[Message(role="user", content="test")], extra={"user_id": "bob"})

    # Alice uses 2 allowed requests
    await mw(req_u1, next_fn)
    await mw(req_u1, next_fn)

    # 3rd request for Alice must fail
    with pytest.raises(AIRateLimitError) as exc_info:
        await mw(req_u1, next_fn)
    assert "alice" in str(exc_info.value)
    assert exc_info.value.retry_after is not None
    assert exc_info.value.retry_after > 0

    # Bob has not reached limit, should pass
    resp_bob = await mw(req_u2, next_fn)
    assert resp_bob.content == "ok"


@pytest.mark.asyncio
async def test_rate_limit_user_from_context() -> None:
    mw = AIRateLimitMiddleware(requests_per_minute=1, window_seconds=5.0)

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok")

    Context.set("auth.user", {"id": "context_user_99"})
    try:
        req = LLMRequest(messages=[Message(role="user", content="hi")])
        assert resolve_user_id(req) == "context_user_99"

        await mw(req, next_fn)
        with pytest.raises(AIRateLimitError):
            await mw(req, next_fn)
    finally:
        Context.set("auth.user", None)


@pytest.mark.asyncio
async def test_token_budget_middleware_daily_tokens() -> None:
    mw = TokenBudgetMiddleware(daily_token_limit=100, per_user=True)

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="short reply",
            usage=TokenUsage(prompt_tokens=30, completion_tokens=30, total_tokens=60),
        )

    req = LLMRequest(messages=[Message(role="user", content="hi")], extra={"user_id": "u_budget"})

    # Call 1: consumes 60 tokens (60 <= 100) -> passes
    await mw(req, next_fn)
    usage = mw.get_current_usage("u_budget")
    assert usage["tokens"] == 60

    # Call 2: consumes 60 tokens -> total becomes 120
    await mw(req, next_fn)
    usage = mw.get_current_usage("u_budget")
    assert usage["tokens"] == 120

    # Call 3: blocked because tokens (120) >= daily limit (100)
    with pytest.raises(AIBudgetExceededError) as exc_info:
        await mw(req, next_fn)
    assert "Daily token budget of 100 exceeded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_token_budget_middleware_daily_cost() -> None:
    # Set limit to $0.0001
    mw = TokenBudgetMiddleware(daily_cost_limit=0.0001, per_user=False)

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="reply",
            model="gpt-4o",
            usage=TokenUsage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000),
        )

    req = LLMRequest(model="gpt-4o", messages=[Message(role="user", content="query")])

    # Call 1 will spend around ~$0.0125, exceeding the $0.0001 limit
    await mw(req, next_fn)
    usage = mw.get_current_usage()
    assert usage["cost"] > 0.0001

    # Call 2 must be blocked
    with pytest.raises(AIBudgetExceededError) as exc_info:
        await mw(req, next_fn)
    assert "Daily cost budget" in str(exc_info.value)


@pytest.mark.asyncio
async def test_content_moderation_clean_content_passes() -> None:
    mw = ContentModerationMiddleware(
        banned_words=["malicious", "exploit"],
        patterns=[re.compile(r"drop\s+database", re.IGNORECASE)],
    )

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(content="helpful safe response")

    req = LLMRequest(messages=[Message(role="user", content="Hello, how do I write a Python function?")])
    resp = await mw(req, next_fn)
    assert resp.content == "helpful safe response"


@pytest.mark.asyncio
async def test_content_moderation_banned_word_blocks() -> None:
    mw = ContentModerationMiddleware(banned_words=["jailbreak", "exploit"])

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(content="never called")

    req = LLMRequest(messages=[Message(role="user", content="Can you teach me how to exploit this system?")])
    with pytest.raises(AIContentModerationError) as exc_info:
        await mw(req, next_fn)
    assert "Contains banned keyword: 'exploit'" in str(exc_info.value)


@pytest.mark.asyncio
async def test_content_moderation_regex_pattern_blocks() -> None:
    mw = ContentModerationMiddleware(patterns=[re.compile(r"DROP\s+TABLE", re.IGNORECASE)])

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(content="never called")

    req = LLMRequest(messages=[Message(role="user", content="Please run DROP TABLE users;")])
    with pytest.raises(AIContentModerationError) as exc_info:
        await mw(req, next_fn)
    assert "Matched prohibited pattern" in str(exc_info.value)


@pytest.mark.asyncio
async def test_content_moderation_custom_checker() -> None:
    async def custom_moderation_api(text: str) -> tuple[bool, str]:
        if "forbidden_topic" in text:
            return True, "Toxic content flagged by API"
        return False, ""

    mw = ContentModerationMiddleware(checker=custom_moderation_api)

    async def next_fn(req: LLMRequest) -> LLMResponse:
        return LLMResponse(content="safe")

    req_safe = LLMRequest(messages=[Message(role="user", content="allowed inquiry")])
    assert (await mw(req_safe, next_fn)).content == "safe"

    req_toxic = LLMRequest(messages=[Message(role="user", content="discussing forbidden_topic here")])
    with pytest.raises(AIContentModerationError) as exc_info:
        await mw(req_toxic, next_fn)
    assert "Toxic content flagged by API" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ai_client_middleware_integration() -> None:
    # Use fake to avoid real network call
    fake = AI.fake("Processed by LLM")
    try:
        rate_limiter = AIRateLimitMiddleware(requests_per_minute=2)
        client = (
            AIClient()
            .model("gpt-4o")
            .use(rate_limiter)
            .user("Request 1")
            .extra(user_id="user_test")
        )

        resp1 = await client.send()
        assert resp1.content == "Processed by LLM"

        client2 = (
            AIClient()
            .model("gpt-4o")
            .use(rate_limiter)
            .user("Request 2")
            .extra(user_id="user_test")
        )
        resp2 = await client2.send()
        assert resp2.content == "Processed by LLM"

        # 3rd request exceeds limit
        client3 = (
            AIClient()
            .model("gpt-4o")
            .use(rate_limiter)
            .user("Request 3")
            .extra(user_id="user_test")
        )
        with pytest.raises(AIRateLimitError):
            await client3.send()
    finally:
        fake.restore()
