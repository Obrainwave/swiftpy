from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

try:
    import httpx
except ImportError:
    httpx = None

import pytest

from swiftpy.ai.exceptions import (
    AIAuthenticationError,
    AIConnectionError,
    AIProviderError,
    AIRateLimitError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.providers.anthropic_provider import AnthropicProvider
from swiftpy.ai.providers.base import LLMRequest, Message
from swiftpy.ai.providers.openai_provider import OpenAIProvider
from swiftpy.core.config import Config

# ---------------------------------------------------------------------------
# OpenAI Provider Tests
# ---------------------------------------------------------------------------


async def test_openai_payload_mapping_and_response(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "created": 1677652288,
                "model": "gpt-4o-2024-05-13",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Hello! I am OpenAI.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 6,
                    "total_tokens": 18,
                },
            },
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = OpenAIProvider(
        config={
            "api_key": "sk-proj-testkey12345",
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1",
        },
        http_pool=pool,
    )

    req = LLMRequest(
        messages=[
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hello!"),
        ],
        temperature=0.7,
        max_tokens=100,
        top_p=0.95,
        stop=["STOP"],
    )

    resp = await provider.send(req)

    assert resp.content == "Hello! I am OpenAI."
    assert resp.role == "assistant"
    assert resp.model == "gpt-4o-2024-05-13"
    assert resp.usage.prompt_tokens == 12
    assert resp.usage.completion_tokens == 6
    assert resp.usage.total_tokens == 18
    assert resp.finish_reason == "stop"
    assert resp.latency_ms >= 0.0

    assert len(captured_requests) == 1
    sent_req = captured_requests[0]
    assert sent_req.url == "https://api.openai.com/v1/chat/completions"
    assert sent_req.headers["Authorization"] == "Bearer sk-proj-testkey12345"
    assert sent_req.headers["Content-Type"] == "application/json"

    body = json.loads(sent_req.content.decode("utf-8"))
    assert body["model"] == "gpt-4o"
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 100
    assert body["top_p"] == 0.95
    assert body["stop"] == ["STOP"]
    assert len(body["messages"]) == 2


async def test_openai_omits_optional_parameters_when_none(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=pool)
    # No temperature, max_tokens, top_p, or stop specified
    req = LLMRequest(messages=[Message(role="user", content="Hello")])
    await provider.send(req)

    body = json.loads(captured_requests[0].content.decode("utf-8"))
    assert "temperature" not in body
    assert "max_tokens" not in body
    assert "top_p" not in body
    assert "stop" not in body


async def test_openai_preserves_multiple_messages_in_order(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=pool)
    req = LLMRequest(
        messages=[
            Message(role="system", content="Sys 1"),
            Message(role="system", content="Sys 2"),
            Message(role="user", content="User 1"),
            Message(role="assistant", content="Assistant 1"),
            Message(role="user", content="User 2"),
        ]
    )
    await provider.send(req)

    body = json.loads(captured_requests[0].content.decode("utf-8"))
    messages = body["messages"]
    assert len(messages) == 5
    assert messages[0] == {"role": "system", "content": "Sys 1"}
    assert messages[1] == {"role": "system", "content": "Sys 2"}
    assert messages[2] == {"role": "user", "content": "User 1"}
    assert messages[3] == {"role": "assistant", "content": "Assistant 1"}
    assert messages[4] == {"role": "user", "content": "User 2"}


async def test_openai_empty_choices_raises_provider_error(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=pool)
    req = LLMRequest(messages=[Message(role="user", content="Hi")])

    with pytest.raises(AIProviderError, match="no choices"):
        await provider.send(req)


async def test_openai_missing_and_malformed_usage(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Response with missing usage object entirely
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=pool)
    req = LLMRequest(messages=[Message(role="user", content="Hi")])
    resp = await provider.send(req)

    assert resp.usage.prompt_tokens == 0
    assert resp.usage.completion_tokens == 0
    assert resp.usage.total_tokens == 0


async def test_openai_empty_choice_content(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=pool)
    req = LLMRequest(messages=[Message(role="user", content="Hi")])
    resp = await provider.send(req)

    assert resp.content == ""


async def test_openai_non_json_error_response(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            content=b"<html><body>502 Bad Gateway from Cloudflare</body></html>",
            headers={"Content-Type": "text/html"},
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=pool)
    req = LLMRequest(messages=[Message(role="user", content="Hi")])

    with pytest.raises(AIProviderError) as exc_info:
        await provider.send(req)

    assert exc_info.value.status_code == 502
    assert "502 Bad Gateway" in str(exc_info.value)


async def test_openai_error_status_codes(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    for status, exc_type in [
        (400, AIProviderError),
        (401, AIAuthenticationError),
        (403, AIAuthenticationError),
        (500, AIProviderError),
        (503, AIProviderError),
    ]:
        def handler(request: httpx.Request, s: int = status) -> httpx.Response:
            return httpx.Response(s, json={"error": {"message": f"Error {s}"}})

        client = mock_client_factory(handler)
        pool = AIHttpPool(client=client)
        provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=pool)

        with pytest.raises(exc_type) as exc_info:
            await provider.send(LLMRequest(messages=[Message(role="user", content="Hi")]))

        assert exc_info.value.status_code == status


async def test_openai_rate_limit_error(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "15"},
            json={"error": {"message": "Rate limit reached."}},
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = OpenAIProvider(config={"api_key": "test-key"}, http_pool=pool)
    req = LLMRequest(messages=[Message(role="user", content="Hi")])

    with pytest.raises(AIRateLimitError) as exc_info:
        await provider.send(req)

    assert exc_info.value.provider == "openai"
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 15.0


async def test_openai_connection_error(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Connection timed out")

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = OpenAIProvider(config={"api_key": "test-key"}, http_pool=pool)
    req = LLMRequest(messages=[Message(role="user", content="Hi")])

    with pytest.raises(AIConnectionError) as exc_info:
        await provider.send(req)

    assert exc_info.value.provider == "openai"
    assert "ConnectTimeout" in str(exc_info.value)


async def test_openai_sanitizes_api_key_in_errors(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    secret_key = "sk-proj-supersecretkey1234567890abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"Failed auth for key: {secret_key}")

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = OpenAIProvider(config={"api_key": secret_key}, http_pool=pool)
    req = LLMRequest(messages=[Message(role="user", content="Hi")])

    with pytest.raises(AIAuthenticationError) as exc_info:
        await provider.send(req)

    assert secret_key not in str(exc_info.value)
    assert secret_key not in (exc_info.value.response_body or "")


# ---------------------------------------------------------------------------
# Anthropic Provider Tests
# ---------------------------------------------------------------------------


async def test_anthropic_payload_mapping_and_response(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_01X9Z8",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-5-sonnet-20241022",
                "content": [
                    {
                        "type": "text",
                        "text": "Hello! I am Claude.",
                    }
                ],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 10,
                },
            },
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = AnthropicProvider(
        config={
            "api_key": "sk-ant-testkey",
            "model": "claude-3-5-sonnet-20241022",
            "base_url": "https://api.anthropic.com/v1",
            "anthropic_version": "2023-06-01",
        },
        http_pool=pool,
    )

    req = LLMRequest(
        messages=[
            Message(role="system", content="You are a wise tutor."),
            Message(role="user", content="Tell me a fact."),
        ],
        temperature=0.3,
        max_tokens=500,
    )

    resp = await provider.send(req)

    assert resp.content == "Hello! I am Claude."
    assert resp.role == "assistant"
    assert resp.model == "claude-3-5-sonnet-20241022"
    assert resp.usage.prompt_tokens == 20
    assert resp.usage.completion_tokens == 10
    assert resp.usage.total_tokens == 30
    assert resp.finish_reason == "end_turn"

    sent_req = captured_requests[0]
    body = json.loads(sent_req.content.decode("utf-8"))
    assert body["system"] == "You are a wise tutor."
    assert body["max_tokens"] == 500
    assert body["temperature"] == 0.3
    # System removed from messages list
    assert len(body["messages"]) == 1
    assert body["messages"][0] == {"role": "user", "content": "Tell me a fact."}


async def test_anthropic_multiple_system_messages_combined(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = AnthropicProvider(config={"api_key": "sk-ant"}, http_pool=pool)
    req = LLMRequest(
        messages=[
            Message(role="system", content="System Rule 1."),
            Message(role="system", content="System Rule 2."),
            Message(role="user", content="User message."),
        ]
    )
    await provider.send(req)

    body = json.loads(captured_requests[0].content.decode("utf-8"))
    assert body["system"] == "System Rule 1.\n\nSystem Rule 2."
    assert len(body["messages"]) == 1
    assert body["messages"][0] == {"role": "user", "content": "User message."}


async def test_anthropic_no_system_message_omits_system_key(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = AnthropicProvider(config={"api_key": "sk-ant"}, http_pool=pool)
    req = LLMRequest(messages=[Message(role="user", content="User only.")])
    await provider.send(req)

    body = json.loads(captured_requests[0].content.decode("utf-8"))
    assert "system" not in body


async def test_anthropic_multiple_text_blocks_combined(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "First part. "},
                    {"type": "text", "text": "Second part."},
                ]
            },
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = AnthropicProvider(config={"api_key": "sk-ant"}, http_pool=pool)
    resp = await provider.send(LLMRequest(messages=[Message(role="user", content="Hi")]))

    assert resp.content == "First part. Second part."


async def test_anthropic_empty_content_array(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": []})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = AnthropicProvider(config={"api_key": "sk-ant"}, http_pool=pool)
    resp = await provider.send(LLMRequest(messages=[Message(role="user", content="Hi")]))

    assert resp.content == ""


async def test_anthropic_missing_usage_and_stop_reason(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = AnthropicProvider(config={"api_key": "sk-ant"}, http_pool=pool)
    resp = await provider.send(LLMRequest(messages=[Message(role="user", content="Hi")]))

    assert resp.usage.prompt_tokens == 0
    assert resp.usage.completion_tokens == 0
    assert resp.finish_reason is None


async def test_anthropic_non_json_error_response(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"503 Service Unavailable", headers={"Content-Type": "text/plain"})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    provider = AnthropicProvider(config={"api_key": "sk-ant"}, http_pool=pool)

    with pytest.raises(AIProviderError) as exc_info:
        await provider.send(LLMRequest(messages=[Message(role="user", content="Hi")]))

    assert exc_info.value.status_code == 503
    assert "503 Service Unavailable" in str(exc_info.value)


async def test_anthropic_error_status_codes(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    for status, exc_type in [
        (400, AIProviderError),
        (401, AIAuthenticationError),
        (403, AIAuthenticationError),
        (429, AIRateLimitError),
        (500, AIProviderError),
        (503, AIProviderError),
    ]:
        def handler(request: httpx.Request, s: int = status) -> httpx.Response:
            return httpx.Response(s, json={"error": {"type": "error", "message": f"Error {s}"}})

        client = mock_client_factory(handler)
        pool = AIHttpPool(client=client)
        provider = AnthropicProvider(config={"api_key": "sk-ant"}, http_pool=pool)

        with pytest.raises(exc_type) as exc_info:
            await provider.send(LLMRequest(messages=[Message(role="user", content="Hi")]))

        assert exc_info.value.status_code == status


# ---------------------------------------------------------------------------
# Shared HTTP Pool & Resource Management Tests
# ---------------------------------------------------------------------------


async def test_shared_http_pool_used_across_both_providers(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    """
    CRITICAL TEST: Verifies that multiple providers share the EXACT same
    underlying httpx.AsyncClient instance through AIHttpPool.
    """
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if "openai" in str(request.url):
            return httpx.Response(200, json={"choices": [{"message": {"content": "OpenAI resp"}}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Anthropic resp"}]})

    shared_client = mock_client_factory(handler)
    shared_pool = AIHttpPool(client=shared_client)

    openai_provider = OpenAIProvider(config={"api_key": "sk-o"}, http_pool=shared_pool)
    anthropic_provider = AnthropicProvider(config={"api_key": "sk-a"}, http_pool=shared_pool)

    # Verify both providers reference the exact same HTTP client instance
    assert openai_provider._http_pool.get_client() is shared_client
    assert anthropic_provider._http_pool.get_client() is shared_client

    await openai_provider.send(LLMRequest(messages=[Message(role="user", content="Ping OpenAI")]))
    await anthropic_provider.send(LLMRequest(messages=[Message(role="user", content="Ping Anthropic")]))

    assert len(requested_urls) == 2
    assert "openai.com" in requested_urls[0]
    assert "anthropic.com" in requested_urls[1]


@pytest.mark.skipif(httpx is None, reason="httpx is required")
async def test_closing_pool_closes_client() -> None:
    pool = AIHttpPool()
    client = pool.get_client()
    assert not client.is_closed

    await pool.aclose()
    assert client.is_closed


@pytest.mark.skipif(httpx is None, reason="httpx is required")
async def test_closed_pool_reinitializes_fresh_client() -> None:
    pool = AIHttpPool()
    client1 = pool.get_client()
    await pool.aclose()
    assert client1.is_closed

    # Calling get_client after closing should return a fresh, open client
    client2 = pool.get_client()
    assert client2 is not client1
    assert not client2.is_closed
    await pool.aclose()


@pytest.mark.skipif(httpx is None, reason="httpx is required")
async def test_pool_connection_limits_and_timeout_applied() -> None:
    config = Config(
        {
            "ai": {
                "http": {
                    "max_connections": 123,
                    "max_keepalive_connections": 45,
                    "keepalive_expiry": 67.8,
                    "timeout": 89.0,
                }
            }
        }
    )
    pool = AIHttpPool(config=config)
    assert pool.max_connections == 123
    assert pool.max_keepalive_connections == 45
    assert pool.keepalive_expiry == 67.8
    assert pool.timeout == 89.0

    client = pool.get_client()
    assert client.timeout.connect == 89.0
    await pool.aclose()


async def test_concurrent_requests_through_shared_pool(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    """
    CRITICAL TEST: Verifies that multiple concurrent tasks can query providers
    through the single shared pool without interference, errors, or deadlock.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    shared_client = mock_client_factory(handler)
    shared_pool = AIHttpPool(client=shared_client)
    provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=shared_pool)

    async def _send_task(idx: int) -> str:
        req = LLMRequest(messages=[Message(role="user", content=f"Hello {idx}")])
        resp = await provider.send(req)
        return resp.content

    results = await asyncio.gather(*[_send_task(i) for i in range(25)])
    assert len(results) == 25
    assert all(r == "ok" for r in results)


# ---------------------------------------------------------------------------
# Response Metadata Tests
# ---------------------------------------------------------------------------


async def test_response_metadata_provider_reported_model(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-2024-08-06",
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)
    provider = OpenAIProvider(config={"api_key": "sk-test", "model": "gpt-4o"}, http_pool=pool)

    resp = await provider.send(LLMRequest(messages=[Message(role="user", content="Hi")]))
    assert resp.model == "gpt-4o-2024-08-06"


async def test_response_zero_token_metadata(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)
    provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=pool)

    resp = await provider.send(LLMRequest(messages=[Message(role="user", content="Hi")]))
    assert resp.usage.prompt_tokens == 0
    assert resp.usage.completion_tokens == 0
    assert resp.usage.total_tokens == 0
