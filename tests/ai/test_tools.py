from __future__ import annotations

import asyncio
import datetime
import enum
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, ClassVar, Literal, Optional, Union

try:
    import httpx
except ImportError:
    httpx = None

import pytest

from swiftpy.ai.client import AIClient
from swiftpy.ai.conversation import Conversation
from swiftpy.ai.exceptions import AgentLoopError
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.providers.anthropic_provider import AnthropicProvider
from swiftpy.ai.providers.base import (
    LLMRequest,
    LLMResponse,
    Message,
    ProviderInterface,
    TokenUsage,
    ToolCall,
)
from swiftpy.ai.providers.openai_provider import OpenAIProvider
from swiftpy.ai.tools.base_tool import BaseTool, FunctionTool, tool
from swiftpy.ai.tools.first_party import (
    CalculatorTool,
    DateTimeTool,
    WebSearchTool,
)
from swiftpy.core.config import Config

# ==============================================================================
# Test Fixtures & Scoped Mock Provider & Mock HTTP Client
# ==============================================================================


class MockHttpResponse:
    def __init__(self, status_code: int, data: dict[str, Any], text: str = "") -> None:
        self.status_code = status_code
        self._data = data
        self.text = text or json.dumps(data)
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._data


class MockHttpClient:
    def __init__(self, handler: Callable[..., MockHttpResponse]) -> None:
        self._handler = handler
        self.is_closed = False

    async def post(self, url: str, **kwargs: Any) -> MockHttpResponse:
        return self._handler(url=url, **kwargs)

    async def aclose(self) -> None:
        self.is_closed = True


@pytest.fixture
def mock_config() -> Config:
    return Config(
        {
            "ai": {
                "enabled": True,
                "default": "mock_tools_provider",
                "providers": {
                    "mock_tools_provider": {
                        "api_key": "test-mock-key",
                    },
                },
            }
        }
    )


class ScopedMockProvider(ProviderInterface):
    """
    Mock provider for isolated, non-shared multi-turn autonomous tool interaction.
    """

    _test_responses: ClassVar[list[LLMResponse]] = []
    _received_requests: ClassVar[list[LLMRequest]] = []

    @classmethod
    def set_responses(cls, responses: list[LLMResponse]) -> None:
        cls._test_responses = list(responses)
        cls._received_requests.clear()

    @property
    def name(self) -> str:
        return "mock_tools_provider"

    def get_default_model(self) -> str:
        return "mock-agent-v1"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self._received_requests.append(request)
        if self._test_responses:
            return self._test_responses.pop(0)
        return LLMResponse(
            content="Default fallback final answer.",
            role="assistant",
            model=self.get_default_model(),
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        yield "streaming-not-used-in-tool-test"


# ==============================================================================
# 1. BaseTool & FunctionTool & Schema Inference & Argument Validation
# ==============================================================================


class StatusEnum(enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING = "pending"


@pytest.mark.asyncio
async def test_function_tool_complex_schema_inference() -> None:
    """Verify schema generation for Optional, Union, Literal, Enum, list, dict, and defaults."""

    def complex_operation(
        user_id: int,
        status: StatusEnum,
        mode: Literal["fast", "accurate"] = "fast",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        note: Optional[str] = None,  # noqa: UP045
        priority: Union[int, str] = 1,  # noqa: UP007
    ) -> dict[str, Any]:
        """A complex function with rich Python type annotations."""
        return {"user_id": user_id, "status": status.value, "mode": mode}

    ft = FunctionTool(fn=complex_operation, name="complex_op")
    schema = ft.parameters

    assert schema["type"] == "object"
    props = schema["properties"]

    # int
    assert props["user_id"]["type"] == "integer"
    # Enum
    assert props["status"]["type"] == "string"
    assert props["status"]["enum"] == ["active", "inactive", "pending"]
    # Literal
    assert props["mode"]["type"] == "string"
    assert props["mode"]["enum"] == ["fast", "accurate"]
    # list | None
    assert props["tags"]["type"] == "array"
    assert props["tags"]["nullable"] is True
    # dict | None
    assert props["metadata"]["type"] == "object"
    assert props["metadata"]["nullable"] is True
    # Optional[str]
    assert props["note"]["type"] == "string"
    assert props["note"]["nullable"] is True
    # Union[int, str]
    assert "anyOf" in props["priority"]

    # Required fields should only include non-default, non-nullable parameters
    assert "user_id" in schema["required"]
    assert "status" in schema["required"]
    assert "mode" not in schema["required"]
    assert "tags" not in schema["required"]
    assert "note" not in schema["required"]


@pytest.mark.asyncio
async def test_function_tool_argument_validation() -> None:
    """Verify runtime validation catches missing, wrong-typed, or unexpected arguments."""

    def greet_user(name: str, count: int = 1) -> str:
        return f"Hello {name} * {count}"

    ft = FunctionTool(fn=greet_user, name="greet_user")

    # 1. Missing required argument
    with pytest.raises(TypeError, match="Missing required parameter 'name'"):
        await ft.handle()

    # 2. Incorrect type for integer (passing string)
    with pytest.raises(TypeError, match="Parameter 'count' must be an integer"):
        await ft.handle(name="Alice", count="three")

    # 3. Incorrect type for integer (passing boolean: bool is subclass of int, so must be guarded)
    with pytest.raises(TypeError, match="Parameter 'count' must be an integer"):
        await ft.handle(name="Alice", count=True)

    # 4. Incorrect type for string (passing integer)
    with pytest.raises(TypeError, match="Parameter 'name' must be a string"):
        await ft.handle(name=123, count=2)

    # 5. Unexpected argument when fn does not take **kwargs
    with pytest.raises(TypeError, match="Unexpected parameter 'extra'"):
        await ft.handle(name="Alice", extra="forbidden")

    # 6. Valid execution
    res = await ft.handle(name="Alice", count=3)
    assert res == "Hello Alice * 3"


@pytest.mark.asyncio
async def test_function_tool_sync_blocking_offloaded_to_thread() -> None:
    """Verify synchronous functions are offloaded to worker threads via asyncio.to_thread."""

    def blocking_computation(x: int) -> int:
        time.sleep(0.05)
        return x * 10

    ft = FunctionTool(fn=blocking_computation, name="blocking_comp")
    res = await ft.handle(x=5)
    assert res == 50


@pytest.mark.asyncio
async def test_tool_decorator_and_naming_validation() -> None:
    """Verify @tool decorator syntax, invalid tool names, and bound methods."""

    @tool(name="valid_tool_name_123")
    async def sample_tool(a: int) -> int:
        return a + 1

    assert sample_tool.name == "valid_tool_name_123"
    assert await sample_tool.handle(a=10) == 11

    # Invalid tool names rejected
    with pytest.raises(ValueError, match="Invalid tool name"):
        FunctionTool(fn=lambda x: x, name="invalid name with spaces!")

    with pytest.raises(ValueError, match="Invalid tool name"):
        FunctionTool(fn=lambda x: x, name="invalid/slash")


# ==============================================================================
# 2. Calculator Tool Security & Resource Limits
# ==============================================================================


@pytest.mark.asyncio
async def test_calculator_tool_security_and_ast_rejections() -> None:
    calc = CalculatorTool(max_expression_length=100, max_ast_depth=10)

    # 1. Attribute access rejected (().__class__)
    res = await calc.handle(expression="().__class__")
    assert "error" in res
    assert "Disallowed expression element" in res["error"] or "Only direct math" in res["error"]

    # 2. Attribute access on numbers ((1).__class__.__mro__)
    res = await calc.handle(expression="(1).__class__.__mro__")
    assert "error" in res

    # 3. Subscript access rejected ([1, 2, 3][0])
    res = await calc.handle(expression="[1, 2, 3][0]")
    assert "error" in res
    assert "Disallowed expression element" in res["error"]

    # 4. Exponent explosion rejected (2 ** 100000000)
    res = await calc.handle(expression="2 ** 100000000")
    assert "error" in res
    assert "Exponent too large" in res["error"]

    # 5. Negative square root rejected
    res = await calc.handle(expression="sqrt(-1)")
    assert "error" in res
    assert "Math domain error" in res["error"]

    # 6. Expression length limit exceeded
    long_expr = "1 + " * 60 + "1"
    res = await calc.handle(expression=long_expr)
    assert "error" in res
    assert "exceeds maximum allowed limit" in res["error"]

    # 7. AST recursion depth limit exceeded
    nested_expr = "(" * 12 + "1" + " + 1)" * 12  # > 10 levels deep
    res = await calc.handle(expression=nested_expr)
    assert "error" in res
    assert "depth exceeds" in res["error"]

    # 8. Type validation: passing non-string
    with pytest.raises(TypeError, match="expression must be a string"):
        await calc.handle(expression=123)  # type: ignore[arg-type]


# ==============================================================================
# 3. DateTimeTool Precision & WebSearchTool Robustness
# ==============================================================================


@pytest.mark.asyncio
async def test_date_time_tool_timezone_precision_and_fallback() -> None:
    dt = DateTimeTool()

    # Asia/Tokyo timezone must have exact +09:00 UTC offset (9 * 3600 seconds)
    res_tokyo = await dt.handle(timezone="Asia/Tokyo")
    parsed = datetime.datetime.fromisoformat(res_tokyo["iso"])
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 9 * 3600

    # Invalid timezone falls back to UTC
    res_invalid = await dt.handle(timezone="NonExistent/Timezone_123")
    assert res_invalid["timezone"] == "UTC"

    # Invalid strftime format falls back to ISO
    res_fmt_fallback = await dt.handle(format="%Y-%m-%d %Q")
    assert "datetime" in res_fmt_fallback

    # Type validation
    with pytest.raises(TypeError, match="timezone must be a string"):
        await dt.handle(timezone=123)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_web_search_tool_timeout_and_error_handling() -> None:
    # 1. Search function timeout
    async def hanging_search(q: str, max_results: int = 5) -> list[dict[str, str]]:
        await asyncio.sleep(0.5)
        return [{"title": "Done", "snippet": "", "url": ""}]

    search_tool = WebSearchTool(search_fn=hanging_search, timeout=0.05)
    res = await search_tool.handle(query="hanging query")
    assert "error" in res
    assert "timed out" in res["error"]

    # 2. Search function exception recovery
    def failing_search(q: str, max_results: int = 5) -> Any:
        raise ConnectionError("Network down")

    fail_tool = WebSearchTool(search_fn=failing_search)
    res_fail = await fail_tool.handle(query="test fail")
    assert "error" in res_fail
    assert "Network down" in res_fail["error"]

    # 3. Invalid max_results validation
    with pytest.raises(ValueError, match="positive integer > 0"):
        await fail_tool.handle(query="valid", max_results=0)


# ==============================================================================
# 4. OpenAI Wire-Level Protocol Compatibility
# ==============================================================================


@pytest.mark.asyncio
async def test_openai_raw_wire_payload_and_multi_tool_calls() -> None:
    """
    Test OpenAI raw HTTP wire-level parsing:
    - Multiple tool calls in choices[0].message.tool_calls.
    - JSON-string argument parsing.
    - Invalid JSON argument fallback.
    - Preserving finish_reason='tool_calls'.
    - Wire formatting of outgoing messages with role='tool'.
    """
    captured_payloads: list[dict[str, Any]] = []

    def handler(url: str, **kwargs: Any) -> MockHttpResponse:
        if "json" in kwargs:
            captured_payloads.append(kwargs["json"])
        return MockHttpResponse(
            200,
            {
                "id": "chatcmpl-tool-test",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "Let me calculate those numbers.",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "calculator",
                                        "arguments": json.dumps({"expression": "10 + 20"}),
                                    },
                                },
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "calculator",
                                        "arguments": "invalid-json-string{",
                                    },
                                },
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 15, "completion_tokens": 25, "total_tokens": 40},
            },
        )

    mock_client = MockHttpClient(handler)
    pool = AIHttpPool(client=mock_client)
    provider = OpenAIProvider(config={"api_key": "sk-mock-key"}, http_pool=pool)

    # 1. Send request with tools and history including a previous tool turn
    req = LLMRequest(
        messages=[
            Message(role="user", content="Add 10 and 20"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="call_prev", name="calculator", arguments={"expression": "5+5"})],
            ),
            Message(role="tool", content='{"result": 10}', tool_call_id="call_prev", name="calculator"),
        ],
        tools=[CalculatorTool().to_dict()],
    )

    resp = await provider.send(req)

    # 2. Verify incoming wire response parsing
    assert resp.finish_reason == "tool_calls"
    assert resp.content == "Let me calculate those numbers."
    assert len(resp.tool_calls) == 2

    # First call: parsed JSON-string
    tc1 = resp.tool_calls[0]
    assert tc1.id == "call_1"
    assert tc1.name == "calculator"
    assert tc1.arguments == {"expression": "10 + 20"}

    # Second call: invalid JSON string preserved under 'raw'
    tc2 = resp.tool_calls[1]
    assert tc2.id == "call_2"
    assert tc2.name == "calculator"
    assert tc2.arguments == {"raw": "invalid-json-string{"}

    # 3. Verify outgoing wire request formatting
    assert len(captured_payloads) == 1
    sent_payload = captured_payloads[0]
    assert "tools" in sent_payload
    assert sent_payload["tools"][0]["type"] == "function"
    assert sent_payload["tools"][0]["function"]["name"] == "calculator"

    # Verify outgoing message structure matches OpenAI specifications
    out_msgs = sent_payload["messages"]
    assert out_msgs[1]["role"] == "assistant"
    assert "tool_calls" in out_msgs[1]
    assert out_msgs[2]["role"] == "tool"
    assert out_msgs[2]["tool_call_id"] == "call_prev"
    assert out_msgs[2]["content"] == '{"result": 10}'


# ==============================================================================
# 5. Anthropic Wire-Level Protocol Compatibility
# ==============================================================================


@pytest.mark.asyncio
async def test_anthropic_raw_wire_payload_and_multi_tool_results() -> None:
    """
    Test Anthropic raw HTTP wire-level parsing and formatting:
    - Multiple tool_use blocks in content array.
    - stop_reason='tool_use'.
    - Merging consecutive role='tool' results into single user turn with tool_result blocks.
    - Input_schema serialization.
    """
    captured_payloads: list[dict[str, Any]] = []

    def handler(url: str, **kwargs: Any) -> MockHttpResponse:
        if "json" in kwargs:
            captured_payloads.append(kwargs["json"])
        return MockHttpResponse(
            200,
            {
                "id": "msg_tool_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-5-sonnet-20241022",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "I will check the time and do math."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01_time",
                        "name": "date_time",
                        "input": {"timezone": "Asia/Tokyo"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_02_calc",
                        "name": "calculator",
                        "input": {"expression": "25 * 4"},
                    },
                ],
                "usage": {"input_tokens": 30, "output_tokens": 40},
            },
        )

    mock_client = MockHttpClient(handler)
    pool = AIHttpPool(client=mock_client)
    provider = AnthropicProvider(config={"api_key": "ant-mock-key"}, http_pool=pool)

    # 1. Send request with tools and consecutive tool results
    req = LLMRequest(
        messages=[
            Message(role="user", content="Perform operations"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="toolu_prev1", name="calculator", arguments={"expression": "1+1"}),
                    ToolCall(id="toolu_prev2", name="calculator", arguments={"expression": "2+2"}),
                ],
            ),
            Message(role="tool", content='{"result": 2}', tool_call_id="toolu_prev1", name="calculator"),
            Message(role="tool", content='{"result": 4}', tool_call_id="toolu_prev2", name="calculator"),
        ],
        tools=[CalculatorTool().to_dict(), DateTimeTool().to_dict()],
    )

    resp = await provider.send(req)

    # 2. Verify Anthropic response parsing
    assert resp.finish_reason == "tool_use"
    assert resp.content == "I will check the time and do math."
    assert len(resp.tool_calls) == 2
    assert resp.tool_calls[0].id == "toolu_01_time"
    assert resp.tool_calls[0].name == "date_time"
    assert resp.tool_calls[0].arguments == {"timezone": "Asia/Tokyo"}
    assert resp.tool_calls[1].id == "toolu_02_calc"
    assert resp.tool_calls[1].name == "calculator"
    assert resp.tool_calls[1].arguments == {"expression": "25 * 4"}

    # 3. Verify outgoing wire format meets Anthropic requirements
    assert len(captured_payloads) == 1
    sent_payload = captured_payloads[0]
    assert "tools" in sent_payload
    # Anthropic expects 'input_schema' rather than 'parameters'
    assert "input_schema" in sent_payload["tools"][0]

    messages = sent_payload["messages"]
    # The consecutive role="tool" messages must be merged into a single user message
    # containing two tool_result blocks
    last_user_msg = messages[-1]
    assert last_user_msg["role"] == "user"
    assert isinstance(last_user_msg["content"], list)
    assert len(last_user_msg["content"]) == 2
    assert last_user_msg["content"][0]["type"] == "tool_result"
    assert last_user_msg["content"][0]["tool_use_id"] == "toolu_prev1"
    assert last_user_msg["content"][1]["type"] == "tool_result"
    assert last_user_msg["content"][1]["tool_use_id"] == "toolu_prev2"


# ==============================================================================
# 6. Parallel Concurrent Tool Execution (Timing & Fault Isolation)
# ==============================================================================


@pytest.mark.asyncio
async def test_parallel_concurrent_tool_execution(mock_config: Config) -> None:
    """
    Verify that multiple tool calls emitted in a single turn execute concurrently
    (overlapping start/end intervals), with isolated error handling and order preservation.
    """
    timestamps: dict[str, dict[str, float]] = {}

    class TimingToolA(BaseTool):
        @property
        def name(self) -> str:
            return "tool_a"

        @property
        def description(self) -> str:
            return "Timing tool A"

        @property
        def parameters(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}, "required": []}

        async def handle(self, *args: Any, **kwargs: Any) -> dict[str, str]:
            timestamps["tool_a"] = {"start": time.perf_counter()}
            await asyncio.sleep(0.08)
            timestamps["tool_a"]["end"] = time.perf_counter()
            return {"status": "ok_a"}

    class TimingToolB(BaseTool):
        @property
        def name(self) -> str:
            return "tool_b"

        @property
        def description(self) -> str:
            return "Timing tool B"

        @property
        def parameters(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}, "required": []}

        async def handle(self, *args: Any, **kwargs: Any) -> dict[str, str]:
            timestamps["tool_b"] = {"start": time.perf_counter()}
            await asyncio.sleep(0.08)
            timestamps["tool_b"]["end"] = time.perf_counter()
            return {"status": "ok_b"}

    class FailingToolC(BaseTool):
        @property
        def name(self) -> str:
            return "tool_c"

        @property
        def description(self) -> str:
            return "Failing tool C"

        @property
        def parameters(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}, "required": []}

        async def handle(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Tool C simulated failure")

    responses = [
        LLMResponse(
            content="",
            role="assistant",
            model="mock-agent-v1",
            tool_calls=[
                ToolCall(id="tc_a", name="tool_a", arguments={}),
                ToolCall(id="tc_b", name="tool_b", arguments={}),
                ToolCall(id="tc_c", name="tool_c", arguments={}),
            ],
            usage=TokenUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
        ),
        LLMResponse(
            content="Parallel execution finished.",
            role="assistant",
            model="mock-agent-v1",
            usage=TokenUsage(prompt_tokens=40, completion_tokens=10, total_tokens=50),
            finish_reason="stop",
        ),
    ]

    ScopedMockProvider.set_responses(responses)
    AIClient.register_provider("mock_tools_provider", ScopedMockProvider)

    client = (
        AIClient(config=mock_config, default_provider="mock_tools_provider")
        .user("Run tools in parallel")
        .tools([TimingToolA(), TimingToolB(), FailingToolC()])
        .auto_execute_tools(enable=True)
    )

    t0 = time.perf_counter()
    resp = await client.send()
    total_duration = time.perf_counter() - t0

    assert resp.content == "Parallel execution finished."

    # 1. Proving true concurrent execution:
    # If tools ran sequentially, total duration would exceed 0.08 + 0.08 = 0.16s.
    # Concurrent execution runs tool_a and tool_b in parallel (~0.08s).
    assert total_duration < 0.15
    assert timestamps["tool_a"]["start"] < timestamps["tool_b"]["end"]
    assert timestamps["tool_b"]["start"] < timestamps["tool_a"]["end"]

    # 2. Proving error isolation and order preservation:
    loop = client.last_tool_loop
    assert loop is not None
    assert len(loop.executed_results) == 3
    assert loop.executed_results[0].name == "tool_a"
    assert loop.executed_results[0].result == {"status": "ok_a"}
    assert loop.executed_results[0].is_error is False

    assert loop.executed_results[1].name == "tool_b"
    assert loop.executed_results[1].result == {"status": "ok_b"}
    assert loop.executed_results[1].is_error is False

    assert loop.executed_results[2].name == "tool_c"
    assert "Tool C simulated failure" in str(loop.executed_results[2].result)
    assert loop.executed_results[2].is_error is True


# ==============================================================================
# 7. Dual Guard Semantics: max_iterations vs max_tool_calls
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_loop_dual_guards(mock_config: Config) -> None:
    """Verify max_iterations and max_tool_calls operate as independent guards."""

    # 1. max_tool_calls guard triggers when tool count limit is reached within turns
    responses_many_calls = [
        LLMResponse(
            content="",
            role="assistant",
            model="mock-agent-v1",
            tool_calls=[
                ToolCall(id="tc_1", name="calculator", arguments={"expression": "1+1"}),
                ToolCall(id="tc_2", name="calculator", arguments={"expression": "2+2"}),
                ToolCall(id="tc_3", name="calculator", arguments={"expression": "3+3"}),
            ],
        ),
        LLMResponse(
            content="",
            role="assistant",
            model="mock-agent-v1",
            tool_calls=[
                ToolCall(id="tc_4", name="calculator", arguments={"expression": "4+4"}),
            ],
        ),
    ]

    ScopedMockProvider.set_responses(responses_many_calls)
    AIClient.register_provider("mock_tools_provider", ScopedMockProvider)

    client = (
        AIClient(config=mock_config, default_provider="mock_tools_provider")
        .user("Run many calls")
        .tools([CalculatorTool()])
        .auto_execute_tools(enable=True, max_iterations=10, max_tool_calls=3)
    )

    with pytest.raises(AgentLoopError) as exc_info:
        await client.send()

    assert "exceeded maximum tool calls guard (3)" in str(exc_info.value)

    # 2. Rejection of zero or negative limits
    client_invalid = AIClient(config=mock_config)
    with pytest.raises(ValueError, match="max_iterations must be a positive integer"):
        client_invalid.auto_execute_tools(max_iterations=0)

    with pytest.raises(ValueError, match="max_tool_calls must be a positive integer"):
        client_invalid.auto_execute_tools(max_tool_calls=-1)


# ==============================================================================
# 8. Tool Execution Policy & Permissions
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_loop_policy_and_permissions(mock_config: Config) -> None:
    """Verify allowed_tools enforces authorization and blocks unauthorized tools."""
    responses = [
        LLMResponse(
            content="",
            role="assistant",
            model="mock-agent-v1",
            tool_calls=[
                ToolCall(id="tc_calc", name="calculator", arguments={"expression": "10 * 10"}),
                ToolCall(id="tc_search", name="web_search", arguments={"query": "forbidden search"}),
            ],
        ),
        LLMResponse(
            content="Summary with permitted results only.",
            role="assistant",
            model="mock-agent-v1",
            finish_reason="stop",
        ),
    ]

    ScopedMockProvider.set_responses(responses)
    AIClient.register_provider("mock_tools_provider", ScopedMockProvider)

    # Policy allows only 'calculator', explicitly denying 'web_search'
    client = (
        AIClient(config=mock_config, default_provider="mock_tools_provider")
        .user("Calculate and search")
        .tools([CalculatorTool(), WebSearchTool()])
        .auto_execute_tools(enable=True, allowed_tools=["calculator"])
    )

    resp = await client.send()
    assert resp.content == "Summary with permitted results only."

    loop = client.last_tool_loop
    assert loop is not None
    assert len(loop.executed_results) == 2

    # Permitted tool executed
    assert loop.executed_results[0].name == "calculator"
    assert loop.executed_results[0].result["result"] == 100
    assert loop.executed_results[0].is_error is False

    # Denied tool was safely blocked
    assert loop.executed_results[1].name == "web_search"
    assert "not permitted by current execution policy" in str(loop.executed_results[1].result)
    assert loop.executed_results[1].is_error is True


# ==============================================================================
# 9. Client State Isolation & Concurrency
# ==============================================================================


@pytest.mark.asyncio
async def test_client_state_isolation_and_concurrency(mock_config: Config) -> None:
    """Verify that independent AIClient instances do not leak state or tool registrations."""
    client_a = AIClient(config=mock_config).tools([CalculatorTool()])
    client_b = AIClient(config=mock_config).tools([DateTimeTool(), WebSearchTool()])

    assert [t.name for t in client_a.registered_tools] == ["calculator"]
    assert [t.name for t in client_b.registered_tools] == ["date_time", "web_search"]

    # Test that failed tool loop resets execution guard flag
    ScopedMockProvider.set_responses([
        LLMResponse(
            content="",
            role="assistant",
            tool_calls=[ToolCall(id="err", name="calculator", arguments={"expression": "1+1"})],
        )
    ])
    AIClient.register_provider("mock_tools_provider", ScopedMockProvider)

    client_failing = (
        AIClient(config=mock_config, default_provider="mock_tools_provider")
        .user("Will fail on guard")
        .tools([CalculatorTool()])
        .auto_execute_tools(enable=True, max_iterations=1)
    )

    with pytest.raises(AgentLoopError):
        await client_failing.send()

    # Guard must be reset to False, allowing subsequent calls without hanging
    assert client_failing._is_executing_tool_loop is False


# ==============================================================================
# 10. Autonomous Tool Loop 3-Step Chain Query (Core Requirement)
# ==============================================================================


@pytest.mark.asyncio
async def test_autonomous_tool_loop_three_step_chain(mock_config: Config) -> None:
    """
    Test a complex multi-step autonomous tool execution loop requiring 3 distinct tool calls:
    1. web_search("Tokyo population")
    2. calculator("14000000 * 0.15")
    3. date_time(timezone="Asia/Tokyo")
    Followed by the final text response synthesizing all results.
    """
    responses = [
        LLMResponse(
            content="",
            role="assistant",
            model="mock-agent-v1",
            tool_calls=[
                ToolCall(
                    id="call_ws_1",
                    name="web_search",
                    arguments={"query": "Tokyo population", "max_results": 1},
                )
            ],
            usage=TokenUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
        ),
        LLMResponse(
            content="I found that Tokyo's population is approximately 14,000,000. Now calculating 15%.",
            role="assistant",
            model="mock-agent-v1",
            tool_calls=[
                ToolCall(
                    id="call_calc_2",
                    name="calculator",
                    arguments={"expression": "14000000 * 0.15"},
                )
            ],
            usage=TokenUsage(prompt_tokens=100, completion_tokens=30, total_tokens=130),
        ),
        LLMResponse(
            content="15% of Tokyo's population is 2,100,000. Next checking the current local time in Tokyo.",
            role="assistant",
            model="mock-agent-v1",
            tool_calls=[
                ToolCall(
                    id="call_dt_3",
                    name="date_time",
                    arguments={"timezone": "Asia/Tokyo"},
                )
            ],
            usage=TokenUsage(prompt_tokens=160, completion_tokens=30, total_tokens=190),
        ),
        LLMResponse(
            content=(
                "The population of Tokyo is approximately 14,000,000. "
                "15% of that population corresponds to 2,100,000 citizens. "
                "The current local time in Asia/Tokyo is recorded in the report."
            ),
            role="assistant",
            model="mock-agent-v1",
            usage=TokenUsage(prompt_tokens=220, completion_tokens=45, total_tokens=265),
            finish_reason="stop",
        ),
    ]

    ScopedMockProvider.set_responses(responses)
    AIClient.register_provider("mock_tools_provider", ScopedMockProvider)

    client = (
        AIClient(config=mock_config, default_provider="mock_tools_provider")
        .user(
            "Find the population of Tokyo, calculate what 15% of that population is, "
            "and give me the current time in Asia/Tokyo for the report."
        )
        .tools([WebSearchTool(), CalculatorTool(), DateTimeTool()])
        .auto_execute_tools(enable=True, max_iterations=10)
    )

    final_response = await client.send()

    assert "14,000,000" in final_response.content
    assert "2,100,000" in final_response.content
    assert "Asia/Tokyo" in final_response.content

    tool_loop = client.last_tool_loop
    assert tool_loop is not None
    assert len(tool_loop.executed_calls) == 3
    assert [c.name for c in tool_loop.executed_calls] == ["web_search", "calculator", "date_time"]
    assert len(tool_loop.executed_results) == 3
    assert not any(r.is_error for r in tool_loop.executed_results)


# ==============================================================================
# 11. Attached Conversation Integration
# ==============================================================================


@pytest.mark.asyncio
async def test_client_conversation_integration(mock_config: Config) -> None:
    conv = Conversation()
    responses = [
        LLMResponse(
            content="",
            role="assistant",
            model="mock-agent-v1",
            tool_calls=[ToolCall(id="tc_c", name="calculator", arguments={"expression": "2 + 2"})],
        ),
        LLMResponse(
            content="Final answer: 4",
            role="assistant",
            model="mock-agent-v1",
        ),
    ]

    ScopedMockProvider.set_responses(responses)
    AIClient.register_provider("mock_tools_provider", ScopedMockProvider)

    client = (
        AIClient(config=mock_config, default_provider="mock_tools_provider")
        .conversation(conv)
        .user("What is 2+2?")
        .tools([CalculatorTool()])
        .auto_execute_tools(enable=True)
    )

    await client.send()

    conv_messages = conv.messages
    assert len(conv_messages) >= 2
    assert conv_messages[0].content == "What is 2+2?"
    assert conv_messages[-1].content == "Final answer: 4"
