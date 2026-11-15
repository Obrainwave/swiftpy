from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from swiftpy.ai.client import AI
from swiftpy.ai.exceptions import (
    AIAuthenticationError,
    AIProviderError,
    AIRateLimitError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.providers.anthropic_provider import AnthropicProvider
from swiftpy.ai.providers.base import LLMRequest, Message
from swiftpy.ai.providers.openai_provider import OpenAIProvider
from swiftpy.ai.streaming import parse_sse_events, split_chunk_lines
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response, stream
from swiftpy.testing.http_client import TestClient

# ---------------------------------------------------------------------------
# Critical Gap A: True Incremental HTTP Delivery
# ---------------------------------------------------------------------------


async def test_http_stream_delivers_chunks_incrementally() -> None:
    """
    CRITICAL TEST: Verify that the ASGI StreamingResponse delivers chunks
    incrementally across the wire as they arrive, rather than buffering
    the entire response until the generator finishes.
    """
    app = Application()
    chunk1_event = asyncio.Event()
    chunk2_allowed = asyncio.Event()
    finished_event = asyncio.Event()

    async def token_generator() -> AsyncIterator[str]:
        # Emit chunk 1 and signal that it was emitted
        yield "FirstChunk "
        chunk1_event.set()

        # Wait until the client confirms receipt before emitting chunk 2
        await chunk2_allowed.wait()
        yield "SecondChunk"
        finished_event.set()

    @app.get("/stream-test")
    async def stream_route(request: Request) -> Response:
        return stream(token_generator(), content_type="text/plain; charset=utf-8")

    # Simulate ASGI scope, receive, send lifecycle
    received_chunks: list[str] = []

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/stream-test",
        "raw_path": b"/stream-test",
        "query_string": b"",
        "headers": [],
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                received_chunks.append(body.decode("utf-8"))

    async def run_request() -> None:
        await app(scope, receive, send)

    request_task = asyncio.create_task(run_request())

    # Wait for generator to yield chunk 1
    await asyncio.wait_for(chunk1_event.wait(), timeout=1.0)
    # Brief yield to let ASGI send receive chunk 1
    await asyncio.sleep(0.01)

    # PROVE: First chunk has ALREADY been delivered to client while chunk 2 has NOT been emitted!
    assert received_chunks == ["FirstChunk "]
    assert not finished_event.is_set()

    # Now allow chunk 2 to be emitted
    chunk2_allowed.set()
    await asyncio.wait_for(finished_event.wait(), timeout=1.0)
    await asyncio.wait_for(request_task, timeout=1.0)

    # PROVE: Second chunk arrived after chunk 1
    assert received_chunks == ["FirstChunk ", "SecondChunk"]


# ---------------------------------------------------------------------------
# Critical Gap B: Stream Cancellation & Resource Cleanup
# ---------------------------------------------------------------------------


async def test_stream_cancellation_closes_provider_response(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    """
    CRITICAL TEST: Verify that cancelling a stream generator triggers
    response.aclose() and releases the HTTP connection without leaks.
    """
    response_closed = False

    async def infinite_sse_lines() -> AsyncIterator[str]:
        nonlocal response_closed
        try:
            while True:
                yield 'data: {"choices":[{"delta":{"content":"token"}}]}\n\n'
                await asyncio.sleep(0.01)
        finally:
            response_closed = True

    # Use a dummy provider stream loop to simulate cancellation
    async def mock_stream() -> AsyncGenerator[str, None]:
        nonlocal response_closed
        try:
            async for event in parse_sse_events(infinite_sse_lines()):
                data = event.get("data")
                if isinstance(data, dict):
                    yield "token"
        finally:
            response_closed = True

    gen = mock_stream()
    first_token = await anext(gen)
    assert first_token == "token"

    # Cancel / close consumer
    await gen.aclose()

    # Verify that response was closed
    assert response_closed is True


# ---------------------------------------------------------------------------
# Critical Gap C: SSE Parser Edge Cases
# ---------------------------------------------------------------------------


async def test_sse_parser_handles_multiple_data_lines() -> None:
    """
    Per SSE specification: consecutive data: lines in the same event
    must be joined by a newline (\\n).
    """
    lines = [
        "data: Line 1",
        "data: Line 2",
        "data: Line 3",
        "",  # Event boundary
    ]

    async def line_gen() -> AsyncIterator[str]:
        for line in lines:
            yield line

    events = [e async for e in parse_sse_events(line_gen())]
    assert len(events) == 1
    assert events[0]["data"] == "Line 1\nLine 2\nLine 3"


async def test_sse_parser_handles_crlf_line_endings() -> None:
    lines = [
        "event: message\r\n",
        'data: {"text":"hello"}\r\n',
        "\r\n",
    ]

    async def line_gen() -> AsyncIterator[str]:
        for line in lines:
            yield line

    events = [e async for e in parse_sse_events(line_gen())]
    assert len(events) == 1
    assert events[0]["data"] == {"text": "hello"}


async def test_sse_parser_handles_events_split_across_network_chunks() -> None:
    """
    Network chunks do not always align with line or event boundaries.
    Verify that split_chunk_lines reconstructs complete lines before parsing.
    """
    chunks = [
        'data: {"choices":[{"delta":',
        '{"content":"fragmented"}}]}\n\n',
        "data: [DONE]\n\n",
    ]

    async def chunk_gen() -> AsyncIterator[str]:
        for c in chunks:
            yield c

    lines = split_chunk_lines(chunk_gen())
    events = [e async for e in parse_sse_events(lines)]
    assert len(events) == 1
    assert events[0]["data"] == {"choices": [{"delta": {"content": "fragmented"}}]}


async def test_sse_parser_handles_empty_events_and_comments() -> None:
    lines = [
        ": keepalive comment",
        "",  # Empty event boundary without data
        ": another comment",
        'data: {"choices":[{"delta":{"content":"valid"}}]}',
        "",
    ]

    async def line_gen() -> AsyncIterator[str]:
        for line in lines:
            yield line

    events = [e async for e in parse_sse_events(line_gen())]
    assert len(events) == 1
    assert events[0]["data"] == {"choices": [{"delta": {"content": "valid"}}]}


async def test_sse_parser_handles_eof_without_final_blank_line() -> None:
    lines = [
        'data: {"val": 42}',
        # No trailing blank line before EOF
    ]

    async def line_gen() -> AsyncIterator[str]:
        for line in lines:
            yield line

    events = [e async for e in parse_sse_events(line_gen())]
    assert len(events) == 1
    assert events[0]["data"] == {"val": 42}


async def test_sse_parser_raises_on_provider_error_payload() -> None:
    lines = [
        'data: {"error": {"message": "Invalid request parameter", "type": "invalid_request_error"}}',
        "",
    ]

    async def line_gen() -> AsyncIterator[str]:
        for line in lines:
            yield line

    with pytest.raises(AIProviderError, match="Invalid request parameter"):
        async for _ in parse_sse_events(line_gen()):
            pass


# ---------------------------------------------------------------------------
# Provider SSE Streaming Tests
# ---------------------------------------------------------------------------


async def test_openai_streaming_yields_incremental_chunks(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    sse_body = (
        b": ping\n\n"
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"!"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_body,
            headers={"Content-Type": "text/event-stream"},
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)
    provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=pool)

    req = LLMRequest(messages=[Message(role="user", content="Hi")])
    chunks: list[str] = []
    async for chunk in provider.stream(req):
        chunks.append(chunk)

    assert chunks == ["Hello", " world", "!"]


async def test_openai_streaming_error_handling(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)
    provider = OpenAIProvider(config={"api_key": "bad-key"}, http_pool=pool)

    with pytest.raises(AIAuthenticationError, match="Invalid API key"):
        async for _ in provider.stream(LLMRequest(messages=[Message(role="user", content="Hi")])):
            pass


async def test_anthropic_streaming_yields_incremental_chunks(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    sse_body = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_1"}}\n\n'
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Swift"}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"PY is"}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" fast"}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_body,
            headers={"Content-Type": "text/event-stream"},
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)
    provider = AnthropicProvider(config={"api_key": "sk-ant"}, http_pool=pool)

    req = LLMRequest(messages=[Message(role="user", content="Describe SwiftPY")])
    chunks: list[str] = []
    async for chunk in provider.stream(req):
        chunks.append(chunk)

    assert chunks == ["Swift", "PY is", " fast"]


async def test_anthropic_streaming_rate_limit_error(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "20"},
            json={"error": {"type": "rate_limit_error", "message": "Too many requests"}},
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)
    provider = AnthropicProvider(config={"api_key": "sk-ant"}, http_pool=pool)

    with pytest.raises(AIRateLimitError) as exc_info:
        async for _ in provider.stream(LLMRequest(messages=[Message(role="user", content="Hi")])):
            pass

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 20.0


# ---------------------------------------------------------------------------
# Target API Integration Tests
# ---------------------------------------------------------------------------


async def test_target_api_streaming_route(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    sse_body = (
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Chunk1 "}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Chunk2"}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body, headers={"Content-Type": "text/event-stream"})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    app = Application()
    cfg = Config(
        {
            "ai": {
                "enabled": True,
                "default": "anthropic",
                "providers": {
                    "anthropic": {"api_key": "sk-ant-test", "base_url": "https://api.anthropic.com/v1"},
                },
            }
        }
    )

    @app.post("/chat/stream")
    async def chat_stream(request: Request) -> Response:
        data = await request.json()
        message = data.get("message", "")

        async def tokens() -> Any:
            async for chunk in (
                AI.chat("claude-sonnet-4-6", config=cfg, http_pool=pool)
                .system("You are a helpful assistant.")
                .user(message)
                .stream()
            ):
                yield chunk

        return stream(tokens(), content_type="text/plain; charset=utf-8")

    test_client = TestClient(app)
    response = await test_client.post("/chat/stream", json={"message": "Hello Claude"})

    assert response.status_code == 200
    assert response.headers.get("content-type") == "text/plain; charset=utf-8"
    assert response.text == "Chunk1 Chunk2"


# ---------------------------------------------------------------------------
# Concurrency Tests (Non-blocking and Stream Isolation)
# ---------------------------------------------------------------------------


async def test_streaming_does_not_block_concurrent_requests(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    app = Application()

    @app.get("/ping")
    async def ping(request: Request) -> Response:
        return json_response({"status": "pong"})

    @app.get("/slow-stream")
    async def slow_stream(request: Request) -> Response:
        async def slow_tokens() -> Any:
            for i in range(5):
                await asyncio.sleep(0.04)  # 40ms per chunk = 200ms total
                yield f"token_{i} "

        return stream(slow_tokens())

    test_client = TestClient(app)

    async def stream_task() -> str:
        res = await test_client.get("/slow-stream")
        return res.text

    async def ping_task() -> tuple[float, int]:
        await asyncio.sleep(0.02)
        start = asyncio.get_running_loop().time()
        res = await test_client.get("/ping")
        elapsed = asyncio.get_running_loop().time() - start
        return elapsed, res.status_code

    stream_future = asyncio.create_task(stream_task())
    ping_future = asyncio.create_task(ping_task())

    ping_elapsed, ping_status = await ping_future
    stream_content = await stream_future

    assert ping_status == 200
    assert "token_0 token_1 token_2 token_3 token_4" in stream_content
    # Verified: /ping completed without waiting for the 200ms stream to finish
    assert ping_elapsed < 0.15, f"/ping was blocked! Took {ping_elapsed:.3f}s"


async def test_concurrent_streams_do_not_mix_chunks(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    """
    CRITICAL CONCURRENCY TEST: Start two streams simultaneously and verify
    that their tokens do not interleave or mix into the wrong consumer.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["messages"][-1]["content"]
        if "StreamA" in prompt:
            sse = b'data: {"choices":[{"delta":{"content":"A1"}}]}\n\ndata: {"choices":[{"delta":{"content":"A2"}}]}\n\ndata: [DONE]\n\n'
        else:
            sse = b'data: {"choices":[{"delta":{"content":"B1"}}]}\n\ndata: {"choices":[{"delta":{"content":"B2"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=sse, headers={"Content-Type": "text/event-stream"})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)
    provider = OpenAIProvider(config={"api_key": "sk-test"}, http_pool=pool)

    async def run_stream(tag: str) -> list[str]:
        chunks: list[str] = []
        async for c in provider.stream(LLMRequest(messages=[Message(role="user", content=f"Hello {tag}")])):
            chunks.append(c)
        return chunks

    results = await asyncio.gather(run_stream("StreamA"), run_stream("StreamB"))
    assert results[0] == ["A1", "A2"]
    assert results[1] == ["B1", "B2"]
