from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from swiftpy.ai import AI, AIClient
from swiftpy.ai.agents import Agent
from swiftpy.ai.embeddings import EmbeddingsClient
from swiftpy.ai.observability.observer import get_observability
from swiftpy.ai.rag.pipeline_builder import rag
from swiftpy.ai.rag.retriever import Retriever
from swiftpy.ai.tools.base_tool import tool
from swiftpy.ai.vectorstore.base import VectorStore
from swiftpy.testing.fakes.ai_fake import AIFake


@pytest.mark.asyncio
async def test_concurrent_tasks_fake_task_isolation() -> None:
    """
    Verify two concurrent async tasks using different fake fixtures via asyncio.gather()
    have absolute task-local isolation without cross-talk.
    """
    async def task_a() -> str:
        with AI.fake("Response Alpha") as fake_a:
            await asyncio.sleep(0.01)
            resp = await AI.chat("gpt-4o").user("Request A").send()
            fake_a.assert_called_once()
            fake_a.assert_called_with(user="Request A")
            return resp.content or ""

    async def task_b() -> str:
        with AI.fake("Response Beta") as fake_b:
            await asyncio.sleep(0.01)
            resp = await AI.chat("gpt-4o").user("Request B").send()
            fake_b.assert_called_once()
            fake_b.assert_called_with(user="Request B")
            return resp.content or ""

    res_a, res_b = await asyncio.gather(task_a(), task_b())

    assert res_a == "Response Alpha"
    assert res_b == "Response Beta"


@pytest.mark.asyncio
async def test_nested_fake_contexts_and_restoration() -> None:
    """Verify nested fake contexts push and pop in stack order."""
    assert AIFake.get_active() is None

    with AI.fake("Outer Response") as outer_fake:
        assert AIFake.get_active() is outer_fake
        resp1 = await AI.chat("gpt-4o").user("First").send()
        assert resp1.content == "Outer Response"

        with AI.fake("Inner Response") as inner_fake:
            assert AIFake.get_active() is inner_fake
            resp2 = await AI.chat("gpt-4o").user("Second").send()
            assert resp2.content == "Inner Response"
            inner_fake.assert_called_once()

        # After exiting inner, active fake must be outer
        assert AIFake.get_active() is outer_fake
        resp3 = await AI.chat("gpt-4o").user("Third").send()
        assert resp3.content == "Outer Response"
        assert outer_fake.call_count == 2

    # After exiting outer, no fake is active
    assert AIFake.get_active() is None


@pytest.mark.asyncio
async def test_exception_inside_fake_context_restores_cleanly() -> None:
    """Verify an unhandled exception inside a fake context still restores the active fake."""
    assert AIFake.get_active() is None

    with pytest.raises(ValueError, match="boom"), AI.fake("Temp Response"):
        assert AIFake.get_active() is not None
        raise ValueError("boom")

    assert AIFake.get_active() is None


@pytest.mark.asyncio
async def test_multiple_independent_aiclient_instances_intercepted() -> None:
    """Verify all AIClient instances are intercepted by the active fake."""
    with AI.fake("Unified Fake Reply") as fake:
        c1 = AI.chat("gpt-4o").user("C1")
        c2 = AI.chat("claude-sonnet-4-6").user("C2")
        c3 = AIClient().model("custom-llm").user("C3")

        r1 = await c1.send()
        r2 = await c2.send()
        r3 = await c3.send()

        assert r1.content == "Unified Fake Reply"
        assert r2.content == "Unified Fake Reply"
        assert r3.content == "Unified Fake Reply"
        assert fake.call_count == 3


@pytest.mark.asyncio
async def test_embedding_interception_and_assertions() -> None:
    """Verify AI.embed() and EmbeddingsClient calls are intercepted by active fake."""
    with AI.fake("LLM text") as fake:
        fake.assert_embedding_not_called()

        # Single embedding
        vec1 = await AI.embed("SwiftPY ASGI framework")
        assert len(vec1) == 1536
        fake.assert_embedding_called_once()
        fake.assert_embedding_called_with(text="SwiftPY ASGI framework")

        # Batch embedding
        client = EmbeddingsClient()
        batch_vecs = await client.embed_batch(["text 1", "text 2"], model="text-embedding-3-large")
        assert len(batch_vecs) == 2
        assert fake.embedding_call_count == 2
        fake.assert_embedding_called_with(texts=["text 1", "text 2"], model="text-embedding-3-large")


@pytest.mark.asyncio
async def test_rag_pipeline_interception() -> None:
    """Verify RAG pipeline execution is fully intercepted without external credentials."""
    store = VectorStore.in_memory(collection="rag_fake_test", dimensions=1536)
    retriever = Retriever(vectorstore=store, top_k=2)

    await retriever.add_documents(
        documents=["SwiftPY features an asynchronous ASGI core."],
        ids=["doc_1"],
    )

    with AI.fake("Sourced answer: SwiftPY is asynchronous.") as fake:
        pipeline = rag().retriever(retriever)
        result = await pipeline.run("What is SwiftPY?")
        assert result.text == "Sourced answer: SwiftPY is asynchronous."
        fake.assert_called_once()
        fake.assert_embedding_called()


@pytest.mark.asyncio
async def test_agent_execution_interception() -> None:
    """Verify BaseAgent/Agent execution calls are intercepted."""
    with AI.fake("Agent solved task.") as fake:
        agent = Agent(
            name="ResearchAgent",
            system_prompt="You are an autonomous research agent.",
        )
        resp = await agent.run("Perform research task")
        assert resp.content == "Agent solved task."
        fake.assert_called_once()


@pytest.mark.asyncio
async def test_tool_calling_sub_ai_interception() -> None:
    """Verify tools that internally make AI calls are intercepted."""
    @tool(description="Runs a nested AI call")
    async def subquery(topic: str) -> str:
        res = await AI.chat("gpt-4o").user(f"Summarize {topic}").send()
        return f"Summary: {res.content}"

    with AI.fake("Summarized topic details.") as fake:
        result = await subquery.handle(topic="Async Python")
        assert result == "Summary: Summarized topic details."
        fake.assert_called_once()


@pytest.mark.asyncio
async def test_streaming_cancellation_resource_cleanup() -> None:
    """Verify streaming cancellation gracefully cleans up and records cancelled status."""
    observer = get_observability()

    with AI.fake("Token one two three four five six") as fake:
        stream = AI.chat("gpt-4o").user("Stream prompt").stream()

        # Consumer consumes only 2 tokens then breaks early
        collected = []
        async for chunk in stream:
            collected.append(chunk)
            if len(collected) == 2:
                break
        if isinstance(stream, AsyncGenerator):
            await stream.aclose()

        assert len(collected) == 2
        fake.assert_called_once()

    # Check latest observation status is cancelled
    recent = await observer.get_observations(limit=1)
    assert len(recent) == 1
    assert recent[0].status == "cancelled"


@pytest.mark.asyncio
async def test_network_guard_zero_outbound_http_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Network guard test: monkeypatches provider resolution to raise if invoked.
    Proves that zero real API provider logic or HTTP calls escape through AI.fake().
    """
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Real provider resolution was triggered while AI.fake() was active!")

    monkeypatch.setattr(AIClient, "_resolve_provider", forbidden)
    monkeypatch.setattr(EmbeddingsClient, "_resolve_provider_and_key", forbidden)

    with AI.fake("Protected fake response"):
        resp = await AI.chat("gpt-4o").user("Should not hit provider").send()
        assert resp.content == "Protected fake response"

        vec = await AI.embed("Should not hit provider embedding")
        assert len(vec) == 1536
