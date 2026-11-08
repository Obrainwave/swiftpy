from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest

from swiftpy.ai.agents import (
    Agent,
    AgentResponse,
    AgentTask,
    AgentTracker,
    DatabaseMemoryBackend,
    InMemoryMemoryBackend,
    RedisMemoryBackend,
)
from swiftpy.ai.client import AIClient
from swiftpy.ai.exceptions import AgentLoopError
from swiftpy.ai.providers.base import (
    LLMRequest,
    LLMResponse,
    Message,
    ProviderInterface,
    TokenUsage,
    ToolCall,
)
from swiftpy.ai.tools.base_tool import tool
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.database.pool import DatabasePool
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient

# ==============================================================================
# Test Fixtures & Mock Providers
# ==============================================================================


class ScopedAgentMockProvider1(ProviderInterface):
    _responses: ClassVar[list[LLMResponse]] = []
    _received: ClassVar[list[LLMRequest]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    @classmethod
    def set_responses(cls, resps: list[LLMResponse]) -> None:
        cls._responses = list(resps)
        cls._received.clear()

    @property
    def name(self) -> str:
        return "mock_agent_provider_1"

    def get_default_model(self) -> str:
        return "mock-agent-1"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self._received.append(request)
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(
            content="Fallback answer from mock LLM 1.",
            role="assistant",
            model=self.get_default_model(),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        yield "stream-unused"


class ScopedAgentMockProvider2(ProviderInterface):
    _responses: ClassVar[list[LLMResponse]] = []
    _received: ClassVar[list[LLMRequest]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    @classmethod
    def set_responses(cls, resps: list[LLMResponse]) -> None:
        cls._responses = list(resps)
        cls._received.clear()

    @property
    def name(self) -> str:
        return "mock_agent_provider_2"

    def get_default_model(self) -> str:
        return "mock-agent-2"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self._received.append(request)
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(
            content="Fallback answer from mock LLM 2.",
            role="assistant",
            model=self.get_default_model(),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        yield "stream-unused"


def _build_test_config() -> Config:
    cfg = Config()
    cfg.set("ai.default", "openai")
    cfg.set("ai.providers.openai.api_key", "sk-mock-test")
    cfg.set("ai.providers.openai.model", "gpt-4o")
    cfg.set("ai.redis.host", "redis")
    cfg.set("ai.redis.port", 6379)
    cfg.set("ai.redis.password", "secret")
    cfg.set("database.connections.postgresql.host", "postgres")
    cfg.set("database.connections.postgresql.port", 5432)
    cfg.set("database.connections.postgresql.database", "swiftpy")
    cfg.set("database.connections.postgresql.username", "postgres")
    cfg.set("database.connections.postgresql.password", "secret")
    return cfg


def _create_mock_client(provider_name: str, provider_obj: ProviderInterface) -> AIClient:
    cfg = _build_test_config()
    cfg.set(f"ai.providers.{provider_name}.api_key", "sk-mock-key")
    client = AIClient(config=cfg, default_provider=provider_name)
    client.register_provider(provider_name, provider_obj)
    client.provider(provider_name)
    return client


# ==============================================================================
# Agent Concurrency & Isolation Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_two_agents_simultaneously_do_not_share_memory_or_tool_state() -> None:
    """
    Verify that two agents executing concurrently:
    1. Are synchronized via barriers so both enter their provider calls simultaneously.
    2. Maintain strictly isolated message memory.
    3. Maintain strictly isolated tool states.
    """
    agent1_entered = asyncio.Event()
    agent2_entered = asyncio.Event()
    release_both = asyncio.Event()

    class BarrierMockProvider1(ProviderInterface):
        name: str = "barrier_p1"

        def get_default_model(self) -> str:
            return "m1"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._turn = 0

        async def send(self, request: LLMRequest) -> LLMResponse:
            self._turn += 1
            if self._turn == 1:
                agent1_entered.set()
                await release_both.wait()
                return LLMResponse(
                    content="",
                    role="assistant",
                    model="m1",
                    tool_calls=[ToolCall(id="call_a1", name="agent_one_tool", arguments={"value": "data_a"})],
                )
            return LLMResponse(content="Agent 1 final answer.", role="assistant", model="m1")

        async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
            yield ""

    class BarrierMockProvider2(ProviderInterface):
        name: str = "barrier_p2"

        def get_default_model(self) -> str:
            return "m2"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._turn = 0

        async def send(self, request: LLMRequest) -> LLMResponse:
            self._turn += 1
            if self._turn == 1:
                agent2_entered.set()
                await release_both.wait()
                return LLMResponse(
                    content="",
                    role="assistant",
                    model="m2",
                    tool_calls=[ToolCall(id="call_b2", name="agent_two_tool", arguments={"value": "data_b"})],
                )
            return LLMResponse(content="Agent 2 final answer.", role="assistant", model="m2")

        async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
            yield ""

    agent_1_calls: list[str] = []
    agent_2_calls: list[str] = []

    @tool(name="agent_one_tool", description="Tool for agent 1")
    async def agent_one_tool(value: str) -> dict[str, str]:
        agent_1_calls.append(value)
        return {"agent_1_result": value}

    @tool(name="agent_two_tool", description="Tool for agent 2")
    async def agent_two_tool(value: str) -> dict[str, str]:
        agent_2_calls.append(value)
        return {"agent_2_result": value}

    c1 = _create_mock_client("barrier_p1", BarrierMockProvider1())
    c2 = _create_mock_client("barrier_p2", BarrierMockProvider2())

    mem1 = InMemoryMemoryBackend()
    mem2 = InMemoryMemoryBackend()

    agent1 = Agent(
        name="AgentOne",
        system_prompt="You are Agent One.",
        tools=[agent_one_tool],
        client=c1,
        memory=mem1,
        max_iterations=3,
    )
    agent2 = Agent(
        name="AgentTwo",
        system_prompt="You are Agent Two.",
        tools=[agent_two_tool],
        client=c2,
        memory=mem2,
        max_iterations=3,
    )

    async def coordinator() -> None:
        await asyncio.gather(agent1_entered.wait(), agent2_entered.wait())
        release_both.set()

    coord_task = asyncio.create_task(coordinator())

    # Both should run concurrently, waiting at the barrier
    r1, r2 = await asyncio.gather(
        agent1.run("Prompt for agent 1", session_id="s1"),
        agent2.run("Prompt for agent 2", session_id="s2"),
    )
    await coord_task

    assert agent_1_calls == ["data_a"]
    assert agent_2_calls == ["data_b"]
    assert len(r1.executed_calls) == 1
    assert r1.executed_calls[0].name == "agent_one_tool"
    assert len(r2.executed_calls) == 1
    assert r2.executed_calls[0].name == "agent_two_tool"

    m1_history = await mem1.load_messages("s1")
    m2_history = await mem2.load_messages("s2")
    assert any("Agent One" in m.content for m in m1_history)
    assert not any("Agent Two" in m.content for m in m1_history)
    assert any("Agent Two" in m.content for m in m2_history)
    assert not any("Agent One" in m.content for m in m2_history)


@pytest.mark.asyncio
async def test_two_agents_sharing_same_memory_backend_have_isolated_sessions() -> None:
    """Verify multiple agents sharing a single memory backend instance maintain strict session isolation."""
    shared_backend = InMemoryMemoryBackend()

    agent_a = Agent(name="AgentA", system_prompt="System A", memory=shared_backend)
    agent_b = Agent(name="AgentB", system_prompt="System B", memory=shared_backend)

    mem_a = agent_a.get_session_memory("session_alpha")
    mem_b = agent_b.get_session_memory("session_beta")

    await mem_a.add_user("User message for Alpha")
    await mem_a.set_state("key_a", "val_a")

    await mem_b.add_user("User message for Beta")
    await mem_b.set_state("key_b", "val_b")

    a_msgs = await mem_a.get_messages()
    b_msgs = await mem_b.get_messages()

    assert len(a_msgs) == 1 and a_msgs[0].content == "User message for Alpha"
    assert len(b_msgs) == 1 and b_msgs[0].content == "User message for Beta"
    assert await mem_a.get_state("key_a") == "val_a"
    assert await mem_a.get_state("key_b") is None
    assert await mem_b.get_state("key_b") == "val_b"
    assert await mem_b.get_state("key_a") is None


@pytest.mark.asyncio
async def test_agent_enforces_system_prompt() -> None:
    """Verify system prompt is enforced in memory and sent on every dialogue turn."""
    mem = InMemoryMemoryBackend()
    received_requests: list[LLMRequest] = []

    class SysPromptProvider(ProviderInterface):
        name: str = "sys_provider"

        def get_default_model(self) -> str:
            return "sys-model"

        async def send(self, request: LLMRequest) -> LLMResponse:
            received_requests.append(request)
            return LLMResponse(content=f"Echo turn {len(received_requests)}", role="assistant", model="sys-model")

        async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
            yield ""

    client = _create_mock_client("sys_provider", SysPromptProvider())

    agent = Agent(
        name="Assistant",
        system_prompt="You are a specialized security agent.",
        memory=mem,
        client=client,
    )

    await agent.run("Turn 1", session_id="sys_test")
    await agent.run("Turn 2", session_id="sys_test")

    assert len(received_requests) == 2
    for req in received_requests:
        assert len(req.messages) >= 2
        assert req.messages[0].role == "system"
        assert req.messages[0].content == "You are a specialized security agent."

    history = await mem.load_messages("sys_test")
    system_messages = [m for m in history if m.role == "system"]
    assert len(system_messages) == 1
    assert system_messages[0].content == "You are a specialized security agent."


@pytest.mark.asyncio
async def test_agent_enforces_iteration_limit_boundary() -> None:
    """
    Verify exact iteration boundary protections:
    - Exactly 3 tool-loop iterations executed.
    - 4th model turn was NEVER requested.
    - Tool was executed exactly 3 times.
    - Memory remains consistent.
    """
    tool_exec_count = 0

    @tool(name="looping_tool", description="Tool that requests itself")
    async def looping_tool(count: int) -> dict[str, int]:
        nonlocal tool_exec_count
        tool_exec_count += 1
        return {"current": count + 1}

    provider_calls = 0

    class ContinuousLoopProvider(ProviderInterface):
        name: str = "loop_p"

        def get_default_model(self) -> str:
            return "m"

        async def send(self, request: LLMRequest) -> LLMResponse:
            nonlocal provider_calls
            provider_calls += 1
            return LLMResponse(
                content="",
                role="assistant",
                model="m",
                tool_calls=[ToolCall(id=f"call_{provider_calls}", name="looping_tool", arguments={"count": provider_calls})],
            )

        async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
            yield ""

    client = _create_mock_client("loop_p", ContinuousLoopProvider())

    mem = InMemoryMemoryBackend()
    agent = Agent(
        name="LoopAgent",
        tools=[looping_tool],
        client=client,
        memory=mem,
        max_iterations=3,
    )

    with pytest.raises(AgentLoopError) as exc_info:
        await agent.run("Start infinite loop", session_id="bound_test")

    assert exc_info.value.iterations == 3
    assert provider_calls == 3
    assert tool_exec_count == 3
    history = await mem.load_messages("bound_test")
    assert len(history) == 0


# ==============================================================================
# Background Asyncio Concurrency & Responsiveness
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_runs_as_background_task_non_blocking_heartbeat() -> None:
    """Verify background task execution does not block event loop heartbeat ticks."""
    heartbeat_ticks = 0
    stop_heartbeat = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not stop_heartbeat.is_set():
            await asyncio.sleep(0.005)
            heartbeat_ticks += 1

    hb_task = asyncio.create_task(heartbeat())

    @tool(name="slow_tool", description="Simulates slow background work")
    async def slow_tool() -> dict[str, str]:
        await asyncio.sleep(0.08)
        return {"status": "finished"}

    class SlowProvider(ProviderInterface):
        name: str = "slow_p"

        def get_default_model(self) -> str:
            return "m"

        async def send(self, request: LLMRequest) -> LLMResponse:
            if not request.messages or request.messages[-1].role != "tool":
                return LLMResponse(
                    content="",
                    role="assistant",
                    model="m",
                    tool_calls=[ToolCall(id="c1", name="slow_tool", arguments={})],
                )
            return LLMResponse(content="Slow work complete.", role="assistant", model="m")

        async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
            yield ""

    client = _create_mock_client("slow_p", SlowProvider())

    agent = Agent(name="SlowAgent", tools=[slow_tool], client=client)
    task: AgentTask = agent.run_background("Execute slow work")

    assert task.is_running
    assert not task.is_done
    assert task.status == "running"

    resp = await task
    stop_heartbeat.set()
    await hb_task

    assert resp.content == "Slow work complete."
    assert task.is_done
    assert task.status == "completed"
    assert heartbeat_ticks >= 8


@pytest.mark.asyncio
async def test_concurrent_http_requests_while_agent_runs_in_background() -> None:
    """
    Integration test: verify 50 concurrent HTTP requests succeed via TestClient
    while a background agent task executes asynchronously.
    """
    app = Application()
    completed_agent = asyncio.Event()

    @tool(name="bg_work", description="Work tool")
    async def bg_work() -> str:
        await asyncio.sleep(0.05)
        completed_agent.set()
        return "done"

    class BackgroundProvider(ProviderInterface):
        name: str = "bg_prov"

        def get_default_model(self) -> str:
            return "bg-model"

        async def send(self, request: LLMRequest) -> LLMResponse:
            if request.messages[-1].role != "tool":
                return LLMResponse(content="", role="assistant", model="bg-model", tool_calls=[ToolCall(id="tc1", name="bg_work", arguments={})])
            return LLMResponse(content="Work finished", role="assistant", model="bg-model")

        async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
            yield ""

    client = _create_mock_client("bg_prov", BackgroundProvider())

    agent = Agent(name="BgWorker", tools=[bg_work], client=client)

    @app.get("/ping")
    async def ping_handler(req: Request) -> Response:
        return json_response({"pong": True})

    http_client = TestClient(app)

    # Launch background agent task
    agent_task = agent.run_background("Start background job")

    # Issue 50 concurrent HTTP requests while agent is running
    async def request_worker() -> int:
        resp = await http_client.get("/ping")
        return resp.status_code

    statuses = await asyncio.gather(*[request_worker() for _ in range(50)])
    assert all(status == 200 for status in statuses)

    # Await agent completion
    response = await agent_task
    assert response.content == "Work finished"
    assert completed_agent.is_set()


# ==============================================================================
# Task Rule, Exception Handling & Cancellation Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_tool_failure_recovery() -> None:
    """Verify tool failure does not crash the agent; the model receives error result and recovers."""
    @tool(name="failing_tool", description="Tool that fails")
    async def failing_tool() -> dict[str, str]:
        raise RuntimeError("External service connection reset.")

    class RecoveryProvider(ProviderInterface):
        name: str = "rec_p"

        def get_default_model(self) -> str:
            return "m"

        async def send(self, request: LLMRequest) -> LLMResponse:
            if request.messages[-1].role != "tool":
                return LLMResponse(content="", role="assistant", model="m", tool_calls=[ToolCall(id="c1", name="failing_tool", arguments={})])
            return LLMResponse(content="I handled the failure gracefully.", role="assistant", model="m")

        async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
            yield ""

    client = _create_mock_client("rec_p", RecoveryProvider())

    agent = Agent(name="RecoveryAgent", tools=[failing_tool], client=client)
    task = agent.run_background("Do work")
    resp = await task
    assert resp.content == "I handled the failure gracefully."
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_agent_task_unhandled_failure_logging_and_await_propagation(caplog: pytest.LogCaptureFixture) -> None:
    """Verify unhandled exception in background agent task is logged via _on_done and propagated on await."""
    class FailingProvider(ProviderInterface):
        name: str = "fail_p"

        def get_default_model(self) -> str:
            return "m"

        async def send(self, request: LLMRequest) -> LLMResponse:
            raise ConnectionResetError("Fatal connection loss to LLM host.")

        async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
            yield ""

    client = _create_mock_client("fail_p", FailingProvider())

    agent = Agent(name="FailAgent", client=client)

    with caplog.at_level(logging.ERROR):
        task = agent.run_background("Will fail", session_id="fail_sess")
        # Give event loop time to run task to completion
        with pytest.raises(ConnectionResetError) as exc_info:
            await task

    assert "Fatal connection loss to LLM host." in str(exc_info.value)
    assert task.status == "failed"
    assert isinstance(task.exception(), ConnectionResetError)
    assert any("Fatal connection loss to LLM host." in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_agent_task_cancellation_and_tracker_shutdown() -> None:
    """Verify task cancellation sets cancelled status and AgentTracker cancels all active tasks."""
    AgentTracker.reset()
    tracker = AgentTracker.get_instance()

    async def infinite_work() -> AgentResponse:
        while True:
            await asyncio.sleep(0.01)

    task1 = AgentTask(infinite_work(), session_id="s1")
    task2 = AgentTask(infinite_work(), session_id="s2")

    assert tracker.active_count == 2
    assert task1.is_running

    # Cancel task1 directly
    assert task1.cancel() is True
    assert task1.status == "cancelled"

    with pytest.raises(asyncio.CancelledError):
        await task1

    assert tracker.active_count == 1

    # Cancel all remaining tasks via tracker
    cancelled_count = await tracker.cancel_all()
    assert cancelled_count == 1
    assert tracker.active_count == 0
    assert task2.status == "cancelled"


# ==============================================================================
# Memory Backend Tests: In-Memory, Redis, and Database
# ==============================================================================


@pytest.mark.asyncio
async def test_in_memory_backend_persistence_and_state() -> None:
    """Verify in-memory backend stores and returns messages and state."""
    backend = InMemoryMemoryBackend()
    session_id = "mem_test_session"

    msg1 = Message(role="user", content="Hello In-Memory")
    msg2 = Message(role="assistant", content="Echo from memory")
    await backend.save_messages(session_id, [msg1, msg2])

    loaded = await backend.load_messages(session_id)
    assert len(loaded) == 2
    assert loaded[0].content == "Hello In-Memory"

    await backend.set_state(session_id, "counter", 42)
    assert await backend.get_state(session_id, "counter") == 42
    assert await backend.exists(session_id) is True

    await backend.clear(session_id)
    assert len(await backend.load_messages(session_id)) == 0
    assert await backend.get_state(session_id, "counter") is None
    assert await backend.exists(session_id) is False


@pytest.mark.asyncio
async def test_redis_memory_backend_real_integration() -> None:
    """
    Test real RedisMemoryBackend against live Redis:
    - Message persistence across separate instances
    - TTL expiry verification
    - Session isolation
    - Concurrent atomic appends to same session
    - Prefix isolation
    - Corrupted JSON recovery
    """
    cfg = _build_test_config()
    backend1 = RedisMemoryBackend(config=cfg, key_prefix="test_ai_agent", default_ttl=3600)
    client = await backend1.get_client()

    sid = "redis_integ_sess"
    await backend1.clear(sid)

    # 1. Message persistence across separate instances
    m1 = Message(role="user", content="Message One")
    m2 = Message(role="assistant", content="Message Two")
    await backend1.save_messages(sid, [m1, m2], ttl=300)

    backend2 = RedisMemoryBackend(config=cfg, key_prefix="test_ai_agent")
    loaded = await backend2.load_messages(sid)
    assert len(loaded) == 2
    assert loaded[0].content == "Message One"
    assert loaded[1].content == "Message Two"

    # 2. TTL verification
    key = backend1._msg_key(sid)
    ttl_val = await client.ttl(key)
    assert ttl_val > 0 and ttl_val <= 300

    # 3. Concurrent atomic appends to same session (No lost updates)
    await backend1.clear(sid)
    async def append_worker(idx: int) -> None:
        await backend1.append_message(sid, Message(role="user", content=f"Concurrent {idx}"))

    await asyncio.gather(*[append_worker(i) for i in range(10)])
    loaded_concurrent = await backend1.load_messages(sid)
    assert len(loaded_concurrent) == 10

    # 4. State persistence
    await backend1.set_state(sid, "flag", True)
    assert await backend1.get_state(sid, "flag") is True

    # 5. Prefix isolation
    backend_alt = RedisMemoryBackend(config=cfg, key_prefix="different_prefix")
    assert await backend_alt.exists(sid) is False

    # 6. Corrupted JSON handling
    await client.rpush(backend1._msg_key("corrupt_sess"), "{not-json")
    with pytest.raises(ValueError) as err:
        await backend1.load_messages("corrupt_sess")
    assert "Corrupted agent memory data in Redis" in str(err.value)

    await backend1.clear(sid)
    await backend1.close()
    await backend2.close()
    await backend_alt.close()


@pytest.mark.asyncio
async def test_database_memory_backend_real_integration() -> None:
    """
    Test real DatabaseMemoryBackend against live PostgreSQL:
    - Automatic table creation
    - Message persistence across separate instances
    - State persistence and upserts
    - Concurrent atomic appends to same session
    - Session clearing isolation
    """
    cfg = _build_test_config()
    pool = DatabasePool(config=cfg)
    await pool.init()

    backend1 = DatabaseMemoryBackend(pool=pool, table_name="test_agent_memories")
    await backend1.ensure_table()

    sid = "db_integ_sess"
    await backend1.clear(sid)

    # 1. Message persistence across separate instances
    m1 = Message(role="user", content="DB Message 1")
    m2 = Message(role="assistant", content="DB Response 1", tool_calls=[ToolCall(id="tc1", name="db_tool", arguments={"q": 1})])
    await backend1.save_messages(sid, [m1, m2])

    backend2 = DatabaseMemoryBackend(pool=pool, table_name="test_agent_memories")
    loaded = await backend2.load_messages(sid)
    assert len(loaded) == 2
    assert loaded[0].content == "DB Message 1"
    assert loaded[1].content == "DB Response 1"
    assert loaded[1].tool_calls is not None
    assert loaded[1].tool_calls[0].name == "db_tool"

    # 2. Concurrent atomic appends to same session (No lost updates)
    await backend1.clear(sid)
    async def append_worker(idx: int) -> None:
        await backend1.append_message(sid, Message(role="user", content=f"Atomic DB {idx}"))

    await asyncio.gather(*[append_worker(i) for i in range(10)])
    loaded_atomic = await backend1.load_messages(sid)
    assert len(loaded_atomic) == 10

    # 3. State persistence & upsert
    await backend1.set_state(sid, "score", 99)
    assert await backend1.get_state(sid, "score") == 99

    # Upsert overwrite
    await backend1.set_state(sid, "score", 100)
    assert await backend1.get_state(sid, "score") == 100
    all_state = await backend1.get_all_state(sid)
    assert all_state == {"score": 100}

    # 4. Session isolation on clear
    await backend1.set_state("other_sess", "score", 50)
    await backend1.clear(sid)
    assert await backend1.exists(sid) is False
    assert await backend1.get_state("other_sess", "score") == 50

    await backend1.clear("other_sess")
    await pool.close()
