from __future__ import annotations

import asyncio
import dataclasses
import datetime
import enum
import json
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from swiftpy.ai.exceptions import AgentLoopError, AIBudgetExceededError
from swiftpy.ai.pricing import calculate_token_cost
from swiftpy.ai.providers.base import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolResult,
    redact_sensitive_text,
)
from swiftpy.ai.tools.base_tool import BaseTool, validate_tool_arguments

if TYPE_CHECKING:
    from swiftpy.ai.client import AIClient

_INVALID_ARGS_MARKER = "__invalid_tool_arguments__"


def _to_json_safe(value: Any, _seen: set[int] | None = None) -> Any:
    """Recursively convert Python values to JSON-serializable primitives."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, enum.Enum):
        return _to_json_safe(value.value, _seen)
    if isinstance(value, int):
        # Guard Python's 4300-digit integer stringification limit
        if value != 0 and value.bit_length() > 14000:
            return str(value)
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, complex):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    seen = _seen if _seen is not None else set()
    obj_id = id(value)
    if isinstance(value, (dict, list, tuple, set, frozenset)) or dataclasses.is_dataclass(value):
        if obj_id in seen:
            return "<circular>"
        seen.add(obj_id)
        try:
            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                return {
                    str(k): _to_json_safe(v, seen)
                    for k, v in dataclasses.asdict(value).items()
                }
            if isinstance(value, dict):
                return {str(k): _to_json_safe(v, seen) for k, v in value.items()}
            if isinstance(value, (list, tuple, set, frozenset)):
                return [_to_json_safe(v, seen) for v in value]
        finally:
            seen.discard(obj_id)

    return str(value)


def serialize_tool_result(result: Any) -> str:
    """Serialize any tool return value cleanly into a string payload for LLM/memory persistence."""
    if isinstance(result, str):
        return result
    safe_val = _to_json_safe(result)
    try:
        return json.dumps(safe_val, allow_nan=False)
    except (TypeError, ValueError):
        return str(safe_val)


class ToolLoop:
    """
    Autonomous execution loop coordinating iterative LLM tool calling.

    Dispatches requests with registered tools, intercepts model-generated tool calls,
    executes matching Python tool handlers concurrently with bounded concurrency,
    appends execution results back into the conversation, and repeats until the
    model emits a final text response (or configured limits are reached).
    """

    def __init__(
        self,
        client: AIClient,
        tools: Sequence[BaseTool],
        max_iterations: int = 10,
        max_tool_calls: int | None = None,
        allowed_tools: Sequence[str] | None = None,
        tool_timeout: float | None = None,
        max_concurrency: int = 10,
        max_total_tokens: int | None = None,
        max_cost: float | None = None,
    ) -> None:
        if isinstance(max_iterations, bool) or max_iterations <= 0:
            raise ValueError(f"max_iterations must be a positive integer, got {max_iterations}")
        if max_tool_calls is not None and (isinstance(max_tool_calls, bool) or max_tool_calls <= 0):
            raise ValueError(f"max_tool_calls must be a positive integer, got {max_tool_calls}")
        if tool_timeout is not None and (isinstance(tool_timeout, bool) or tool_timeout <= 0):
            raise ValueError(f"tool_timeout must be a positive float, got {tool_timeout}")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency <= 0:
            raise ValueError(f"max_concurrency must be a positive integer, got {max_concurrency}")
        if max_total_tokens is not None and (
            isinstance(max_total_tokens, bool) or not isinstance(max_total_tokens, int) or max_total_tokens <= 0
        ):
            raise ValueError(f"max_total_tokens must be a positive integer, got {max_total_tokens}")
        if max_cost is not None and (
            isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)) or float(max_cost) <= 0
        ):
            raise ValueError(f"max_cost must be a positive float, got {max_cost}")

        self._client = client
        self._tools = list(tools)
        self._max_iterations = max_iterations
        self._max_tool_calls = max_tool_calls
        self._allowed_tools: set[str] | None = set(allowed_tools) if allowed_tools is not None else None
        self._tool_timeout = tool_timeout
        self._max_concurrency = max_concurrency
        self._max_total_tokens = max_total_tokens
        self._max_cost = float(max_cost) if max_cost is not None else None

        self._executed_calls: list[ToolCall] = []
        self._executed_results: list[ToolResult] = []
        self._executed_turns: list[tuple[str, list[ToolCall], list[ToolResult]]] = []
        self._iterations: int = 0
        self._total_prompt_tokens: int = 0
        self._total_completion_tokens: int = 0
        self._total_tokens: int = 0
        self._total_cost: float = 0.0

    @property
    def client(self) -> AIClient:
        return self._client

    @property
    def tools(self) -> list[BaseTool]:
        return list(self._tools)

    @property
    def max_iterations(self) -> int:
        return self._max_iterations

    @property
    def max_tool_calls(self) -> int | None:
        return self._max_tool_calls

    @property
    def allowed_tools(self) -> set[str] | None:
        return set(self._allowed_tools) if self._allowed_tools is not None else None

    @property
    def tool_timeout(self) -> float | None:
        return self._tool_timeout

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def executed_calls(self) -> list[ToolCall]:
        return list(self._executed_calls)

    @property
    def executed_results(self) -> list[ToolResult]:
        return list(self._executed_results)

    @property
    def executed_turns(self) -> list[tuple[str, list[ToolCall], list[ToolResult]]]:
        return list(self._executed_turns)

    @property
    def iterations(self) -> int:
        return self._iterations

    @property
    def total_usage(self) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self._total_prompt_tokens,
            completion_tokens=self._total_completion_tokens,
            total_tokens=self._total_tokens,
        )

    @property
    def total_cost(self) -> float:
        return self._total_cost

    def _detect_tool_calls(self, response: LLMResponse) -> list[ToolCall]:
        """Detect and extract tool calls from the response object or raw provider payload."""
        if response.tool_calls:
            return response.tool_calls

        raw = response.raw
        if not isinstance(raw, dict):
            return []

        # 1. OpenAI-style choices[0].message.tool_calls
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            raw_tc = msg.get("tool_calls") if isinstance(msg, dict) else None
            if isinstance(raw_tc, list):
                extracted: list[ToolCall] = []
                for item in raw_tc:
                    if isinstance(item, dict):
                        tc_id = str(item.get("id") or "")
                        fn = item.get("function") or {}
                        fn_name = str(fn.get("name") or "") if isinstance(fn, dict) else ""
                        fn_args = fn.get("arguments") if isinstance(fn, dict) else {}
                        if isinstance(fn_args, str):
                            try:
                                parsed_args = json.loads(fn_args)
                                if not isinstance(parsed_args, dict):
                                    parsed_args = {_INVALID_ARGS_MARKER: True, "_raw": fn_args}
                            except (json.JSONDecodeError, TypeError, ValueError):
                                parsed_args = {_INVALID_ARGS_MARKER: True, "_raw": fn_args}
                        elif isinstance(fn_args, dict):
                            parsed_args = fn_args
                        elif fn_args is None:
                            parsed_args = {}
                        else:
                            parsed_args = {_INVALID_ARGS_MARKER: True, "_raw": fn_args}
                        extracted.append(ToolCall(id=tc_id, name=fn_name, arguments=parsed_args))
                return extracted

        # 2. Anthropic-style content blocks with type='tool_use'
        content_blocks = raw.get("content")
        if isinstance(content_blocks, list):
            extracted = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tc_id = str(block.get("id") or "")
                    name = str(block.get("name") or "")
                    input_args = block.get("input")
                    if isinstance(input_args, dict):
                        coerced_args = dict(input_args)
                    elif isinstance(input_args, str):
                        try:
                            parsed = json.loads(input_args)
                            coerced_args = (
                                dict(parsed)
                                if isinstance(parsed, dict)
                                else {_INVALID_ARGS_MARKER: True, "_raw": input_args}
                            )
                        except (json.JSONDecodeError, ValueError):
                            coerced_args = {_INVALID_ARGS_MARKER: True, "_raw": input_args}
                    elif input_args is None:
                        coerced_args = {}
                    else:
                        coerced_args = {_INVALID_ARGS_MARKER: True, "_raw": input_args}
                    extracted.append(ToolCall(id=tc_id, name=name, arguments=coerced_args))
            if extracted:
                return extracted

        return []

    async def _execute_single_tool(
        self,
        tc: ToolCall,
        tool_map: dict[str, BaseTool],
        semaphore: asyncio.Semaphore | None = None,
    ) -> ToolResult:
        """Execute a single tool call with policy, schema validation, redaction, and concurrency bounds."""
        if self._allowed_tools is not None and tc.name not in self._allowed_tools:
            err_msg = f"Error: Tool '{tc.name}' is not permitted by current execution policy."
            return ToolResult(tool_call_id=tc.id, name=tc.name, result={"error": err_msg}, is_error=True)

        tool = tool_map.get(tc.name)
        if tool is None:
            err_msg = f"Error: Tool '{tc.name}' not found. Available tools: {sorted(tool_map.keys())}"
            return ToolResult(tool_call_id=tc.id, name=tc.name, result={"error": err_msg}, is_error=True)

        if not isinstance(tc.arguments, dict) or _INVALID_ARGS_MARKER in tc.arguments:
            err_msg = f"Error: Tool '{tc.name}' received invalid non-object arguments."
            return ToolResult(tool_call_id=tc.id, name=tc.name, result={"error": err_msg}, is_error=True)

        try:
            validate_tool_arguments(tool.parameters, tc.arguments, tool.name)
        except (TypeError, ValueError) as exc:
            err_msg = f"Invalid arguments for tool '{tc.name}': {redact_sensitive_text(str(exc))}"
            return ToolResult(tool_call_id=tc.id, name=tc.name, result={"error": err_msg}, is_error=True)

        async def _invoke() -> Any:
            coro = tool.handle(**tc.arguments)
            if self._tool_timeout is not None:
                return await asyncio.wait_for(coro, timeout=self._tool_timeout)
            return await coro

        try:
            if semaphore is not None:
                async with semaphore:
                    raw_result = await _invoke()
            else:
                raw_result = await _invoke()
            return ToolResult(tool_call_id=tc.id, name=tc.name, result=raw_result, is_error=False)
        except TimeoutError:
            err_msg = f"Error: Tool '{tc.name}' execution timed out after {self._tool_timeout}s"
            return ToolResult(tool_call_id=tc.id, name=tc.name, result={"error": err_msg}, is_error=True)
        except Exception as exc:  # noqa: BLE001
            err_msg = f"Error executing tool '{tc.name}': {redact_sensitive_text(str(exc))}"
            return ToolResult(tool_call_id=tc.id, name=tc.name, result={"error": err_msg}, is_error=True)

    def _record_and_check_budget(self, response: LLMResponse) -> None:
        usage = response.usage
        turn_total = usage.total_tokens or (usage.prompt_tokens + usage.completion_tokens)
        self._total_prompt_tokens += usage.prompt_tokens
        self._total_completion_tokens += usage.completion_tokens
        self._total_tokens += turn_total
        self._total_cost += calculate_token_cost(
            response.model or "",
            usage.prompt_tokens,
            usage.completion_tokens,
        )

        if self._max_total_tokens is not None and self._total_tokens > self._max_total_tokens:
            raise AIBudgetExceededError(
                f"Autonomous loop token budget exceeded: {self._total_tokens} > {self._max_total_tokens}"
            )
        if self._max_cost is not None and self._total_cost > self._max_cost:
            raise AIBudgetExceededError(
                f"Autonomous loop cost budget exceeded: ${self._total_cost:.6f} > ${self._max_cost:.6f}"
            )

    async def run(self, prompt: str | None = None) -> LLMResponse:
        """
        Execute the autonomous tool loop.

        Returns the final LLMResponse when the model emits a text answer.
        Raises AgentLoopError or AIBudgetExceededError if guards are exceeded.
        """
        if prompt:
            self._client.user(prompt)

        self._client.tools(self._tools)
        tool_map = {t.name: t for t in self._tools}
        semaphore = asyncio.Semaphore(self._max_concurrency)

        was_executing = getattr(self._client, "_is_executing_tool_loop", False)
        prev_allowed = getattr(self._client, "_allowed_tools", None)
        prev_suppress = getattr(self._client, "_suppress_tools_in_request", False)

        self._client._is_executing_tool_loop = True
        if self._allowed_tools is not None:
            self._client._allowed_tools = sorted(self._allowed_tools)

        try:
            iteration = 0
            while iteration < self._max_iterations:
                self._client._suppress_tools_in_request = (
                    self._max_tool_calls is not None
                    and len(self._executed_calls) >= self._max_tool_calls
                )
                response = await self._client.send()
                self._record_and_check_budget(response)

                tool_calls = self._detect_tool_calls(response)
                if not tool_calls:
                    self._iterations = iteration + 1
                    return response

                if self._max_tool_calls is not None and (
                    len(self._executed_calls) >= self._max_tool_calls
                    or len(self._executed_calls) + len(tool_calls) > self._max_tool_calls
                ):
                    raise AgentLoopError(
                        f"Autonomous agent tool loop exceeded maximum tool calls guard ({self._max_tool_calls}).",
                        iterations=iteration,
                    )

                self._client._messages.append(
                    Message(
                        role="assistant",
                        content=response.content,
                        tool_calls=tool_calls,
                    )
                )

                tasks = [self._execute_single_tool(tc, tool_map, semaphore) for tc in tool_calls]
                results = list(await asyncio.gather(*tasks))

                self._executed_turns.append((response.content, list(tool_calls), results))
                for tc, res in zip(tool_calls, results):
                    self._executed_calls.append(tc)
                    self._executed_results.append(res)
                    self._client._messages.append(
                        Message(
                            role="tool",
                            content=serialize_tool_result(res.result),
                            name=tc.name,
                            tool_call_id=tc.id,
                        )
                    )

                iteration += 1
                self._iterations = iteration

            raise AgentLoopError(
                f"Autonomous agent tool loop exceeded maximum iteration guard ({self._max_iterations}).",
                iterations=iteration,
            )
        finally:
            self._client._is_executing_tool_loop = was_executing
            self._client._allowed_tools = prev_allowed
            self._client._suppress_tools_in_request = prev_suppress
