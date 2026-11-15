from __future__ import annotations

import json
from pathlib import Path

import pytest

from swiftpy.admin import (
    ADMIN_COOKIE_NAME,
    AdminSite,
    AdminUser,
    create_admin_session,
)
from swiftpy.ai.client import AI, AIClient
from swiftpy.ai.observability.drivers import (
    DatabaseObservabilityDriver,
    FileObservabilityDriver,
    InMemoryObservabilityDriver,
)
from swiftpy.ai.observability.observer import (
    AIObservabilityManager,
    compute_prompt_hash,
    get_observability,
)
from swiftpy.ai.providers.base import Message
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.database.pool import DatabasePool
from swiftpy.http.response import Response
from swiftpy.testing.http_client import TestClient


def test_prompt_hash_deterministic() -> None:
    msgs1 = [
        Message(role="system", content="You are a helpful assistant."),
        Message(role="user", content="Hello world!"),
    ]
    msgs2 = [
        Message(role="system", content="You are a helpful assistant."),
        Message(role="user", content="Hello world!"),
    ]
    msgs3 = [
        Message(role="system", content="You are a helpful assistant."),
        Message(role="user", content="Hello SwiftPY!"),
    ]

    hash1 = compute_prompt_hash(msgs1)
    hash2 = compute_prompt_hash(msgs2)
    hash3 = compute_prompt_hash(msgs3)

    assert hash1 == hash2
    assert hash1 != hash3
    assert len(hash1) == 64


@pytest.mark.asyncio
async def test_in_memory_observability_driver() -> None:
    driver = InMemoryObservabilityDriver()
    manager = AIObservabilityManager(drivers=[driver])

    # Record 2 observations
    obs1 = await manager.record(
        model="gpt-4o",
        provider="openai",
        input_tokens=100,
        output_tokens=50,
        latency_ms=120.5,
        prompt_hash="hash_1",
    )
    obs2 = await manager.record(
        model="claude-3-5-sonnet",
        provider="anthropic",
        input_tokens=200,
        output_tokens=100,
        latency_ms=250.0,
        prompt_hash="hash_2",
    )

    assert obs1.total_tokens == 150
    assert obs1.cost > 0.0
    assert obs2.total_tokens == 300

    recent = await manager.get_observations(limit=10)
    assert len(recent) == 2
    # Most recent first
    assert recent[0].prompt_hash == "hash_2"
    assert recent[1].prompt_hash == "hash_1"

    stats = await manager.get_stats()
    assert stats["total_calls"] == 2
    assert stats["successful_calls"] == 2
    assert stats["total_tokens"] == 450
    assert stats["total_input_tokens"] == 300
    assert stats["total_output_tokens"] == 150
    assert stats["total_cost"] == round(obs1.cost + obs2.cost, 6)
    assert "openai" in stats["by_provider"]
    assert "anthropic" in stats["by_provider"]
    assert "gpt-4o" in stats["by_model"]
    assert "claude-3-5-sonnet" in stats["by_model"]


@pytest.mark.asyncio
async def test_file_observability_driver(tmp_path: Path) -> None:
    log_file = tmp_path / "ai.log"
    driver = FileObservabilityDriver(log_path=log_file)
    manager = AIObservabilityManager(drivers=[driver])

    obs = await manager.record(
        model="gpt-4o-mini",
        provider="openai",
        input_tokens=50,
        output_tokens=25,
        latency_ms=85.0,
        prompt_hash="file_hash_test",
        metadata={"user": "test_user"},
    )

    assert log_file.is_file()
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["model"] == "gpt-4o-mini"
    assert data["provider"] == "openai"
    assert data["total_tokens"] == 75
    assert data["prompt_hash"] == "file_hash_test"
    assert data["metadata"]["user"] == "test_user"

    # Query through manager
    recent = await manager.get_observations(limit=5)
    assert len(recent) == 1
    assert recent[0].id == obs.id

    stats = await manager.get_stats()
    assert stats["total_calls"] == 1
    assert stats["total_tokens"] == 75


@pytest.mark.asyncio
async def test_database_observability_driver_live() -> None:
    """Verify database observability driver against live Docker PostgreSQL."""
    try:
        cfg = Config.get_instance()
    except RuntimeError:
        cfg = Config()

    pool = DatabasePool(config=cfg)
    driver = DatabaseObservabilityDriver(pool=pool, table_name="ai_observations_test")
    await driver.ensure_table()
    await driver.clear()

    manager = AIObservabilityManager(drivers=[driver])

    obs = await manager.record(
        model="gpt-4o",
        provider="openai",
        input_tokens=150,
        output_tokens=75,
        latency_ms=110.2,
        prompt_hash="db_prompt_hash",
        metadata={"session_id": "session_123"},
    )

    recent = await manager.get_observations(limit=10)
    assert len(recent) == 1
    assert recent[0].model == "gpt-4o"
    assert recent[0].provider == "openai"
    assert recent[0].total_tokens == 225
    assert recent[0].prompt_hash == "db_prompt_hash"
    assert recent[0].metadata == {"session_id": "session_123"}

    stats = await manager.get_stats()
    assert stats["total_calls"] == 1
    assert stats["total_tokens"] == 225
    assert stats["total_cost"] == obs.cost
    assert "openai" in stats["by_provider"]


@pytest.mark.asyncio
async def test_observability_records_failed_call() -> None:
    driver = InMemoryObservabilityDriver()
    manager = AIObservabilityManager(drivers=[driver])

    with pytest.raises(ValueError):
        async with manager.observe_call(
            model="gpt-4o",
            provider="openai",
            messages=[Message(role="user", content="Will fail")],
        ):
            raise ValueError("Upstream provider failure simulation")

    recent = await manager.get_observations(limit=5)
    assert len(recent) == 1
    assert recent[0].status == "error"
    assert recent[0].error_message == "Upstream provider failure simulation"

    stats = await manager.get_stats()
    assert stats["total_calls"] == 1
    assert stats["successful_calls"] == 0
    assert stats["failed_calls"] == 1


@pytest.mark.asyncio
async def test_observability_records_streaming_call() -> None:
    driver = InMemoryObservabilityDriver()
    AIObservabilityManager.set_instance(AIObservabilityManager(drivers=[driver]))

    fake = AI.fake("Streaming token response from assistant")
    try:
        chunks: list[str] = []
        async for chunk in AIClient().model("gpt-4o").user("Stream query").stream():
            chunks.append(chunk)

        assert "".join(chunks) == "Streaming token response from assistant"

        recent = await get_observability().get_observations(limit=5)
        assert len(recent) == 1
        assert recent[0].status == "success"
        assert recent[0].total_tokens > 0
        assert recent[0].latency_ms > 0.0
    finally:
        fake.restore()
        AIObservabilityManager.set_instance(None)


@pytest.mark.asyncio
async def test_admin_panel_ai_usage_dashboard_views() -> None:
    # Set up in-memory observability records
    driver = InMemoryObservabilityDriver()
    manager = AIObservabilityManager(drivers=[driver])
    AIObservabilityManager.set_instance(manager)

    try:
        await manager.record(
            model="gpt-4o",
            provider="openai",
            input_tokens=100,
            output_tokens=50,
            latency_ms=130.0,
            prompt_hash="admin_hash_1",
        )
        await manager.record(
            model="claude-3-5-sonnet",
            provider="anthropic",
            input_tokens=200,
            output_tokens=100,
            latency_ms=210.0,
            prompt_hash="admin_hash_2",
        )

        site = AdminSite(title="AI Admin Portal")
        app = Application()
        site.mount(app, prefix="/admin")
        client = TestClient(app)

        user = AdminUser(username="admin_tester")
        login_resp = Response()
        token = create_admin_session(login_resp, user)
        auth_cookie = f"{ADMIN_COOKIE_NAME}={token}"
        headers = {"cookie": auth_cookie}

        # 1. Unauthenticated request to /admin/ai-usage redirects or returns 401
        unauth_resp = await client.get("/admin/ai-usage")
        assert unauth_resp.status_code == 302

        # 2. Authenticated request to /admin/ai-usage returns 200 HTML
        resp_html = await client.get("/admin/ai-usage", headers=headers)
        assert resp_html.status_code == 200
        assert "AI Observability &amp; Usage" in resp_html.text or "AI Observability & Usage" in resp_html.text
        assert "Provider Breakdown" in resp_html.text
        assert "Recent Observations" in resp_html.text
        assert "gpt-4o" in resp_html.text
        assert "claude-3-5-sonnet" in resp_html.text

        # 3. Authenticated JSON API request to /admin/ai-usage?format=json
        resp_json = await client.get("/admin/ai-usage?format=json", headers=headers)
        assert resp_json.status_code == 200
        payload = resp_json.json()
        assert "stats" in payload
        assert payload["stats"]["total_calls"] == 2
        assert len(payload["recent_observations"]) == 2
        assert payload["recent_observations"][0]["model"] in ("gpt-4o", "claude-3-5-sonnet")

        # 4. Main admin dashboard includes the AI Observability card
        resp_dash = await client.get("/admin/", headers=headers)
        assert resp_dash.status_code == 200
        assert "AI Observability &amp; Usage" in resp_dash.text or "AI Observability & Usage" in resp_dash.text
        assert "/admin/ai-usage" in resp_dash.text
    finally:
        AIObservabilityManager.set_instance(None)
