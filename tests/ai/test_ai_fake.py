from __future__ import annotations

import pytest

from swiftpy.ai.client import AI
from swiftpy.ai.observability.drivers import InMemoryObservabilityDriver
from swiftpy.ai.observability.observer import (
    AIObservabilityManager,
    get_observability,
)
from swiftpy.ai.providers.base import LLMRequest
from swiftpy.testing.fakes.ai_fake import AIFake


@pytest.mark.asyncio
async def test_ai_fake_basic_string_fixture() -> None:
    fake = AI.fake("Hello from fake AI!")
    try:
        fake.assert_not_called()

        response = (
            await AI.chat("gpt-4o")
            .system("You are a helpful assistant.")
            .user("Hi there!")
            .send()
        )

        assert response.content == "Hello from fake AI!"
        fake.assert_called()
        fake.assert_called_once()
        fake.assert_called(times=1)
        fake.assert_called_with(model="gpt-4o", user="Hi there!", system="helpful assistant")
    finally:
        fake.restore()


@pytest.mark.asyncio
async def test_ai_fake_sequential_queue_fixtures() -> None:
    fake = AI.fake(["First response", "Second response", "Third response"])
    try:
        r1 = await AI.chat("gpt-4o").user("Q1").send()
        r2 = await AI.chat("claude-3-5-sonnet").user("Q2").send()
        r3 = await AI.chat("gpt-4o").user("Q3").send()

        assert r1.content == "First response"
        assert r2.content == "Second response"
        assert r3.content == "Third response"

        assert fake.call_count == 3
        fake.assert_called(times=3)

        fake.assert_called_with(model="claude-3-5-sonnet", user="Q2")
    finally:
        fake.restore()


@pytest.mark.asyncio
async def test_ai_fake_dict_fixtures_by_model() -> None:
    fixtures = {
        "gpt-4o": "Response from GPT-4o",
        "claude-3-5-sonnet": "Response from Claude 3.5 Sonnet",
        "default": "Fallback response",
    }
    fake = AI.fake(fixtures)
    try:
        r_gpt = await AI.chat("gpt-4o").user("Hello GPT").send()
        r_claude = await AI.chat("claude-3-5-sonnet").user("Hello Claude").send()
        r_other = await AI.chat("other-model").user("Hello other").send()

        assert r_gpt.content == "Response from GPT-4o"
        assert r_claude.content == "Response from Claude 3.5 Sonnet"
        assert r_other.content == "Fallback response"

        fake.assert_called(times=3)
    finally:
        fake.restore()


@pytest.mark.asyncio
async def test_ai_fake_callable_fixture() -> None:
    def echo_fixture(req: LLMRequest) -> str:
        user_msg = req.messages[-1].content or ""
        return f"Echo: {user_msg}"

    fake = AI.fake(echo_fixture)
    try:
        resp = await AI.chat("gpt-4o").user("Testing echo").send()
        assert resp.content == "Echo: Testing echo"
        fake.assert_called_once()
    finally:
        fake.restore()


@pytest.mark.asyncio
async def test_ai_fake_streaming() -> None:
    fake = AI.fake("The quick brown fox jumps over the lazy dog")
    try:
        tokens: list[str] = []
        async for chunk in AI.chat("gpt-4o").user("Stream sentence").stream():
            tokens.append(chunk)

        reconstructed = "".join(tokens)
        assert reconstructed == "The quick brown fox jumps over the lazy dog"
        assert len(tokens) > 1

        fake.assert_called_once()
        fake.assert_called_with(user="Stream sentence")
    finally:
        fake.restore()


@pytest.mark.asyncio
async def test_ai_fake_context_manager() -> None:
    with AI.fake("Context manager response") as fake:
        resp = await AI.chat("gpt-4o").user("Testing context manager").send()
        assert resp.content == "Context manager response"
        fake.assert_called_once()

    # Outside context manager, fake must be deactivated
    assert AIFake.get_active() is None


@pytest.mark.asyncio
async def test_ai_fake_assert_called_failures() -> None:
    fake = AI.fake("Test")
    try:
        # Not called yet
        with pytest.raises(AssertionError) as exc1:
            fake.assert_called()
        assert "zero calls were made" in str(exc1.value)

        with pytest.raises(AssertionError) as exc2:
            fake.assert_called_once()
        assert "called 0 times" in str(exc2.value)

        await AI.chat("gpt-4o").user("Hello world").send()

        # Called once, so assert_not_called must fail
        with pytest.raises(AssertionError):
            fake.assert_not_called()

        # assert_called(times=2) must fail
        with pytest.raises(AssertionError):
            fake.assert_called(times=2)

        # assert_called_with mismatches
        with pytest.raises(AssertionError) as exc3:
            fake.assert_called_with(model="wrong-model")
        assert "wrong-model" in str(exc3.value)

        with pytest.raises(AssertionError) as exc4:
            fake.assert_called_with(user="unmatched prompt")
        assert "unmatched prompt" in str(exc4.value)
    finally:
        fake.restore()


@pytest.mark.asyncio
async def test_ai_fake_observability_integration_zero_real_api_calls() -> None:
    """
    Verify observability records every AI call while AI.fake() intercepts them,
    guaranteeing zero real external API calls made and no real credentials required.
    """
    obs_driver = InMemoryObservabilityDriver()
    AIObservabilityManager.set_instance(AIObservabilityManager(drivers=[obs_driver]))

    fake = AI.fake("Observability verified fake response")
    try:
        # 1. Send call
        resp1 = (
            await AI.chat("gpt-4o")
            .system("You are an AI.")
            .user("Record this observation please.")
            .send()
        )
        assert resp1.content == "Observability verified fake response"

        # 2. Stream call
        stream_chunks = []
        async for chunk in AI.chat("claude-3-5-sonnet").user("Stream observation").stream():
            stream_chunks.append(chunk)
        assert "".join(stream_chunks) == "Observability verified fake response"

        # Verify fake asserts
        fake.assert_called(times=2)

        # Verify Observability telemetry recorded both calls!
        records = await get_observability().get_observations(limit=10)
        assert len(records) == 2

        # Most recent first: stream call
        stream_rec = records[0]
        assert stream_rec.model == "claude-3-5-sonnet"
        assert stream_rec.status == "success"
        assert stream_rec.total_tokens > 0
        assert stream_rec.latency_ms >= 0.0
        assert len(stream_rec.prompt_hash) == 64

        # Older: send call
        send_rec = records[1]
        assert send_rec.model == "gpt-4o"
        assert send_rec.status == "success"
        assert send_rec.total_tokens > 0
        assert send_rec.latency_ms >= 0.0
        assert len(send_rec.prompt_hash) == 64

        # Verify aggregate stats
        stats = await get_observability().get_stats()
        assert stats["total_calls"] == 2
        assert stats["successful_calls"] == 2
        assert stats["failed_calls"] == 0
        assert "gpt-4o" in stats["by_model"]
        assert "claude-3-5-sonnet" in stats["by_model"]
    finally:
        fake.restore()
        AIObservabilityManager.set_instance(None)
