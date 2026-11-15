from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis.asyncio as aioredis

import swiftpy.ai.middleware as mw_mod
from swiftpy.ai._fake import AIFake
from swiftpy.ai.client import AI, AIClient
from swiftpy.ai.conversation import Conversation
from swiftpy.ai.embeddings import EmbeddingsClient
from swiftpy.ai.exceptions import (
    AIBudgetExceededError,
    AIConfigurationError,
    AIContentModerationError,
    AIProviderError,
    AIRateLimitError,
)
from swiftpy.ai.history_strategies import SlidingWindowStrategy, TokenBudgetStrategy
from swiftpy.ai.middleware import (
    AIRateLimitMiddleware,
    ContentModerationMiddleware,
    TokenBudgetMiddleware,
    resolve_tenant_id,
    resolve_user_id,
)
from swiftpy.ai.observability.drivers import (
    AIObservation,
    DatabaseObservabilityDriver,
    InMemoryObservabilityDriver,
    sanitize_metadata,
)
from swiftpy.ai.observability.observer import AIObservabilityManager
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.pricing import calculate_token_cost
from swiftpy.ai.providers.anthropic_provider import AnthropicProvider
from swiftpy.ai.providers.base import (
    LLMRequest,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
)
from swiftpy.ai.providers.openai_provider import OpenAIProvider
from swiftpy.ai.redis_store import InMemoryConversationStore, RedisConversationStore
from swiftpy.ai.streaming import parse_sse_events, split_chunk_lines
from swiftpy.ai.tools.base_tool import BaseTool
from swiftpy.console.commands.new import CONFIG_AI_CONTENT
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.support.redis_config import resolve_redis_params


def _redis_test_config() -> Config:
    return Config(
        {
            "redis": {
                "host": os.environ.get("REDIS_HOST", "127.0.0.1"),
                "port": int(os.environ.get("REDIS_PORT", "6379")),
                "password": os.environ.get("REDIS_PASSWORD"),
                "database": int(os.environ.get("REDIS_DB", "0")),
            }
        }
    )


async def _live_redis_client() -> aioredis.Redis:
    cfg = _redis_test_config()
    params = resolve_redis_params(cfg, section="ai")
    return aioredis.from_url(params["url"], decode_responses=True)


class _DummyTool(BaseTool):
    name = "get_weather"
    description = "Get weather for a city"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        }

    async def handle(self, city: str = "unknown") -> dict[str, Any]:
        return {"city": city, "temp": 22}


# ---------------------------------------------------------------------------
# P7-AI1: Streaming Middleware, Metadata Isolation, & TokenBudgetMiddleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_executes_moderation_and_rate_limit_middlewares(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    provider_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        body = (
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    pool = AIHttpPool(client=mock_client_factory(handler))
    cfg = Config({"ai": {"providers": {"openai": {"api_key": "sk-test"}}}})

    moderation = ContentModerationMiddleware(banned_words=["forbidden"])
    blocked_client = (
        AIClient(config=cfg, http_pool=pool, default_provider="openai")
        .use(moderation)
        .user("contains forbidden phrase")
    )
    with pytest.raises(AIContentModerationError):
        async for _ in blocked_client.stream():
            pass
    assert provider_calls == 0

    rate_limiter = AIRateLimitMiddleware(requests_per_minute=1, storage="memory")
    AIClient.use_global_middleware(rate_limiter)
    try:
        ok_client = (
            AIClient(config=cfg, http_pool=pool, default_provider="openai")
            .metadata(user_id="stream-rl-user")
            .user("first request")
        )
        chunks = [c async for c in ok_client.stream()]
        assert "".join(chunks) == "Hello"
        assert provider_calls == 1

        second_client = (
            AIClient(config=cfg, http_pool=pool, default_provider="openai")
            .metadata(user_id="stream-rl-user")
            .user("second request")
        )
        with pytest.raises(AIRateLimitError):
            async for _ in second_client.stream():
                pass
        assert provider_calls == 1
    finally:
        AIClient.clear_global_middlewares()


@pytest.mark.asyncio
async def test_stream_token_budget_middleware_reconciles_usage(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            b'data: {"choices":[{"delta":{"content":"Alpha Beta Gamma"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    pool = AIHttpPool(client=mock_client_factory(handler))
    cfg = Config({"ai": {"providers": {"openai": {"api_key": "sk-test"}}}})
    budget = TokenBudgetMiddleware(
        daily_token_limit=20,
        per_user=True,
        mode="hard",
        default_reservation_tokens=10,
        storage="memory",
    )

    client1 = (
        AIClient(config=cfg, http_pool=pool, default_provider="openai")
        .use(budget)
        .metadata(user_id="stream-budget-user")
        .user("12345678")
    )
    chunks = [c async for c in client1.stream()]
    assert "".join(chunks) == "Alpha Beta Gamma"

    usage = budget.get_current_usage("stream-budget-user")
    assert usage["tokens"] > 0

    client2 = (
        AIClient(config=cfg, http_pool=pool, default_provider="openai")
        .use(budget)
        .metadata(user_id="stream-budget-user")
        .user("x" * 100)
    )
    with pytest.raises(AIBudgetExceededError):
        async for _ in client2.stream():
            pass


@pytest.mark.asyncio
async def test_token_budget_middleware_redis_tenant_and_global_isolation() -> None:
    redis_client = await _live_redis_client()
    prefix = "ai:test:p7budget"
    try:
        mw1 = TokenBudgetMiddleware(
            daily_token_limit=100,
            global_daily_token_limit=250,
            per_user=True,
            mode="hard",
            default_reservation_tokens=10,
            redis_client=redis_client,
            storage="redis",
            key_prefix=prefix,
        )
        await mw1.reset_backend()

        mw2 = TokenBudgetMiddleware(
            daily_token_limit=100,
            global_daily_token_limit=250,
            per_user=True,
            mode="hard",
            default_reservation_tokens=10,
            redis_client=redis_client,
            storage="redis",
            key_prefix=prefix,
        )

        async def next_fn(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="ok",
                model="gpt-4o",
                usage=TokenUsage(prompt_tokens=30, completion_tokens=30, total_tokens=60),
            )

        req_t1 = LLMRequest(
            messages=[Message(role="user", content="hi")],
            metadata={"user_id": "shared-user", "tenant_id": "tenant-a"},
        )
        req_t2 = LLMRequest(
            messages=[Message(role="user", content="hi")],
            metadata={"user_id": "shared-user", "tenant_id": "tenant-b"},
        )
        req_global_literal = LLMRequest(
            messages=[Message(role="user", content="hi")],
            metadata={"user_id": "__global__", "tenant_id": "tenant-c"},
        )

        await mw1(req_t1, next_fn)
        # Second instance reading Redis sees tenant-a already spent 60/100; another 60 exceeds 100 on hard/post? Wait:
        # Call again on tenant-b with same user_id -> must be isolated and allowed!
        await mw2(req_t2, next_fn)
        # Calling second time on tenant-a via mw2 (60 already spent; next call reserve >= 11 => allowed or total 120):
        await mw2(req_t1, next_fn)
        with pytest.raises(AIBudgetExceededError):
            await mw2(req_t1, next_fn)

        # User literally named "__global__" should NOT double-count "__global__" bucket (60+60+60=180 <= 250)
        await mw2(req_global_literal, next_fn)
    finally:
        await mw1.reset_backend()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_attribution_metadata_isolated_from_wire_payloads(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    captured_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode("utf-8"))
        captured_payloads.append(payload)
        if "anthropic" in str(request.url):
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "anthropic ok"}], "model": "claude-3-5-sonnet-20241022"},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "openai ok"}}], "model": "gpt-4o"},
        )

    pool = AIHttpPool(client=mock_client_factory(handler))
    cfg = Config(
        {
            "ai": {
                "providers": {
                    "openai": {"api_key": "sk-test-oai"},
                    "anthropic": {"api_key": "sk-ant-test"},
                }
            }
        }
    )

    observed_attribution: list[tuple[str, str]] = []

    async def capture_mw(
        req: LLMRequest,
        nxt: Callable[[LLMRequest], Awaitable[LLMResponse]],
    ) -> LLMResponse:
        observed_attribution.append((resolve_user_id(req), resolve_tenant_id(req)))
        return await nxt(req)

    await (
        AIClient(config=cfg, http_pool=pool, default_provider="openai")
        .use(capture_mw)
        .metadata(user_id="meta-u", tenant_id="meta-t")
        .extra(user="legacy-u", tenant="legacy-t", seed=42)
        .user("Ping OpenAI")
        .send()
    )
    await (
        AIClient(config=cfg, http_pool=pool, default_provider="anthropic")
        .use(capture_mw)
        .extra(user_id="extra-u", tenant_id="extra-t", top_k=5)
        .user("Ping Anthropic")
        .send()
    )

    assert observed_attribution == [("meta-u", "meta-t"), ("extra-u", "extra-t")]
    assert len(captured_payloads) == 2

    oai_payload, ant_payload = captured_payloads
    for forbidden in ("user_id", "tenant_id", "user", "tenant"):
        assert forbidden not in oai_payload
        assert forbidden not in ant_payload
    assert oai_payload.get("seed") == 42
    assert ant_payload.get("top_k") == 5


# ---------------------------------------------------------------------------
# P7-AI2: Embeddings Fail Closed, Secret Redaction, & Longest-Prefix Pricing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embeddings_fail_closed_on_empty_api_key() -> None:
    cfg = Config({"ai": {"enabled": True, "default": "openai", "providers": {"openai": {"api_key": ""}}}})
    client = EmbeddingsClient(config=cfg)

    with pytest.raises(AIConfigurationError, match="Missing API key"):
        await client.embed("hello world")

    with pytest.raises(AIConfigurationError, match="Missing API key"):
        await client.embed_batch(["a", "b"])

    # Deterministic explicit provider and AIFake still succeed without API key
    det_cfg = Config({"ai": {"enabled": True, "default": "deterministic"}})
    det_vec = await EmbeddingsClient(config=det_cfg).embed("hello")
    assert len(det_vec) == 1536

    with AIFake():
        fake_vec = await client.embed("hello")
        assert len(fake_vec) == 1536


@pytest.mark.asyncio
async def test_secret_redaction_in_embeddings_and_observability(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    secret_key = "sk-proj-supersecretkey1234567890abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"Bad key provided: {secret_key}")

    pool = AIHttpPool(client=mock_client_factory(handler))
    cfg = Config(
        {
            "ai": {
                "enabled": True,
                "default": "openai",
                "providers": {"openai": {"api_key": secret_key}},
            }
        }
    )
    emb = EmbeddingsClient(config=cfg, http_pool=pool)
    with pytest.raises(Exception) as exc_info:
        await emb.embed("test secret")
    assert secret_key not in str(exc_info.value)

    obs = AIObservation(
        id="obs-1",
        timestamp=datetime.now(UTC),
        model="gpt-4o",
        provider="openai",
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        latency_ms=1.0,
        cost=0.0,
        prompt_hash="abc",
        status="error",
        error_message=f"Failed with {secret_key}",
        metadata={
            "x-api-key": "anthropic-secret",
            "headers": [{"Authorization": "Bearer secret-jwt"}, {"x-api-key": "secret-2"}],
        },
    )
    assert secret_key not in (obs.error_message or "")
    sanitized = sanitize_metadata(obs.metadata)
    assert sanitized["x-api-key"] == "[REDACTED]"
    assert sanitized["headers"][0]["Authorization"] == "[REDACTED]"
    assert sanitized["headers"][1]["x-api-key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_longest_prefix_pricing_and_claude_sonnet_4_6() -> None:
    cost_mini = calculate_token_cost("gpt-4o-mini-2024-07-18", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost_mini == round(0.15 + 0.60, 6)

    cost_o1_mini = calculate_token_cost("o1-mini-2024-09-12", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost_o1_mini == round(3.00 + 12.00, 6)

    cost_sonnet_46 = calculate_token_cost("claude-sonnet-4-6", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost_sonnet_46 == round(3.00 + 15.00, 6)

    # AIFake mapping fixtures should match longest prefix cleanly & delegate cost to pricing.py
    obs_driver = InMemoryObservabilityDriver()
    old_obs = AIObservabilityManager._instance
    AIObservabilityManager.set_instance(AIObservabilityManager(drivers=[obs_driver]))
    try:
        with AIFake({"gpt-4o": "wrong-match", "gpt-4o-mini": "mini-match"}):
            resp = await AI.chat("gpt-4o-mini-2024-07-18").user("hi").send()
            assert resp.content == "mini-match"
    finally:
        AIObservabilityManager.set_instance(old_obs)


# ---------------------------------------------------------------------------
# P7-AI3: Providers, SSE, Conversation, Stores, Defaults, Pool, Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_http_date_retry_after_and_nondict_error(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    retry_date = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
    call_idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": retry_date},
                json={"error": "rate_limit_string_payload"},
            )
        return httpx.Response(500, json={"error": "internal_string_error"})

    pool = AIHttpPool(client=mock_client_factory(handler))
    provider = OpenAIProvider(config={"api_key": "sk-test", "max_retries": 0}, http_pool=pool)

    with pytest.raises(AIRateLimitError) as rl_exc:
        await provider.send(LLMRequest(messages=[Message(role="user", content="hi")]))
    assert rl_exc.value.retry_after is not None and rl_exc.value.retry_after >= 0.0
    assert "rate_limit_string_payload" in str(rl_exc.value)

    with pytest.raises(AIProviderError) as err_exc:
        await provider.send(LLMRequest(messages=[Message(role="user", content="hi")]))
    assert "internal_string_error" in str(err_exc.value)


@pytest.mark.asyncio
async def test_tool_call_nondict_arguments_handled_safely(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    bad_args: Any = ["unexpected"]
    tc = ToolCall(id="call_1", name="get_weather", arguments=bad_args)
    serialized = tc.to_dict()
    assert isinstance(serialized["function"]["arguments"], str)

    captured_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.read().decode("utf-8")))
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "done"}], "model": "claude-3-5-sonnet-20241022"},
        )

    pool = AIHttpPool(client=mock_client_factory(handler))
    ant = AnthropicProvider(config={"api_key": "sk-ant-test"}, http_pool=pool)
    await ant.send(
        LLMRequest(
            messages=[
                Message(role="user", content="weather"),
                Message(role="assistant", content="", tool_calls=[tc]),
                Message(role="tool", content='{"temp": 20}', tool_call_id="call_1"),
            ]
        )
    )
    tool_use_block = captured_payloads[0]["messages"][1]["content"][0]
    assert isinstance(tool_use_block["input"], dict)


@pytest.mark.asyncio
async def test_anthropic_stream_forwards_tools_and_tool_messages(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read().decode("utf-8")))
        sse = (
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"22C"}}\n\n'
            b'event: message_stop\n'
            b'data: {"type":"message_stop"}\n\n'
        )
        return httpx.Response(200, content=sse, headers={"Content-Type": "text/event-stream"})

    pool = AIHttpPool(client=mock_client_factory(handler))
    provider = AnthropicProvider(config={"api_key": "sk-ant-test"}, http_pool=pool)
    req = LLMRequest(
        messages=[
            Message(role="user", content="Weather?"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="tu_1", name="get_weather", arguments={"city": "Paris"})],
            ),
            Message(role="tool", content='{"temp": 22}', tool_call_id="tu_1"),
        ],
        tools=[_DummyTool().to_dict()],
    )
    chunks = [c async for c in provider.stream(req)]
    assert "".join(chunks) == "22C"
    assert len(captured) == 1
    sent = captured[0]
    assert sent["stream"] is True
    assert len(sent["tools"]) == 1
    assert "input_schema" in sent["tools"][0]
    assert sent["messages"][1]["content"][0]["type"] == "tool_use"
    assert sent["messages"][2]["content"][0]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_sse_null_error_and_split_utf8_chunks() -> None:
    async def sse_lines() -> AsyncIterator[str]:
        yield 'data: {"choices":[{"delta":{"content":"ok"}}],"error":null}\n'
        yield "\n"
        yield "data: [DONE]\n"
        yield "\n"

    events = [ev async for ev in parse_sse_events(sse_lines())]
    assert len(events) == 1
    assert events[0]["data"]["choices"][0]["delta"]["content"] == "ok"

    async def byte_chunks() -> AsyncIterator[bytes]:
        raw = "Hello 🚀\nWorld".encode()
        yield raw[:8]
        yield raw[8:]

    lines = [line async for line in split_chunk_lines(byte_chunks())]
    assert lines == ["Hello 🚀", "World"]


def test_conversation_preserves_tool_calls_and_strategies_drop_orphaned_tool_msgs() -> None:
    tc = ToolCall(id="call_abc", name="get_weather", arguments={"city": "Berlin"})
    conv = Conversation()
    conv.add_system("Sys")
    conv.add_user("Question 1")
    conv.add_message(Message(role="assistant", content="", tool_calls=[tc]))
    conv.add_message(Message(role="tool", content='{"temp": 18}', tool_call_id="call_abc"))
    conv.add_assistant("It is 18C.")
    conv.add_user("Question 2")
    conv.add_assistant("Answer 2")

    cloned = conv.clone()
    assert cloned[2].tool_calls is not None and cloned[2].tool_calls[0].name == "get_weather"
    assert cloned[3].tool_call_id == "call_abc"

    reloaded = Conversation.from_json(conv.to_json())
    assert len(reloaded) == len(conv)
    assert reloaded[2].tool_calls is not None and reloaded[2].tool_calls[0].arguments == {"city": "Berlin"}
    assert reloaded[3].tool_call_id == "call_abc"

    # Window that cuts right before orphaned 'tool' message must drop the orphan 'tool' too!
    window = SlidingWindowStrategy(max_messages=5).apply(conv)
    assert [m.role for m in window.messages] == ["system", "user", "assistant"]

    # TokenBudgetStrategy also drops orphaned leading tool messages cleanly
    budget = TokenBudgetStrategy(max_tokens=30).apply(conv)
    assert budget.dialogue_messages[0].role == "user"


@pytest.mark.asyncio
async def test_attached_conversation_not_mutated_on_failed_calls(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "server down"}})

    pool = AIHttpPool(client=mock_client_factory(failing_handler))
    cfg = Config({"ai": {"providers": {"openai": {"api_key": "sk-test", "max_retries": 0}}}})

    conv = Conversation().add_system("Sys")
    with pytest.raises(AIProviderError):
        await (
            AIClient(config=cfg, http_pool=pool, default_provider="openai")
            .conversation(conv)
            .user("Failed send turn")
            .send()
        )
    assert len(conv) == 1

    with pytest.raises(AIProviderError):
        async for _ in (
            AIClient(config=cfg, http_pool=pool, default_provider="openai")
            .conversation(conv)
            .user("Failed stream turn")
            .stream()
        ):
            pass
    assert len(conv) == 1


@pytest.mark.asyncio
async def test_stores_ttl_bounds_and_redis_params() -> None:
    mem_store = InMemoryConversationStore(max_entries=2)
    c1 = Conversation().add_user("u1")
    c2 = Conversation().add_user("u2")
    c3 = Conversation().add_user("u3")

    await mem_store.save("s1", c1)
    await mem_store.save("s2", c2)
    await mem_store.save("s3", c3)
    assert not await mem_store.exists("s1")  # LRU evicted oldest
    assert await mem_store.exists("s3")

    # Negative TTL deletes / expires immediately
    await mem_store.save("s3", c3, ttl=-1)
    assert not await mem_store.exists("s3")
    assert len(await mem_store.load("s3")) == 0

    # RedisConversationStore honors resolve_redis_params database/url/password/ssl/username
    redis_cfg = _redis_test_config()
    params = resolve_redis_params(redis_cfg, section="ai")
    r_store = RedisConversationStore(config=redis_cfg, key_prefix="ai:test:p7store")
    client = await r_store.get_client()
    assert isinstance(client, aioredis.Redis)
    try:
        conn_kwargs = client.connection_pool.connection_kwargs
        assert conn_kwargs.get("db", 0) == params["database"]
        await r_store.save("perm", c1, ttl=-5)
        assert not await r_store.exists("perm")
    finally:
        await r_store.close()


@pytest.mark.asyncio
async def test_resolved_model_provider_new_config_empty_stream_retries_and_embeddings_timeout(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    seen_in_mw: list[tuple[str | None, str | None]] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
        if attempts == 1:
            return httpx.Response(503, json={"error": {"message": "temporary"}})
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "recovered"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            },
        )

    pool = AIHttpPool(client=mock_client_factory(handler))
    cfg = Config(
        {
            "ai": {
                "default": "openai",
                "providers": {
                    "openai": {"api_key": "sk-test", "model": "gpt-4o-mini", "max_retries": 1, "timeout": 12.5}
                },
            }
        }
    )

    obs_driver = InMemoryObservabilityDriver()
    old_obs = AIObservabilityManager._instance
    AIObservabilityManager.set_instance(AIObservabilityManager(drivers=[obs_driver]))
    try:
        async def check_mw(
            req: LLMRequest,
            nxt: Callable[[LLMRequest], Awaitable[LLMResponse]],
        ) -> LLMResponse:
            seen_in_mw.append((req.model, str(req.metadata.get("provider"))))
            return await nxt(req)

        resp = await AIClient(config=cfg, http_pool=pool).use(check_mw).user("hello").send()
        assert resp.content == "recovered"
        assert attempts == 2
        assert seen_in_mw[0][0] == "gpt-4o-mini"

        records = await obs_driver.get_observations(limit=5)
        assert records[0].provider == "openai"
        assert records[0].model == "gpt-4o-mini"

        # 0-byte failed stream must record output_tokens=0 (not 1)
        def err_stream_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "fail"}})

        err_pool = AIHttpPool(client=mock_client_factory(err_stream_handler))
        err_cfg = Config({"ai": {"default": "openai", "providers": {"openai": {"api_key": "sk-test", "max_retries": 0}}}})
        with pytest.raises(AIProviderError):
            async for _ in AIClient(config=err_cfg, http_pool=err_pool).user("hello").stream():
                pass
        records2 = await obs_driver.get_observations(limit=1)
        assert records2[0].output_tokens == 0
        assert records2[0].provider == "openai"
    finally:
        AIObservabilityManager.set_instance(old_obs)

    # Verify new.py config/ai.py uses "default" and "model" keys
    assert '"default":' in CONFIG_AI_CONTENT
    assert '"model":' in CONFIG_AI_CONTENT
    assert '"default_provider":' not in CONFIG_AI_CONTENT
    assert '"default_model":' not in CONFIG_AI_CONTENT

    # Verify EmbeddingsClient resolves configured timeout instead of hardcoded 60.0
    observed_timeouts: list[Any] = []

    def emb_handler(request: httpx.Request) -> httpx.Response:
        observed_timeouts.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5, 0.5]}]})

    emb_client = EmbeddingsClient(config=cfg, http_pool=AIHttpPool(client=mock_client_factory(emb_handler)))
    await emb_client.embed("check timeout")
    timeout_ext = observed_timeouts[0]
    assert timeout_ext is not None
    assert timeout_ext.get("read") == 12.5


@pytest.mark.asyncio
async def test_pool_ownership_app_shutdown_observer_env_fake_lists_and_bool_guards(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    custom_http = mock_client_factory(lambda r: httpx.Response(200, json={}))
    custom_pool = AIHttpPool(client=custom_http)
    await custom_pool.aclose()
    assert not custom_http.is_closed

    owned_pool = AIHttpPool()
    owned_client = owned_pool.get_client()
    app = Application(base_path=Path.cwd())
    app.container.instance(AIHttpPool, owned_pool)
    await app.shutdown()
    assert owned_client.is_closed

    # Observer driver selection must rely on config/app.env, never "pytest" in sys.modules
    assert "pytest" in sys.modules
    local_cfg = Config({"app": {"env": "local"}})
    mgr = AIObservabilityManager(config=local_cfg)
    assert isinstance(mgr.drivers[0], DatabaseObservabilityDriver)

    # AIFake list embedding fixtures must not truncate vectors below len(texts)
    with AIFake(embedding_fixtures=[[0.1, 0.2]]):
        vecs = await EmbeddingsClient(config=Config({"ai": {"providers": {"openai": {"api_key": "k"}}}})).embed_batch(
            ["text 1", "text 2", "text 3"]
        )
        assert len(vecs) == 3

    # Numeric hyperparameters reject booleans
    untyped_client: Any = AIClient(config=Config({"ai": {}}))
    with pytest.raises((TypeError, ValueError)):
        untyped_client.temperature(True)
    with pytest.raises((TypeError, ValueError)):
        untyped_client.max_tokens(True)
    with pytest.raises((TypeError, ValueError)):
        untyped_client.top_p(False)
    with pytest.raises((TypeError, ValueError)):
        untyped_client.auto_execute_tools(max_iterations=True)


@pytest.mark.asyncio
async def test_redis_reset_uses_scan_not_keys_and_driver_no_implicit_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_keys: list[str] = []

    class _ScanOnlyRedis:
        async def keys(self, _pattern: str) -> list[str]:
            raise AssertionError("Blocking KEYS command must never be called")

        async def scan_iter(self, match: str, count: int = 200) -> AsyncIterator[str]:
            _ = count
            yield f"{match.rstrip('*')}sample"

        async def delete(self, *keys: str) -> int:
            deleted_keys.extend(keys)
            return len(keys)

        async def aclose(self) -> None:
            return None

    fake_redis: Any = _ScanOnlyRedis()
    budget = TokenBudgetMiddleware(daily_token_limit=100, redis_client=fake_redis, storage="redis", key_prefix="ai:p7scan")
    rate_limit = AIRateLimitMiddleware(requests_per_minute=10, redis_client=fake_redis, storage="redis", key_prefix="ai:p7rl")

    await budget.reset_backend()
    await rate_limit.reset_backend()
    assert any(k.startswith("ai:p7scan") for k in deleted_keys)
    assert any(k.startswith("ai:p7rl") for k in deleted_keys)

    # DatabaseObservabilityDriver provides explicit DDL and does not invoke ensure_table implicitly
    driver = DatabaseObservabilityDriver(table_name="ai_observations_no_implicit_ddl")
    ddl = driver.get_ddl()
    assert "CREATE TABLE" in ddl.upper()
    assert "ai_observations_no_implicit_ddl" in ddl

    async def forbidden_ensure_table() -> None:
        raise AssertionError("ensure_table() must not run implicitly during record/query/clear")

    monkeypatch.setattr(driver, "ensure_table", forbidden_ensure_table)

    obs = AIObservation(
        id="obs-no-ddl",
        timestamp=datetime.now(UTC),
        model="gpt-4o",
        provider="openai",
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        latency_ms=1.0,
        cost=0.0,
        prompt_hash="abc",
        status="success",
    )
    # Without explicit ensure_table(), Postgres rejects missing relation instead of implicit DDL
    with pytest.raises(Exception) as record_exc:
        await driver.record(obs)
    assert not isinstance(record_exc.value, AssertionError)


@pytest.mark.asyncio
async def test_token_budget_prunes_stale_date_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_now = [datetime(2099, 1, 1, 12, 0, tzinfo=UTC)]

    class _FakeDatetime:
        @staticmethod
        def now(_tz: Any = None) -> datetime:
            return current_now[0]

    monkeypatch.setattr(mw_mod, "datetime", _FakeDatetime)
    budget = TokenBudgetMiddleware(daily_token_limit=100, per_user=True, storage="memory")

    async def next_fn(_req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="ok",
            model="gpt-4o",
            usage=TokenUsage(prompt_tokens=20, completion_tokens=20, total_tokens=40),
        )

    await budget(LLMRequest(messages=[Message(role="user", content="hi")], metadata={"user_id": "u-day"}), next_fn)
    assert budget.get_current_usage("u-day")["tokens"] == 40

    # Advance to next UTC day: old bucket should be pruned when new day executes
    current_now[0] = datetime(2099, 1, 2, 12, 0, tzinfo=UTC)
    await budget(LLMRequest(messages=[Message(role="user", content="hi")], metadata={"user_id": "u-day2"}), next_fn)
    assert budget.get_current_usage("u-day")["tokens"] == 0
    assert budget.get_current_usage("u-day2")["tokens"] == 40
