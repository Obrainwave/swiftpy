from __future__ import annotations

import asyncio
import datetime
import enum
import functools
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from swiftpy.ai.agents.base_agent import (
    AgentResponse,
    AgentTask,
    AgentTracker,
    BaseAgent,
)
from swiftpy.ai.agents.memory import (
    DatabaseMemoryBackend,
    InMemoryMemoryBackend,
    RedisMemoryBackend,
)
from swiftpy.ai.client import AI, AIClient
from swiftpy.ai.exceptions import AIBudgetExceededError, AIConfigurationError
from swiftpy.ai.middleware import NextHandler
from swiftpy.ai.prompts import PromptLoader, aload_prompt
from swiftpy.ai.providers.base import (
    LLMRequest,
    LLMResponse,
    Message,
    ProviderInterface,
    TokenUsage,
    ToolCall,
)
from swiftpy.ai.tools.base_tool import BaseTool, FunctionTool, tool
from swiftpy.ai.tools.first_party import CalculatorTool, WebSearchTool
from swiftpy.ai.tools.tool_loop import ToolLoop
from swiftpy.core.config import Config
from swiftpy.core.container import Container, bind_container
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import db


def _build_test_config() -> Config:
    cfg = Config()
    cfg.set("ai.default", "openai")
    cfg.set("ai.providers.openai.api_key", "sk-mock-test")
    cfg.set("ai.providers.openai.model", "gpt-4o")
    cfg.set("ai.providers.recording_agent_p8.api_key", "sk-mock-p8")
    cfg.set("ai.providers.recording_agent_p8.model", "gpt-4o")
    cfg.set("ai.redis.host", os.environ.get("REDIS_HOST", "127.0.0.1"))
    cfg.set("ai.redis.port", int(os.environ.get("REDIS_PORT", "6379")))
    cfg.set("ai.redis.password", os.environ.get("REDIS_PASSWORD", "secret"))
    cfg.set("database.connections.postgresql.host", os.environ.get("DB_HOST", "127.0.0.1"))
    cfg.set("database.connections.postgresql.port", int(os.environ.get("DB_PORT", "5432")))
    cfg.set(
        "database.connections.postgresql.database",
        os.environ.get("DB_DATABASE", "swiftpy"),
    )
    cfg.set("database.connections.postgresql.user", os.environ.get("DB_USERNAME", "postgres"))
    cfg.set(
        "database.connections.postgresql.password",
        os.environ.get("DB_PASSWORD", "secret"),
    )
    return cfg


class RecordingProvider(ProviderInterface):
    responses: ClassVar[list[LLMResponse]] = []
    requests: ClassVar[list[LLMRequest]] = []
    delay: ClassVar[float] = 0.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    @property
    def name(self) -> str:
        return "recording_agent_p8"

    def get_default_model(self) -> str:
        return "gpt-4o"

    async def send(self, request: LLMRequest) -> LLMResponse:
        type(self).requests.append(request)
        if type(self).delay > 0:
            await asyncio.sleep(type(self).delay)
        if type(self).responses:
            return type(self).responses.pop(0)
        return LLMResponse(
            content="default reply",
            model=request.model or "gpt-4o",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        yield "unused"


@pytest.mark.asyncio
async def test_agent1_isolated_client_preserves_options_middlewares_and_no_global_mutation() -> None:
    prev_registry = dict(AIClient._registry)
    RecordingProvider.requests = []
    RecordingProvider.responses = [
        LLMResponse(
            content="preserved",
            model="gpt-4o-mini",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
    ]
    seen_middleware_headers: list[str] = []

    async def trace_mw(request: LLMRequest, next_fn: NextHandler) -> LLMResponse:
        seen_middleware_headers.append("ran")
        return await next_fn(request)

    try:
        AIClient.register_provider("recording_agent_p8", RecordingProvider)
        registry_before_agent = dict(AIClient._registry)

        template = (
            AIClient(config=_build_test_config(), default_provider="recording_agent_p8")
            .model("gpt-4o-mini")
            .temperature(0.25)
            .max_tokens(321)
            .top_p(0.85)
            .stop(["END_SEQ"])
            .extra(seed=42)
            .metadata(tenant="acme")
            .use(trace_mw)
        )

        agent = BaseAgent(
            name="OptionAgent",
            system_prompt="You preserve client config.",
            client=template,
        )
        res = await agent.run("hello", session_id="s-opt")

        assert res.content == "preserved"
        assert dict(AIClient._registry) == registry_before_agent
        assert seen_middleware_headers == ["ran"]
        assert len(RecordingProvider.requests) == 1
        req = RecordingProvider.requests[0]
        assert req.model == "gpt-4o-mini"
        assert req.temperature == 0.25
        assert req.max_tokens == 321
        assert req.top_p == 0.85
        assert req.stop == ["END_SEQ"]
        assert req.extra == {"seed": 42}
        assert req.metadata.get("tenant") == "acme"
    finally:
        AIClient._registry = prev_registry


@pytest.mark.asyncio
async def test_agent2_concurrent_shared_session_serializes_without_duplicate_system_or_loss() -> None:
    prev_registry = dict(AIClient._registry)
    RecordingProvider.requests = []
    RecordingProvider.responses = [
        LLMResponse(
            content="answer one",
            model="gpt-4o",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
        LLMResponse(
            content="answer two",
            model="gpt-4o",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
    ]
    RecordingProvider.delay = 0.03
    try:
        AIClient.register_provider("recording_agent_p8", RecordingProvider)
        client = AIClient(config=_build_test_config(), default_provider="recording_agent_p8")
        agent = BaseAgent(name="SharedSessAgent", system_prompt="Only once.", client=client)

        await asyncio.gather(
            agent.run("Prompt 1", session_id="shared-concurrent"),
            agent.run("Prompt 2", session_id="shared-concurrent"),
        )

        msgs = await agent.get_session_memory("shared-concurrent").get_messages()
        system_msgs = [m for m in msgs if m.role == "system"]
        user_contents = sorted(m.content for m in msgs if m.role == "user")
        assistant_contents = sorted(m.content for m in msgs if m.role == "assistant")

        assert len(system_msgs) == 1
        assert user_contents == ["Prompt 1", "Prompt 2"]
        assert assistant_contents == ["answer one", "answer two"]
    finally:
        RecordingProvider.delay = 0.0
        AIClient._registry = prev_registry


@pytest.mark.asyncio
async def test_agent2_parallel_tool_calls_recorded_once_canonical_json_and_turn_iterations() -> None:
    prev_registry = dict(AIClient._registry)
    RecordingProvider.requests = []

    @tool(description="Return structured lookup payload")
    def lookup_item(item_id: int) -> dict[str, Any]:
        return {"id": item_id, "tags": ["ok"]}

    tc1 = ToolCall(id="call_1", name="lookup_item", arguments={"item_id": 1})
    tc2 = ToolCall(id="call_2", name="lookup_item", arguments={"item_id": 2})

    RecordingProvider.responses = [
        LLMResponse(
            content="",
            model="gpt-4o",
            tool_calls=[tc1, tc2],
            finish_reason="tool_calls",
            usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        ),
        LLMResponse(
            content="Both items found.",
            model="gpt-4o",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=30, completion_tokens=10, total_tokens=40),
        ),
    ]
    try:
        AIClient.register_provider("recording_agent_p8", RecordingProvider)
        client = AIClient(config=_build_test_config(), default_provider="recording_agent_p8")
        agent = BaseAgent(
            name="ParallelAgent",
            system_prompt="Use tools.",
            tools=[lookup_item],
            client=client,
        )

        res = await agent.run("Fetch items 1 and 2", session_id="parallel-mem")
        assert res.iterations == 2
        assert len(res.executed_calls) == 2

        msgs = await agent.get_session_memory("parallel-mem").get_messages()
        assistant_tool_turns = [m for m in msgs if m.role == "assistant" and m.tool_calls]
        tool_msgs = [m for m in msgs if m.role == "tool"]

        assert len(assistant_tool_turns) == 1
        assert assistant_tool_turns[0].tool_calls is not None
        assert [c.id for c in assistant_tool_turns[0].tool_calls] == ["call_1", "call_2"]
        assert len(tool_msgs) == 2
        assert json.loads(tool_msgs[0].content) == {"id": 1, "tags": ["ok"]}
        assert json.loads(tool_msgs[1].content) == {"id": 2, "tags": ["ok"]}
    finally:
        AIClient._registry = prev_registry


@pytest.mark.asyncio
async def test_agent3_allowed_tools_filtered_and_max_tool_calls_disables_tools_on_synthesis() -> None:
    prev_registry = dict(AIClient._registry)
    RecordingProvider.requests = []

    @tool(description="Allowed alpha tool")
    def alpha(x: int) -> int:
        return x + 1

    @tool(description="Secret beta tool")
    def beta(x: int) -> int:
        return x * 10

    tc1 = ToolCall(id="call_a", name="alpha", arguments={"x": 5})
    RecordingProvider.responses = [
        LLMResponse(
            content="",
            model="gpt-4o",
            tool_calls=[tc1],
            finish_reason="tool_calls",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
        LLMResponse(
            content="Final answer: 6",
            model="gpt-4o",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=12, completion_tokens=4, total_tokens=16),
        ),
    ]
    try:
        AIClient.register_provider("recording_agent_p8", RecordingProvider)
        client = AIClient(config=_build_test_config(), default_provider="recording_agent_p8")
        agent = BaseAgent(
            name="BudgetAgent",
            system_prompt="Obey limits.",
            tools=[alpha, beta],
            allowed_tools=["alpha"],
            max_tool_calls=1,
            client=client,
        )

        res = await agent.run("Run alpha once", session_id="limit-tools")
        assert res.content == "Final answer: 6"
        assert len(RecordingProvider.requests) == 2

        first_req_tools = RecordingProvider.requests[0].tools
        assert first_req_tools is not None
        assert [t["function"]["name"] for t in first_req_tools] == ["alpha"]

        second_req_tools = RecordingProvider.requests[1].tools
        assert second_req_tools is None
    finally:
        AIClient._registry = prev_registry


@pytest.mark.asyncio
async def test_agent3_cumulative_token_and_cost_budgets_enforced() -> None:
    prev_registry = dict(AIClient._registry)

    @tool(description="Ping tool")
    def ping() -> str:
        return "pong"

    try:
        AIClient.register_provider("recording_agent_p8", RecordingProvider)
        tc = ToolCall(id="tc_budget", name="ping", arguments={})

        # 1. Token budget exceeded on BaseAgent
        RecordingProvider.requests = []
        RecordingProvider.responses = [
            LLMResponse(
                content="",
                model="gpt-4o",
                tool_calls=[tc],
                usage=TokenUsage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
            ),
            LLMResponse(
                content="over budget",
                model="gpt-4o",
                usage=TokenUsage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
            ),
        ]
        agent = BaseAgent(
            name="TokenBudgetAgent",
            system_prompt="Sys",
            tools=[ping],
            max_total_tokens=50,
            client=AIClient(config=_build_test_config(), default_provider="recording_agent_p8"),
        )
        with pytest.raises(AIBudgetExceededError):
            await agent.run("hello", session_id="token-budget")

        # 2. Cost budget exceeded on AIClient.auto_execute_tools
        RecordingProvider.requests = []
        RecordingProvider.responses = [
            LLMResponse(
                content="",
                model="gpt-4o",
                tool_calls=[tc],
                usage=TokenUsage(prompt_tokens=100_000, completion_tokens=50_000, total_tokens=150_000),
            ),
        ]
        cost_client = (
            AIClient(config=_build_test_config(), default_provider="recording_agent_p8")
            .model("gpt-4o")
            .tools([ping])
            .auto_execute_tools(max_cost=0.01)
            .user("spend too much")
        )
        with pytest.raises(AIBudgetExceededError):
            await cost_client.send()
    finally:
        AIClient._registry = prev_registry


@pytest.mark.asyncio
async def test_agent4_in_memory_ttl_and_state_only_exists() -> None:
    backend = InMemoryMemoryBackend(default_ttl=1)
    await backend.set_state("state-only", "step", 2, ttl=60)
    assert await backend.exists("state-only") is True

    await backend.save_messages("ephemeral", [Message(role="user", content="temp")], ttl=1)
    assert await backend.exists("ephemeral") is True

    # Simulate expiry cleanly without long sleeps if backed by monotonic expiry timestamps
    await asyncio.sleep(1.05)
    assert await backend.exists("ephemeral") is False
    assert await backend.load_messages("ephemeral") == []
    assert await backend.exists("state-only") is True


@pytest.mark.asyncio
async def test_agent4_redis_atomic_state_state_only_exists_and_pooled_client() -> None:
    cfg = _build_test_config()
    backend1 = RedisMemoryBackend(config=cfg, key_prefix="test_ai_p8_agent4", default_ttl=120)
    backend2 = RedisMemoryBackend(config=cfg, key_prefix="test_ai_p8_agent4", default_ttl=120)
    sid = "redis_atomic_state_sess"
    try:
        await backend1.clear(sid)
        client1 = await backend1.get_client()
        client2 = await backend2.get_client()
        assert client1.connection_pool is client2.connection_pool

        await asyncio.gather(*(backend1.set_state(sid, f"k{i}", i) for i in range(20)))
        assert await backend1.exists(sid) is True
        all_state = await backend1.get_all_state(sid)
        assert all_state == {f"k{i}": i for i in range(20)}
    finally:
        await backend1.clear(sid)
        await backend1.close()
        await backend2.close()


@pytest.mark.asyncio
async def test_agent4_database_memory_long_table_ttl_exists_and_transaction_reuse() -> None:
    cfg = _build_test_config()
    pool = DatabasePool(config=cfg)
    await pool.init()
    long_table = "agent_messages_with_very_long_table_name_48_chr"
    backend = DatabaseMemoryBackend(pool=pool, table_name=long_table, default_ttl=120)
    assert len(f"idx_{backend.messages_table}_session") > 63
    await backend.ensure_table()
    sid_state = "pg_state_only"
    sid_ttl = "pg_ttl_sess"
    sid_tx = "pg_tx_sess"

    test_container = Container()
    test_container.instance(DatabasePool, pool)
    try:
        await backend.clear(sid_state)
        await backend.clear(sid_ttl)
        await backend.clear(sid_tx)

        # 1. State-only existence check
        await backend.set_state(sid_state, "phase", "p8")
        assert await backend.exists(sid_state) is True

        # 2. TTL expiration check
        await backend.save_messages(sid_ttl, [Message(role="user", content="short lived")], ttl=1)
        assert await backend.exists(sid_ttl) is True
        await asyncio.sleep(1.1)
        assert await backend.exists(sid_ttl) is False
        assert await backend.load_messages(sid_ttl) == []

        # 3. Active transaction connection reuse & rollback atomicity
        with bind_container(test_container):
            try:
                async with db.transaction():
                    await backend.save_messages(sid_tx, [Message(role="user", content="tx msg")])
                    assert len(await backend.load_messages(sid_tx)) == 1
                    raise RuntimeError("trigger rollback")
            except RuntimeError:
                pass
        assert await backend.exists(sid_tx) is False
    finally:
        await backend.clear(sid_state)
        await backend.clear(sid_ttl)
        await backend.clear(sid_tx)
        await pool.close()


@pytest.mark.asyncio
async def test_agent5_agent_task_cancel_preserves_completed_status() -> None:
    AgentTracker.reset()

    async def quick_done() -> AgentResponse:
        return AgentResponse(content="done")

    task = AgentTask(quick_done(), session_id="s-done", task_id="t-done")
    res = await task
    assert res.content == "done"
    assert task.status == "completed"
    assert task.cancel() is False
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_tool1_tool2_serialization_validation_redaction_concurrency_and_anthropic_input() -> None:
    prev_registry = dict(AIClient._registry)
    max_active = 0
    current_active = 0

    class StrictNestedTool(BaseTool):
        @property
        def name(self) -> str:
            return "strict_nested"

        @property
        def description(self) -> str:
            return "Validate nested object and array schemas"

        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "integer"}},
                    "meta": {
                        "type": "object",
                        "properties": {"flag": {"type": "boolean"}},
                        "required": ["flag"],
                        "additionalProperties": False,
                    },
                },
                "required": ["items"],
            }

        async def handle(self, **kwargs: Any) -> Any:
            return datetime.datetime(2026, 11, 15, 10, 0, tzinfo=datetime.UTC)

    @tool(description="Leaks secret on failure")
    async def secret_fail_tool() -> str:
        raise RuntimeError("Upstream failed with sk-secret-abc123 and Bearer topsecret-jwt-token")

    @tool(description="Concurrency probe")
    async def concurrent_probe(idx: int) -> int:
        nonlocal max_active, current_active
        current_active += 1
        max_active = max(max_active, current_active)
        await asyncio.sleep(0.02)
        current_active -= 1
        return idx

    RecordingProvider.requests = []
    RecordingProvider.responses = [
        LLMResponse(
            content="",
            model="gpt-4o",
            tool_calls=[
                ToolCall(id="tc_ok", name="strict_nested", arguments={"items": [1, 2], "meta": {"flag": True}}),
                ToolCall(id="tc_bad_arr", name="strict_nested", arguments={"items": ["not-int"]}),
                ToolCall(
                    id="tc_bad_obj",
                    name="strict_nested",
                    arguments={"items": [1], "meta": {"flag": True, "extra": 1}},
                ),
                ToolCall(id="tc_secret", name="secret_fail_tool", arguments={}),
                *(ToolCall(id=f"tc_conc_{i}", name="concurrent_probe", arguments={"idx": i}) for i in range(5)),
            ],
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
        LLMResponse(
            content="handled",
            model="gpt-4o",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
    ]

    try:
        AIClient.register_provider("recording_agent_p8", RecordingProvider)
        client = AIClient(config=_build_test_config(), default_provider="recording_agent_p8")
        client.user("run tools")

        loop = ToolLoop(
            client=client,
            tools=[StrictNestedTool(), secret_fail_tool, concurrent_probe],
            max_concurrency=2,
        )
        resp = await loop.run()
        assert resp.content == "handled"
        assert max_active <= 2

        by_id = {r.tool_call_id: r for r in loop.executed_results}
        assert by_id["tc_ok"].is_error is False
        assert by_id["tc_bad_arr"].is_error is True
        assert by_id["tc_bad_obj"].is_error is True
        assert by_id["tc_secret"].is_error is True
        secret_msg = str(by_id["tc_secret"].result)
        assert "sk-secret-abc123" not in secret_msg
        assert "topsecret-jwt-token" not in secret_msg

        # Non-mapping Anthropic tool_use input must produce ToolCall cleanly / error result safely
        anthropic_bad = LLMResponse(
            content="",
            model="claude-3-5-sonnet",
            raw={"content": [{"type": "tool_use", "id": "tu_1", "name": "strict_nested", "input": ["bad"]}]},
        )
        detected = loop._detect_tool_calls(anthropic_bad)
        assert len(detected) == 1
        assert isinstance(detected[0].arguments, dict)
        assert "__invalid_tool_arguments__" in detected[0].arguments or "_raw" in detected[0].arguments
    finally:
        AIClient._registry = prev_registry


@pytest.mark.asyncio
async def test_tool3_calculator_bounds_complex_keywords_and_nonblocking() -> None:
    calc = CalculatorTool()

    res_exp = await calc.handle(expression="(9**99)**100")
    assert isinstance(res_exp, dict) and "error" in res_exp and "result" not in res_exp

    res_complex = await calc.handle(expression="(-1)**0.5")
    assert isinstance(res_complex, dict) and "error" in res_complex and "result" not in res_complex

    res_kw = await calc.handle(expression="abs(-1, bad=1)")
    assert isinstance(res_kw, dict) and "error" in res_kw and "result" not in res_kw

    assert await calc.handle(expression="2 + 3 * 4") == {
        "expression": "2 + 3 * 4",
        "result": 14,
    }


class _ModeEnum(enum.IntEnum):
    FAST = 1
    ACCURATE = 2


@pytest.mark.asyncio
async def test_tool4_function_tool_enum_posonly_partial_callable_newline_and_websearch() -> None:
    def with_enum_and_posonly(count: int, /, mode: _ModeEnum = _ModeEnum.FAST) -> dict[str, Any]:
        return {"count": count, "mode_name": mode.name, "is_enum": isinstance(mode, _ModeEnum)}

    ft = FunctionTool(with_enum_and_posonly)
    assert ft.parameters["properties"]["mode"]["type"] == "integer"
    assert ft.parameters["properties"]["mode"]["enum"] == [1, 2]
    res = await ft.handle(count=3, mode=2)
    assert res == {"count": 3, "mode_name": "ACCURATE", "is_enum": True}

    def add_base(base: int, delta: int) -> int:
        return base + delta

    partial_tool = FunctionTool(functools.partial(add_base, 10))
    assert await partial_tool.handle(delta=5) == 15

    class AsyncCallableObj:
        async def __call__(self, x: int) -> int:
            return x * 3

    callable_tool = FunctionTool(AsyncCallableObj())
    assert await callable_tool.handle(x=4) == 12

    with pytest.raises(ValueError):
        FunctionTool(add_base, name="bad_name\n")

    def unresolved(x: Any) -> int:
        return int(x)

    unresolved.__annotations__["x"] = "MissingForwardType"

    with pytest.raises(NameError):
        FunctionTool(unresolved)

    search = WebSearchTool()
    with pytest.raises((TypeError, ValueError)):
        await search.handle(query="swiftpy", max_results=True)

    with pytest.raises(AIConfigurationError):
        await search.handle(query="swiftpy", max_results=2)


def test_prompt1_single_pass_unescaping_literal_backslash_and_no_var_load(tmp_path: Path) -> None:
    loader = PromptLoader(base_dir=tmp_path)

    rendered = loader.render(
        r"Path C:\\{{ folder }} -> {{ value }}",
        folder="src",
        value=r"escaped \{{ kept }}",
    )
    assert rendered == r"Path C:\src -> escaped \{{ kept }}"

    escaped_only = tmp_path / "escaped.md"
    escaped_only.write_text(r"Use \{{ placeholder }} literally.", encoding="utf-8")
    assert loader.load("escaped") == "Use {{ placeholder }} literally."


@pytest.mark.asyncio
async def test_prompt2_base_dir_cache_isolation_recursive_dump_and_async_aprompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dir_a = tmp_path / "prompts_a"
    dir_b = tmp_path / "prompts_b"
    (dir_a / "sub").mkdir(parents=True)
    dir_b.mkdir(parents=True)

    (dir_a / "greet.md").write_text("From A: {{ who }}", encoding="utf-8")
    (dir_a / "sub" / "nested.md").write_text("Nested A: {{ who }}", encoding="utf-8")
    (dir_b / "greet.md").write_text("From B: {{ who }}", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    loader_a = PromptLoader(base_dir=dir_a)
    loader_a.dump_cache()

    loader_b = PromptLoader(base_dir=dir_b)
    assert loader_b.load("greet", who="World") == "From B: World"

    cache_file = tmp_path / "explicit.json"
    out_path = loader_a.dump_cache(cache_path=cache_file)
    dumped = json.loads(out_path.read_text(encoding="utf-8"))
    assert "greet" in dumped
    assert "sub/nested" in dumped

    assert await loader_a.aload("greet", who="Async") == "From A: Async"
    prev_default = PromptLoader._instance
    try:
        PromptLoader._instance = loader_a
        assert await aload_prompt("greet", who="Helper") == "From A: Helper"

        client = await AIClient(config=_build_test_config()).aprompt("greet", who="Client")
        assert client._messages[-1].content == "From A: Client"

        ai_client = await AI.aprompt("greet", who="Facade")
        assert ai_client._messages[-1].content == "From A: Facade"
    finally:
        PromptLoader._instance = prev_default
