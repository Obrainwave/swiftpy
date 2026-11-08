from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from swiftpy.ai import AI
from swiftpy.ai.observability.drivers import (
    AIObservation,
    BaseObservabilityDriver,
    DatabaseObservabilityDriver,
    InMemoryObservabilityDriver,
    sanitize_metadata,
)
from swiftpy.ai.observability.observer import AIObservabilityManager, get_observability


class BrokenFailingDriver(BaseObservabilityDriver):
    """Driver that intentionally throws on every record attempt."""

    async def record(self, observation: AIObservation) -> None:
        raise OSError("Disk write failed: No space left on device")

    async def get_observations(self, *args: Any, **kwargs: Any) -> list[AIObservation]:
        return []

    async def get_stats(self) -> dict[str, Any]:
        return {}

    async def clear(self) -> None:
        pass


@pytest.mark.asyncio
async def test_observability_failure_isolation_fail_open_by_default() -> None:
    """
    Verify that telemetry driver failure does NOT crash user LLM requests
    when fail_closed is False (default resilient policy).
    """
    failing_driver = BrokenFailingDriver()
    manager = AIObservabilityManager(drivers=[failing_driver], fail_closed=False)

    # Calling record should not raise
    obs = await manager.record(
        model="gpt-4o",
        provider="openai",
        input_tokens=10,
        output_tokens=10,
        latency_ms=100.0,
        prompt_hash="abc",
    )
    assert obs.model == "gpt-4o"

    # In context manager, user operation completes successfully despite broken telemetry
    async with manager.observe_call(
        model="gpt-4o",
        provider="openai",
        messages="test prompt",
    ) as ctx:
        ctx.set_tokens(5, 5)

    assert ctx.total_tokens == 10


@pytest.mark.asyncio
async def test_observability_failure_enforcement_fail_closed() -> None:
    """Verify that when fail_closed=True, driver exceptions propagate to caller."""
    failing_driver = BrokenFailingDriver()
    manager = AIObservabilityManager(drivers=[failing_driver], fail_closed=True)

    with pytest.raises(OSError, match="No space left on device"):
        await manager.record(
            model="gpt-4o",
            provider="openai",
            input_tokens=10,
            output_tokens=10,
            latency_ms=50.0,
            prompt_hash="def",
        )


@pytest.mark.asyncio
async def test_streaming_ttft_and_duration_telemetry() -> None:
    """Verify streaming calls record TTFT (time-to-first-token) and total duration."""
    mem_driver = InMemoryObservabilityDriver()
    manager = AIObservabilityManager(drivers=[mem_driver])
    old_manager = get_observability()
    AIObservabilityManager.set_instance(manager)

    try:
        with AI.fake("Word1 Word2 Word3 Word4 Word5"):
            stream = AI.chat("gpt-4o").user("Generate 5 words").stream()
            collected = []
            async for token in stream:
                collected.append(token)
            assert len(collected) == 5

        records = await mem_driver.get_observations(limit=1)
        assert len(records) == 1
        obs = records[0]
        assert obs.status == "success"
        assert obs.ttft_ms is not None
        assert obs.ttft_ms > 0
        assert obs.duration_ms is not None
        assert obs.duration_ms >= obs.ttft_ms
    finally:
        AIObservabilityManager.set_instance(old_manager)


@pytest.mark.asyncio
async def test_provider_exception_records_error_observation() -> None:
    """Verify provider errors are captured in telemetry with error status and message."""
    mem_driver = InMemoryObservabilityDriver()
    manager = AIObservabilityManager(drivers=[mem_driver])

    with pytest.raises(RuntimeError, match="OpenAI rate limit error"):
        async with manager.observe_call(
            model="gpt-4o",
            provider="openai",
            messages="Failing request",
        ):
            await asyncio.sleep(0.005)
            raise RuntimeError("OpenAI rate limit error")

    records = await mem_driver.get_observations(limit=1)
    assert len(records) == 1
    obs = records[0]
    assert obs.status == "error"
    assert "OpenAI rate limit error" in (obs.error_message or "")


@pytest.mark.asyncio
async def test_metadata_secret_redaction_privacy() -> None:
    """Verify sensitive keys in metadata are automatically redacted."""
    raw_meta = {
        "user_id": "usr_123",
        "api_key": "sk-1234567890abcdef",
        "nested": {
            "password": "supersecretpassword",
            "bearer_token": "eyJhbGciOi...",
            "safe_tag": "public-tag",
        },
        "authorization_header": "Bearer token123",
    }

    sanitized = sanitize_metadata(raw_meta)

    assert sanitized["user_id"] == "usr_123"
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["password"] == "[REDACTED]"
    assert sanitized["nested"]["bearer_token"] == "[REDACTED]"
    assert sanitized["nested"]["safe_tag"] == "public-tag"
    assert sanitized["authorization_header"] == "[REDACTED]"

    # Verify AIObservation post_init applies redaction automatically
    obs = AIObservation(
        id="test-obs-1",
        timestamp=datetime.now(UTC),
        model="gpt-4o",
        provider="openai",
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
        latency_ms=10.0,
        cost=0.001,
        prompt_hash="abc",
        metadata={"client_secret": "raw_secret", "normal_flag": True},
    )
    assert obs.metadata["client_secret"] == "[REDACTED]"
    assert obs.metadata["normal_flag"] is True


@pytest.mark.asyncio
async def test_observability_filtering_by_date_provider_model_and_status() -> None:
    """Verify filtering observations by provider, model, status, and date range."""
    mem_driver = InMemoryObservabilityDriver()
    now = datetime.now(UTC)

    # Insert 4 distinct observations
    obs_list = [
        AIObservation(
            id="1",
            timestamp=now - timedelta(days=2),
            model="gpt-4o",
            provider="openai",
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
            latency_ms=15.0,
            cost=0.001,
            prompt_hash="h1",
            status="success",
        ),
        AIObservation(
            id="2",
            timestamp=now - timedelta(days=1),
            model="claude-sonnet-4-6",
            provider="anthropic",
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
            latency_ms=25.0,
            cost=0.002,
            prompt_hash="h2",
            status="success",
        ),
        AIObservation(
            id="3",
            timestamp=now,
            model="gpt-4o",
            provider="openai",
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
            latency_ms=100.0,
            cost=0.0,
            prompt_hash="h3",
            status="error",
        ),
        AIObservation(
            id="4",
            timestamp=now,
            model="claude-sonnet-4-6",
            provider="anthropic",
            input_tokens=5,
            output_tokens=5,
            total_tokens=10,
            latency_ms=20.0,
            cost=0.001,
            prompt_hash="h4",
            status="cancelled",
        ),
    ]

    for o in obs_list:
        await mem_driver.record(o)

    # 1. Filter by provider
    anthropic_records = await mem_driver.get_observations(provider="anthropic")
    assert len(anthropic_records) == 2
    assert all(r.provider == "anthropic" for r in anthropic_records)

    # 2. Filter by status
    cancelled_records = await mem_driver.get_observations(status="cancelled")
    assert len(cancelled_records) == 1
    assert cancelled_records[0].id == "4"

    error_records = await mem_driver.get_observations(status="error")
    assert len(error_records) == 1
    assert error_records[0].id == "3"

    # 3. Filter by model
    gpt_records = await mem_driver.get_observations(model="gpt-4o")
    assert len(gpt_records) == 2

    # 4. Filter by date window (yesterday to now)
    recent_records = await mem_driver.get_observations(
        start_date=now - timedelta(hours=36),
        end_date=now + timedelta(hours=1),
    )
    assert len(recent_records) == 3


@pytest.mark.asyncio
async def test_database_driver_sql_identifier_validation() -> None:
    """Verify that SQL injection attempts in table_name are rejected at construction."""
    with pytest.raises(ValueError):
        DatabaseObservabilityDriver(table_name="ai_obs; DROP TABLE users;--")

    with pytest.raises(ValueError):
        DatabaseObservabilityDriver(table_name="ai_obs' OR '1'='1")

    with pytest.raises(ValueError):
        DatabaseObservabilityDriver(table_name="ai_obs`test")

    # Valid identifier is accepted
    driver = DatabaseObservabilityDriver(table_name="valid_ai_observations")
    assert driver.table_name == "valid_ai_observations"
