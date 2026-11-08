from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from swiftpy.ai.exceptions import AgentLoopError
from swiftpy.ai.providers.base import LLMResponse, Message, ToolCall, ToolResult
from swiftpy.ai.tools.base_tool import BaseTool

if TYPE_CHECKING:
    from swiftpy.ai.client import AIClient


class ToolLoop:
    """
    Autonomous execution loop coordinating iterative LLM tool calling.

    Dispatches requests with registered tools, intercepts model-generated tool calls,
    executes matching Python tool handlers concurrently, appends execution
    results back into the conversation, and repeats until the model emits a final
    text response (or configured limits are reached).
    """

    def __init__(
        self,
        client: AIClient,
        tools: Sequence[BaseTool],
        max_iterations: int = 10,
        max_tool_calls: int | None = None,
        allowed_tools: Sequence[str] | None = None,
        tool_timeout: float | None = None,
    ) -> None:
        if max_iterations <= 0:
            raise ValueError(f"max_iterations must be a positive integer, got {max_iterations}")
        if max_tool_calls is not None and max_tool_calls <= 0:
            raise ValueError(f"max_tool_calls must be a positive integer, got {max_tool_calls}")
        if tool_timeout is not None and tool_timeout <= 0:
            raise ValueError(f"tool_timeout must be a positive float, got {tool_timeout}")

        self._client = client
        self._tools = list(tools)
        self._max_iterations = max_iterations
        self._max_tool_calls = max_tool_calls
        self._allowed_tools: set[str] | None = set(allowed_tools) if allowed_tools is not None else None
        self._tool_timeout = tool_timeout
        self._executed_calls: list[ToolCall] = []
        self._executed_results: list[ToolResult] = []

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
    def executed_calls(self) -> list[ToolCall]:
        return list(self._executed_calls)

    @property
    def executed_results(self) -> list[ToolResult]:
        return list(self._executed_results)

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
                            except (json.JSONDecodeError, TypeError):
                                parsed_args = {"raw": fn_args}
                        elif isinstance(fn_args, dict):
                            parsed_args = fn_args
                        else:
                            parsed_args = {}
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
                    input_args = block.get("input") or {}
                    extracted.append(ToolCall(id=tc_id, name=name, arguments=dict(input_args)))
            if extracted:
                return extracted

        return []

    async def _execute_single_tool(
        self,
        tc: ToolCall,
        tool_map: dict[str, BaseTool],
    ) -> ToolResult:
        """Execute a single tool call with policy checks and timeout isolation."""
        # 1. Permission check
        if self._allowed_tools is not None and tc.name not in self._allowed_tools:
            err_msg = f"Error: Tool '{tc.name}' is not permitted by current execution policy."
            return ToolResult(tool_call_id=tc.id, name=tc.name, result={"error": err_msg}, is_error=True)

        tool = tool_map.get(tc.name)
        if tool is None:
            err_msg = f"Error: Tool '{tc.name}' not found. Available tools: {sorted(tool_map.keys())}"
            return ToolResult(tool_call_id=tc.id, name=tc.name, result={"error": err_msg}, is_error=True)

        try:
            coro = tool.handle(**tc.arguments)
            if self._tool_timeout is not None:
                raw_result = await asyncio.wait_for(coro, timeout=self._tool_timeout)
            else:
                raw_result = await coro
            return ToolResult(tool_call_id=tc.id, name=tc.name, result=raw_result, is_error=False)
        except TimeoutError:
            err_msg = f"Error: Tool '{tc.name}' execution timed out after {self._tool_timeout}s"
            return ToolResult(tool_call_id=tc.id, name=tc.name, result={"error": err_msg}, is_error=True)
        except Exception as exc:  # noqa: BLE001
            err_msg = f"Error executing tool '{tc.name}': {exc!s}"
            return ToolResult(tool_call_id=tc.id, name=tc.name, result={"error": err_msg}, is_error=True)

    async def run(self, prompt: str | None = None) -> LLMResponse:
        """
        Execute the autonomous tool loop.

        Returns the final LLMResponse when the model emits a text answer.
        Raises AgentLoopError if max_iterations or max_tool_calls is exceeded.
        """
        if prompt:
            self._client.user(prompt)

        # Wire tools into client
        self._client.tools(self._tools)
        tool_map = {t.name: t for t in self._tools}

        was_executing = getattr(self._client, "_is_executing_tool_loop", False)
        self._client._is_executing_tool_loop = True
        try:
            iteration = 0
            while iteration < self._max_iterations:
                # 1. Send conversation to LLM
                response = await self._client.send()

                # 2. Detect requested tool calls
                tool_calls = self._detect_tool_calls(response)

                # 3. If no tool calls were requested, return the final text response
                if not tool_calls:
                    return response

                # 4. Check max_tool_calls guard before executing
                if self._max_tool_calls is not None and (
                    len(self._executed_calls) >= self._max_tool_calls
                    or len(self._executed_calls) + len(tool_calls) > self._max_tool_calls
                ):
                    raise AgentLoopError(
                        f"Autonomous agent tool loop exceeded maximum tool calls guard ({self._max_tool_calls}).",
                        iterations=iteration,
                    )

                # 5. Append assistant turn with requested tool calls to message history
                self._client._messages.append(
                    Message(
                        role="assistant",
                        content=response.content,
                        tool_calls=tool_calls,
                    )
                )

                # 6. Execute each tool call concurrently with asyncio.gather
                tasks = [self._execute_single_tool(tc, tool_map) for tc in tool_calls]
                results = await asyncio.gather(*tasks)

                # 7. Record executed calls and results in exact order
                for tc, res in zip(tool_calls, results):
                    self._executed_calls.append(tc)
                    self._executed_results.append(res)

                    content_str = (
                        json.dumps(res.result)
                        if not isinstance(res.result, str)
                        else res.result
                    )

                    # Feed execution result back as role="tool"
                    self._client._messages.append(
                        Message(
                            role="tool",
                            content=content_str,
                            name=tc.name,
                            tool_call_id=tc.id,
                        )
                    )

                iteration += 1

            raise AgentLoopError(
                f"Autonomous agent tool loop exceeded maximum iteration guard ({self._max_iterations}).",
                iterations=iteration,
            )
        finally:
            self._client._is_executing_tool_loop = was_executing
