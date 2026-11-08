from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Coroutine, Generator, Sequence
from typing import Any, ClassVar

from swiftpy.ai.agents.memory import (
    AgentMemory,
    BaseMemoryBackend,
    InMemoryMemoryBackend,
)
from swiftpy.ai.client import AIClient
from swiftpy.ai.exceptions import AgentLoopError
from swiftpy.ai.providers.base import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from swiftpy.ai.tools.base_tool import BaseTool
from swiftpy.ai.tools.tool_loop import ToolLoop
from swiftpy.core.config import Config

logger = logging.getLogger("swiftpy.ai.agents.base_agent")


class AgentResponse:
    """
    Structured outcome of an autonomous agent execution turn.
    Exposes final text answer, executed tool calls, tool results, and telemetry.
    """

    def __init__(
        self,
        content: str,
        role: str = "assistant",
        tool_calls: list[ToolCall] | None = None,
        executed_calls: list[ToolCall] | None = None,
        executed_results: list[ToolResult] | None = None,
        session_id: str = "default",
        iterations: int = 1,
        usage: TokenUsage | None = None,
        model: str = "",
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.content = content
        self.role = role
        self.tool_calls = list(tool_calls or [])
        self.executed_calls = list(executed_calls or [])
        self.executed_results = list(executed_results or [])
        self.session_id = session_id
        self.iterations = iterations
        self.usage = usage or TokenUsage()
        self.model = model
        self.raw = raw or {}

    def __str__(self) -> str:
        return self.content

    def __repr__(self) -> str:
        return (
            f"AgentResponse(session_id={self.session_id!r}, "
            f"iterations={self.iterations}, tool_calls={len(self.executed_calls)}, "
            f"content={self.content[:40]!r})"
        )


class AgentTracker:
    """
    Application-level tracker for actively executing background agent tasks.
    Enables concurrency monitoring, task registry, and graceful cancellation on shutdown.
    """

    _instance: ClassVar[AgentTracker | None] = None

    def __init__(self) -> None:
        self._tasks: dict[str, AgentTask] = {}

    @classmethod
    def get_instance(cls) -> AgentTracker:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (primarily for tests)."""
        cls._instance = None

    def register(self, task: AgentTask) -> None:
        self._tasks[task.task_id] = task

    def unregister(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)

    @property
    def active_tasks(self) -> list[AgentTask]:
        return [t for t in self._tasks.values() if t.is_running]

    @property
    def active_count(self) -> int:
        return len(self.active_tasks)

    async def cancel_all(self, timeout: float = 5.0) -> int:
        """Cancel all registered active agent tasks gracefully."""
        active = self.active_tasks
        if not active:
            return 0
        for t in active:
            t.cancel()
        count = len(active)
        raw_tasks = [t._task for t in active]
        if raw_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*raw_tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except TimeoutError:
                pass
        self._tasks.clear()
        return count


class AgentTask:
    """
    Handle for an agent running as a background asyncio task.
    Safely captures exceptions to adhere to SwiftPY Concurrency rules (The Task Rule).
    Provides awaitable execution, status polling, cancellation, and result retrieval.
    """

    def __init__(
        self,
        coro: Coroutine[Any, Any, AgentResponse],
        session_id: str = "default",
        task_id: str | None = None,
    ) -> None:
        self.task_id = task_id or uuid.uuid4().hex[:12]
        self.session_id = session_id
        self._status = "running"
        self._result: AgentResponse | None = None
        self._exception: BaseException | None = None

        self._task: asyncio.Task[AgentResponse] = asyncio.create_task(coro)
        self._task.add_done_callback(self._on_done)
        AgentTracker.get_instance().register(self)

    def _on_done(self, t: asyncio.Task[AgentResponse]) -> None:
        AgentTracker.get_instance().unregister(self.task_id)
        if t.cancelled():
            self._status = "cancelled"
            return
        exc = t.exception()
        if exc is not None:
            self._status = "failed"
            self._exception = exc
            logger.exception(
                "Background agent task '%s' (session '%s') failed: %s",
                self.task_id,
                self.session_id,
                exc,
                exc_info=exc,
            )
        else:
            self._status = "completed"
            self._result = t.result()

    @property
    def status(self) -> str:
        """Current lifecycle status: 'running', 'completed', 'failed', or 'cancelled'."""
        return self._status

    @property
    def is_done(self) -> bool:
        """Return True if background execution has concluded (success, failure, or cancelled)."""
        return self._task.done()

    @property
    def is_running(self) -> bool:
        """Return True if the background task is actively running."""
        return not self._task.done()

    def result(self) -> AgentResponse:
        """
        Return the AgentResponse if execution succeeded.
        Raises RuntimeError if not done, or re-raises any exception if execution failed.
        """
        if not self._task.done():
            raise RuntimeError(f"Agent task '{self.task_id}' is still running.")
        if self._exception is not None:
            raise self._exception
        if self._result is None:
            raise RuntimeError(f"Agent task '{self.task_id}' produced no result (status: {self._status}).")
        return self._result

    def exception(self) -> BaseException | None:
        """Return the exception raised by the task, or None if completed successfully or still running."""
        return self._exception

    def cancel(self) -> bool:
        """Cancel the background agent task."""
        self._status = "cancelled"
        cancelled = self._task.cancel()
        AgentTracker.get_instance().unregister(self.task_id)
        return cancelled

    async def wait(self, timeout: float | None = None) -> AgentResponse:
        """Wait for the background task to complete and return the AgentResponse."""
        if timeout is not None:
            return await asyncio.wait_for(self._task, timeout=timeout)
        return await self._task

    def __await__(self) -> Generator[Any, None, AgentResponse]:
        """Allow direct awaiting: response = await agent.run_background(...)"""
        return self._task.__await__()


class BaseAgent:
    """
    Autonomous agent coordinating system prompt enforcement, memory management,
    iteration limits, and tool-calling loops. Runs directly or as non-blocking
    background asyncio tasks.
    """

    def __init__(
        self,
        name: str = "Agent",
        *,
        system_prompt: str = "You are a helpful AI assistant.",
        tools: Sequence[BaseTool | type[BaseTool]] | None = None,
        memory: AgentMemory | BaseMemoryBackend | None = None,
        client: AIClient | None = None,
        model: str | None = None,
        provider: str | None = None,
        max_iterations: int = 10,
        max_tool_calls: int | None = None,
        tool_timeout: float | None = None,
        allowed_tools: Sequence[str] | None = None,
        session_id: str | None = None,
        config: Config | None = None,
    ) -> None:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("system_prompt must be a non-empty string.")
        if max_iterations <= 0:
            raise ValueError(f"max_iterations must be a positive integer, got {max_iterations}")
        if max_tool_calls is not None and max_tool_calls <= 0:
            raise ValueError(f"max_tool_calls must be a positive integer, got {max_tool_calls}")
        if tool_timeout is not None and tool_timeout <= 0:
            raise ValueError(f"tool_timeout must be a positive float, got {tool_timeout}")

        self.name = name
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.tool_timeout = tool_timeout
        self.allowed_tools = set(allowed_tools) if allowed_tools is not None else None
        self.default_session_id = session_id or uuid.uuid4().hex[:12]
        self._config = config
        self._model = model
        self._provider = provider

        # Memory configuration
        if isinstance(memory, AgentMemory):
            self._memory = memory
        elif isinstance(memory, BaseMemoryBackend):
            self._memory = AgentMemory(backend=memory, session_id=self.default_session_id)
        else:
            self._memory = AgentMemory(backend=InMemoryMemoryBackend(), session_id=self.default_session_id)

        # Tools configuration (instantiate classes, isolate copies)
        self._tools: list[BaseTool] = []
        if tools:
            for t in tools:
                if isinstance(t, type) and issubclass(t, BaseTool):
                    self._tools.append(t())
                elif isinstance(t, BaseTool):
                    self._tools.append(t)
                else:
                    raise TypeError(f"Expected BaseTool instance or subclass, got {type(t).__name__}")

        # Client template or fresh instance
        self._client_template = client

    @property
    def memory(self) -> AgentMemory:
        """Return the default AgentMemory instance."""
        return self._memory

    @property
    def tools(self) -> list[BaseTool]:
        """Return a defensive copy of registered tools."""
        return list(self._tools)

    def get_session_memory(self, session_id: str) -> AgentMemory:
        """Retrieve memory bound to a specific session ID."""
        if session_id == self._memory.session_id:
            return self._memory
        return self._memory.fork(session_id)

    def _create_isolated_client(self) -> AIClient:
        """
        Create a dedicated, isolated AIClient instance for an execution turn.
        Guarantees that concurrent agent executions never share message buffers or tool telemetry.
        """
        if self._client_template is not None:
            # Clone from existing client configuration
            cfg = getattr(self._client_template, "_config", None)
            prov_name = getattr(self._client_template, "_provider_name", None)
            pool = getattr(self._client_template, "_http_pool", None)
            client = AIClient(config=cfg, http_pool=pool, default_provider=prov_name)
            # Preserve custom registered providers from template
            template_registry = getattr(self._client_template, "_registry", {})
            for p_name, p_instance in template_registry.items():
                client.register_provider(p_name, p_instance)
            if hasattr(self._client_template, "_model_name") and self._client_template._model_name:
                client.model(self._client_template._model_name)
            if hasattr(self._client_template, "_temperature") and self._client_template._temperature is not None:
                client.temperature(self._client_template._temperature)
            return client

        client = AIClient(config=self._config, default_provider=self._provider)
        if self._model:
            client.model(self._model)
        return client

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
    ) -> AgentResponse:
        """
        Execute the autonomous agent loop synchronously (from the caller's coroutine).
        Enforces system prompt, loads prior messages from memory, executes tools,
        persists conversation turns, and returns an AgentResponse.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt must be a non-empty string.")

        sid = session_id or self.default_session_id
        session_mem = self.get_session_memory(sid)

        # 1. Load existing dialogue history from memory backend
        history = await session_mem.get_messages()

        # 2. Build dialogue messages, strictly enforcing the system prompt at the beginning
        ordered_messages: list[Message] = []
        has_system = False
        for msg in history:
            if msg.role == "system":
                # Ensure the current agent's system prompt is enforced
                ordered_messages.append(Message(role="system", content=self.system_prompt))
                has_system = True
            else:
                ordered_messages.append(msg)

        if not has_system:
            ordered_messages.insert(0, Message(role="system", content=self.system_prompt))

        # 3. Append the new user prompt
        user_message = Message(role="user", content=prompt)
        ordered_messages.append(user_message)

        # 4. Create an isolated client and load ordered messages
        client = self._create_isolated_client()
        for msg in ordered_messages:
            client._messages.append(msg)

        # 5. Initialize isolated ToolLoop with configured tools and limits
        loop = ToolLoop(
            client=client,
            tools=self._tools,
            max_iterations=self.max_iterations,
            max_tool_calls=self.max_tool_calls,
            allowed_tools=list(self.allowed_tools) if self.allowed_tools is not None else None,
            tool_timeout=self.tool_timeout,
        )

        try:
            llm_response: LLMResponse = await loop.run()
        except AgentLoopError as exc:
            logger.warning("Agent '%s' tool loop reached limit: %s", self.name, exc)
            raise

        # 6. Capture new dialogue turns generated during this run and persist to memory
        # Messages from client._messages that were added during this run
        new_messages: list[Message] = []
        if not has_system:
            new_messages.append(Message(role="system", content=self.system_prompt))
        new_messages.append(user_message)

        # Any intermediate assistant/tool turns from loop
        for tc, res in zip(loop.executed_calls, loop.executed_results):
            new_messages.append(
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[tc],
                )
            )
            content_str = str(res.result)
            new_messages.append(
                Message(
                    role="tool",
                    content=content_str,
                    name=tc.name,
                    tool_call_id=tc.id,
                )
            )

        # Final assistant text response
        assistant_message = Message(
            role="assistant",
            content=llm_response.content,
            tool_calls=llm_response.tool_calls if llm_response.tool_calls else None,
        )
        new_messages.append(assistant_message)

        # Save to memory backend
        for msg in new_messages:
            await session_mem.add_message(msg)

        # 7. Construct AgentResponse
        iterations_count = len(loop.executed_calls) + 1
        return AgentResponse(
            content=llm_response.content,
            role=llm_response.role,
            tool_calls=llm_response.tool_calls,
            executed_calls=loop.executed_calls,
            executed_results=loop.executed_results,
            session_id=sid,
            iterations=iterations_count,
            usage=llm_response.usage,
            model=llm_response.model,
            raw=llm_response.raw,
        )

    def run_background(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
    ) -> AgentTask:
        """
        Run the agent as a non-blocking background asyncio task.
        Adheres to Concurrency rules by attaching an exception logging callback.
        Returns an AgentTask handle.
        """
        coro = self.run(prompt, session_id=session_id)
        sid = session_id or self.default_session_id
        return AgentTask(coro, session_id=sid)

    def run_in_background(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
    ) -> AgentTask:
        """Alias for run_background()."""
        return self.run_background(prompt, session_id=session_id)


# Class alias
Agent = BaseAgent

__all__ = [
    "Agent",
    "AgentResponse",
    "AgentTask",
    "AgentTracker",
    "BaseAgent",
]
