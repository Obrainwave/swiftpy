from __future__ import annotations

import asyncio
import contextlib
import copy
import time
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Self

from swiftpy.ai._fake import AIFake, FixtureType
from swiftpy.ai.embeddings import EmbeddingsClient
from swiftpy.ai.exceptions import (
    AIConfigurationError,
    AIModuleDisabledError,
)
from swiftpy.ai.middleware import AIMiddlewarePipeline
from swiftpy.ai.observability.observer import (
    ObservationContext,
    compute_prompt_hash,
    get_observability,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.prompts.loader import PromptLoader
from swiftpy.ai.providers.anthropic_provider import AnthropicProvider
from swiftpy.ai.providers.base import (
    INTERNAL_ATTRIBUTION_KEYS,
    LLMRequest,
    LLMResponse,
    Message,
    ProviderInterface,
    TokenUsage,
)
from swiftpy.ai.providers.openai_provider import OpenAIProvider
from swiftpy.ai.tools.base_tool import BaseTool
from swiftpy.ai.tools.tool_loop import ToolLoop
from swiftpy.ai.vectorstore.base import VectorStore
from swiftpy.core.config import Config

if TYPE_CHECKING:
    from swiftpy.ai.conversation import Conversation

_DEFAULT_PROVIDER_MODELS: dict[str, str] = {
    "openai": "gpt-4o",
    "anthropic": "claude-3-5-sonnet-20241022",
}


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Retrieve exceptions from background tasks so asyncio never warns on GC."""
    if not task.cancelled():
        task.exception()


def _estimate_token_count(text: str) -> int:
    """Estimate token count for text, returning 0 for empty content."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _is_real_int(value: Any) -> bool:
    """Return True if value is an int and not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_real_number(value: Any) -> bool:
    """Return True if value is an int/float and not a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


async def _next_stream_chunk(gen_iter: AsyncIterator[str]) -> str:
    """Await the next chunk from an async stream iterator as a coroutine."""
    return await gen_iter.__anext__()


async def _await_or_abort(
    waiter_coro: Coroutine[Any, Any, Any],
    abort_event: asyncio.Event,
) -> tuple[bool, Any]:
    """Await waiter_coro unless abort_event fires first; returns (aborted, result)."""
    wait_task = asyncio.create_task(waiter_coro)
    wait_task.add_done_callback(_consume_task_exception)
    abort_task = asyncio.create_task(abort_event.wait())
    abort_task.add_done_callback(_consume_task_exception)
    try:
        await asyncio.wait(
            {wait_task, abort_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not abort_task.done():
            abort_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await abort_task
    if abort_event.is_set():
        if not wait_task.done():
            wait_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
            await wait_task
        return True, None
    return False, await wait_task


class AIClient:
    """
    Fluent builder client for LLM completions.

    Provides a chained builder interface:
        response = await (
            AIClient()
            .system("You are a helpful coding assistant.")
            .user("Explain Python async/await in two sentences.")
            .temperature(0.7)
            .max_tokens(150)
            .send()
        )
    """

    _registry: ClassVar[dict[str, type[ProviderInterface] | ProviderInterface]] = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
    }
    _global_middlewares: ClassVar[list[Any]] = []

    def __init__(
        self,
        config: Config | None = None,
        http_pool: AIHttpPool | None = None,
        default_provider: str | None = None,
    ) -> None:
        self._has_custom_config: bool = config is not None
        self._prompt_loader: PromptLoader | None = None
        if config is None:
            try:
                self._config = Config.get_instance()
            except RuntimeError:
                self._config = Config()
        else:
            self._config = config

        self._http_pool = http_pool or AIHttpPool.get_instance()
        self._provider_name: str | None = default_provider

        self._messages: list[Message] = []
        self._temperature: float | None = None
        self._max_tokens: int | None = None
        self._top_p: float | None = None
        self._stop: list[str] | None = None
        self._model: str | None = None
        self._extra: dict[str, Any] = {}
        self._metadata: dict[str, Any] = {}
        self._attached_conversation: Conversation | None = None
        self._pending_conversation_messages: list[Message] = []
        self._tools: list[BaseTool] = []
        self._auto_execute_tools: bool = False
        self._max_iterations: int = 10
        self._max_tool_calls: int | None = None
        self._allowed_tools: Sequence[str] | None = None
        self._tool_timeout: float | None = None
        self._max_total_tokens: int | None = None
        self._max_cost: float | None = None
        self._suppress_tools_in_request: bool = False
        self._is_executing_tool_loop: bool = False
        self._last_tool_loop: ToolLoop | None = None
        self._middlewares: list[Any] = []

    def clone(
        self,
        *,
        include_messages: bool = False,
        include_conversation: bool = True,
    ) -> AIClient:
        """
        Return a shallow-isolated copy of this client preserving provider,
        options, middlewares, and tools without mutating ``AIClient._registry``.
        """
        cloned = AIClient(
            config=self._config if self._has_custom_config else None,
            http_pool=self._http_pool,
            default_provider=self._provider_name,
        )
        cloned._config = self._config
        cloned._has_custom_config = self._has_custom_config
        cloned._prompt_loader = self._prompt_loader
        cloned._model = self._model
        cloned._temperature = self._temperature
        cloned._max_tokens = self._max_tokens
        cloned._top_p = self._top_p
        cloned._stop = list(self._stop) if self._stop is not None else None
        cloned._extra = copy.deepcopy(self._extra)
        cloned._metadata = copy.deepcopy(self._metadata)
        cloned._tools = list(self._tools)
        cloned._auto_execute_tools = self._auto_execute_tools
        cloned._max_iterations = self._max_iterations
        cloned._max_tool_calls = self._max_tool_calls
        cloned._allowed_tools = (
            tuple(self._allowed_tools) if self._allowed_tools is not None else None
        )
        cloned._tool_timeout = self._tool_timeout
        cloned._max_total_tokens = self._max_total_tokens
        cloned._max_cost = self._max_cost
        cloned._middlewares = list(self._middlewares)
        if include_messages:
            cloned._messages = copy.deepcopy(self._messages)
        if include_conversation:
            cloned._attached_conversation = self._attached_conversation
            cloned._pending_conversation_messages = copy.deepcopy(
                self._pending_conversation_messages
            )
        return cloned

    @classmethod
    def register_provider(
        cls,
        name: str,
        provider_cls: type[ProviderInterface] | ProviderInterface,
    ) -> None:
        """Register a custom provider adapter class or instance."""
        cls._registry[name.lower()] = provider_cls

    @classmethod
    def use_global_middleware(cls, middleware: Any) -> None:
        """Register global middleware applied to all AIClient instances."""
        cls._global_middlewares.append(middleware)

    @classmethod
    def clear_global_middlewares(cls) -> None:
        """Clear all registered global middlewares."""
        cls._global_middlewares.clear()

    def use(self, middleware: Any) -> Self:
        """Attach a middleware layer to this client builder."""
        self._middlewares.append(middleware)
        return self

    def middleware(self, middleware: Any) -> Self:
        """Alias for use()."""
        return self.use(middleware)

    def _append_message(self, msg: Message) -> None:
        """Append a message locally and buffer it if a Conversation is attached."""
        self._messages.append(msg)
        if self._attached_conversation is not None:
            self._pending_conversation_messages.append(msg)

    def _commit_conversation_turns(self, assistant_content: str | None = None) -> None:
        """Persist buffered turn messages and optional assistant reply to the attached Conversation."""
        if self._attached_conversation is None:
            return
        while self._pending_conversation_messages:
            self._attached_conversation.add_message(
                self._pending_conversation_messages.pop(0)
            )
        if assistant_content:
            self._attached_conversation.add_assistant(assistant_content)

    def _resolve_prompt_loader(self) -> PromptLoader:
        """
        Return the PromptLoader for this client.

        Clients constructed with an explicit `Config` that configures `ai.prompts`
        (or when no manual `PromptLoader._instance` override is pinned) own an isolated
        `PromptLoader` bound to `self._config`, independent of `PromptLoader.get_instance()`
        singleton order.
        """
        has_pinned_singleton = (
            PromptLoader._instance is not None
            and PromptLoader._instance is not PromptLoader._auto_instance
        )
        if self._has_custom_config and (
            self._config.get("ai.prompts") is not None or not has_pinned_singleton
        ):
            if self._prompt_loader is None:
                self._prompt_loader = PromptLoader(config=self._config)
            return self._prompt_loader
        if has_pinned_singleton and PromptLoader._instance is not None:
            return PromptLoader._instance
        return PromptLoader.get_instance()

    def prompt(self, name: str, /, role: str = "user", **variables: Any) -> Self:
        """
        Load a prompt template from disk, interpolate variables, and append message(s).
        Supports both string templates and module-level `messages` lists.
        """
        rendered = self._resolve_prompt_loader().load(name, **variables)
        if isinstance(rendered, list):
            for msg in rendered:
                self._append_message(
                    Message(
                        role=str(msg["role"]),
                        content=str(msg["content"]),
                        name=msg.get("name"),
                    )
                )
        else:
            self._append_message(Message(role=role, content=rendered))
        return self

    async def aprompt(self, name: str, /, role: str = "user", **variables: Any) -> Self:
        """
        Asynchronously load a prompt template off the event loop, interpolate variables,
        and append message(s). Supports both string templates and module-level `messages` lists.
        """
        rendered = await self._resolve_prompt_loader().aload(name, **variables)
        if isinstance(rendered, list):
            for msg in rendered:
                self._append_message(
                    Message(
                        role=str(msg["role"]),
                        content=str(msg["content"]),
                        name=msg.get("name"),
                    )
                )
        else:
            self._append_message(Message(role=role, content=rendered))
        return self

    def system(self, content: str) -> Self:
        """Add a system message prompt."""
        self._append_message(Message(role="system", content=content))
        return self

    def user(self, content: str) -> Self:
        """Add a user message prompt."""
        self._append_message(Message(role="user", content=content))
        return self

    def assistant(self, content: str) -> Self:
        """Add an assistant message to conversation history."""
        self._messages.append(Message(role="assistant", content=content))
        return self

    def conversation(self, conv: Conversation) -> Self:
        """
        Attach a Conversation container to this client.

        Pre-loads existing messages and automatically records new dialogue turns
        (user prompt upon send/stream, and assistant reply upon successful completion).
        """
        self._attached_conversation = conv
        for msg in conv.messages:
            self._messages.append(msg)
        return self

    def history(self, messages: Sequence[Message | dict[str, str]]) -> Self:
        """
        Append conversation history to the existing messages in order.

        Accepts Message instances or dictionaries containing 'role' and 'content' keys.
        Empty sequences are accepted as a no-op without clearing earlier messages.
        """
        if not isinstance(messages, Sequence):
            raise TypeError(
                f"Expected a sequence for history, got {type(messages).__name__}"
            )

        for msg in messages:
            if isinstance(msg, Message):
                self._messages.append(msg)
            elif isinstance(msg, dict):
                role = str(msg.get("role", "user"))
                content = str(msg.get("content", ""))
                name = msg.get("name")
                self._messages.append(Message(role=role, content=content, name=name))
            else:
                raise TypeError(
                    f"Expected Message or dict in history, got {type(msg).__name__}"
                )
        return self

    def temperature(self, temp: float) -> Self:
        """
        Set sampling temperature.
        Must be between 0.0 and 2.0 inclusive.
        """
        if not _is_real_number(temp) or not (0.0 <= float(temp) <= 2.0):
            raise ValueError(f"Temperature must be between 0.0 and 2.0, got {temp}")
        self._temperature = float(temp)
        return self

    def max_tokens(self, tokens: int) -> Self:
        """
        Set maximum tokens to generate.
        Must be a positive integer greater than 0.
        """
        if not _is_real_int(tokens) or tokens <= 0:
            raise ValueError(
                f"max_tokens must be a positive integer greater than 0, got {tokens}"
            )
        self._max_tokens = tokens
        return self

    def top_p(self, p: float) -> Self:
        """
        Set nucleus sampling top_p parameter.
        Must be between 0.0 and 1.0 inclusive.
        """
        if not _is_real_number(p) or not (0.0 <= float(p) <= 1.0):
            raise ValueError(f"top_p must be between 0.0 and 1.0, got {p}")
        self._top_p = float(p)
        return self

    def stop(self, sequences: list[str] | str) -> Self:
        """
        Set stop sequence(s).
        Must be a non-empty string or a sequence of non-empty strings.
        """
        if isinstance(sequences, str):
            if not sequences:
                raise ValueError("Stop sequence string cannot be empty")
            self._stop = [sequences]
        elif isinstance(sequences, Sequence):
            if not sequences:
                raise ValueError("Stop sequence list cannot be empty")
            parsed: list[str] = []
            for item in sequences:
                if not isinstance(item, str) or not item:
                    raise ValueError(
                        f"All stop sequence items must be non-empty strings, got {item!r}"
                    )
                parsed.append(item)
            self._stop = parsed
        else:
            raise TypeError(
                f"Expected str or sequence of str for stop sequences, got {type(sequences).__name__}"
            )
        return self

    def model(self, model_name: str) -> Self:
        """Explicitly override the model used for this request."""
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("Model name must be a non-empty string")
        self._model = model_name.strip()
        return self

    def provider(self, provider_name: str) -> Self:
        """Explicitly override the provider adapter for this request."""
        if not isinstance(provider_name, str) or not provider_name.strip():
            raise ValueError("Provider name must be a non-empty string")
        self._provider_name = provider_name.strip().lower()
        return self

    def metadata(self, **kwargs: Any) -> Self:
        """Attach internal metadata (attribution/tracing) isolated from upstream payloads."""
        self._metadata.update(kwargs)
        return self

    def extra(self, **kwargs: Any) -> Self:
        """Pass additional provider-specific parameters."""
        self._extra.update(kwargs)
        return self

    def tools(self, tools: Sequence[BaseTool | type[BaseTool]]) -> Self:
        """
        Register tools available for LLM tool calling.

        Accepts instances of BaseTool or BaseTool subclasses (which are instantiated).
        """
        if not isinstance(tools, Sequence):
            raise TypeError(f"Expected a sequence for tools, got {type(tools).__name__}")

        resolved: list[BaseTool] = []
        for t in tools:
            if isinstance(t, type) and issubclass(t, BaseTool):
                resolved.append(t())
            elif isinstance(t, BaseTool):
                resolved.append(t)
            else:
                raise TypeError(
                    f"Expected BaseTool instance or subclass, got {type(t).__name__}"
                )
        self._tools = resolved
        return self

    def auto_execute_tools(
        self,
        enable: bool = True,
        max_iterations: int = 10,
        max_tool_calls: int | None = None,
        allowed_tools: Sequence[str] | None = None,
        tool_timeout: float | None = None,
        max_total_tokens: int | None = None,
        max_cost: float | None = None,
    ) -> Self:
        """
        Configure autonomous tool execution loop.

        When enabled, calling .send() will automatically detect tool use responses from the
        LLM, execute matching tool handlers concurrently, feed tool results back, and
        repeat until a final text response is produced or configured limits are reached.
        """
        if not _is_real_int(max_iterations) or max_iterations <= 0:
            raise ValueError(f"max_iterations must be a positive integer, got {max_iterations}")
        if max_tool_calls is not None and (
            not _is_real_int(max_tool_calls) or max_tool_calls <= 0
        ):
            raise ValueError(f"max_tool_calls must be a positive integer, got {max_tool_calls}")
        if tool_timeout is not None and (
            not _is_real_number(tool_timeout) or float(tool_timeout) <= 0
        ):
            raise ValueError(f"tool_timeout must be a positive float, got {tool_timeout}")
        if max_total_tokens is not None and (
            not _is_real_int(max_total_tokens) or max_total_tokens <= 0
        ):
            raise ValueError(
                f"max_total_tokens must be a positive integer, got {max_total_tokens}"
            )
        if max_cost is not None and (
            not _is_real_number(max_cost) or float(max_cost) <= 0
        ):
            raise ValueError(f"max_cost must be a positive float, got {max_cost}")

        self._auto_execute_tools = bool(enable)
        self._max_iterations = max_iterations
        self._max_tool_calls = max_tool_calls
        self._allowed_tools = allowed_tools
        self._tool_timeout = tool_timeout
        self._max_total_tokens = max_total_tokens
        self._max_cost = float(max_cost) if max_cost is not None else None
        return self

    @property
    def registered_tools(self) -> list[BaseTool]:
        """Return a defensive copy of currently registered tools."""
        return list(self._tools)

    @property
    def last_tool_loop(self) -> ToolLoop | None:
        """Return the ToolLoop instance from the most recent autonomous execution."""
        return self._last_tool_loop

    def _resolve_provider(self) -> ProviderInterface:
        # Check if AI module is enabled in configuration
        is_enabled = self._config.get("ai.enabled", True)
        if not is_enabled:
            raise AIModuleDisabledError(
                "AI module is disabled in configuration (ai.enabled is false)."
            )

        provider_name = (
            self._provider_name
            or str(self._config.get("ai.default", "openai"))
        ).lower()

        provider_cls = self._registry.get(provider_name)
        if not provider_cls:
            supported = ", ".join(sorted(self._registry.keys()))
            raise AIConfigurationError(
                f"Unknown LLM provider '{provider_name}'. Supported providers: {supported}"
            )

        raw_cfg = self._config.get(f"ai.providers.{provider_name}", {})
        provider_cfg: dict[str, Any] = raw_cfg if isinstance(raw_cfg, dict) else {}

        # Validate that an API key is configured for the chosen provider
        api_key = str(provider_cfg.get("api_key", "")).strip()
        if not api_key:
            raise AIConfigurationError(
                f"Missing API key for provider '{provider_name}'. "
                f"Please configure 'ai.providers.{provider_name}.api_key' "
                f"or set the appropriate environment variable."
            )

        if isinstance(provider_cls, type):
            return provider_cls(config=provider_cfg, http_pool=self._http_pool)
        return provider_cls

    def _resolve_target_identity(self) -> tuple[str, str | None]:
        """Resolve provider and model names from builder state/config without API key checks."""
        provider_name = (
            self._provider_name
            or str(self._config.get("ai.default", "openai"))
        ).lower()

        model_name = self._model
        if not model_name:
            raw_cfg = self._config.get(f"ai.providers.{provider_name}", {})
            if isinstance(raw_cfg, dict):
                cfg_model = raw_cfg.get("model") or raw_cfg.get("default_model")
                if isinstance(cfg_model, str) and cfg_model.strip():
                    model_name = cfg_model.strip()

        if not model_name:
            registered = self._registry.get(provider_name)
            if registered is not None and not isinstance(registered, type):
                model_name = registered.get_default_model()
            else:
                model_name = _DEFAULT_PROVIDER_MODELS.get(provider_name)

        return provider_name, model_name

    def _build_llm_request(self, *, include_tools: bool) -> tuple[str, str | None, LLMRequest]:
        """Construct an LLMRequest with attribution metadata isolated from wire extra."""
        provider_name, model_name = self._resolve_target_identity()
        metadata_dict: dict[str, Any] = dict(self._metadata)
        wire_extra: dict[str, Any] = {}
        for key, value in self._extra.items():
            if key in INTERNAL_ATTRIBUTION_KEYS:
                metadata_dict.setdefault(key, value)
            else:
                wire_extra[key] = value
        metadata_dict.setdefault("provider", provider_name)

        request_tools: list[dict[str, Any]] | None = None
        if include_tools and self._tools:
            allowed_set = set(self._allowed_tools) if self._allowed_tools is not None else None
            filtered = [
                t.to_dict()
                for t in self._tools
                if allowed_set is None or t.name in allowed_set
            ]
            if filtered:
                request_tools = filtered

        request = LLMRequest(
            messages=list(self._messages),
            model=model_name,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            top_p=self._top_p,
            stop=self._stop,
            tools=request_tools,
            extra=wire_extra,
            metadata=metadata_dict,
        )
        return provider_name, model_name, request

    def _build_pipeline(self) -> AIMiddlewarePipeline:
        """Assemble global and client-scoped AI middleware in order."""
        return AIMiddlewarePipeline(
            list(self._global_middlewares) + list(self._middlewares)
        )

    async def send(self) -> LLMResponse:
        """
        Dispatch the completion request to the active LLM provider.
        """
        if not self._messages:
            raise ValueError("Cannot send request: at least one message is required.")

        # If auto_execute_tools is enabled and tools are registered, delegate to ToolLoop
        if self._auto_execute_tools and self._tools and not self._is_executing_tool_loop:
            self._is_executing_tool_loop = True
            try:
                loop = ToolLoop(
                    client=self,
                    tools=self._tools,
                    max_iterations=self._max_iterations,
                    max_tool_calls=self._max_tool_calls,
                    allowed_tools=self._allowed_tools,
                    tool_timeout=self._tool_timeout,
                    max_total_tokens=self._max_total_tokens,
                    max_cost=self._max_cost,
                )
                self._last_tool_loop = loop
                final_response = await loop.run()
                self._commit_conversation_turns(final_response.content)
                return final_response
            finally:
                self._is_executing_tool_loop = False

        _provider_name, _model_name, request = self._build_llm_request(
            include_tools=not self._suppress_tools_in_request
        )
        pipeline = self._build_pipeline()

        active_fake = AIFake.get_active()
        if active_fake is not None:
            async def fake_terminal(req: LLMRequest) -> LLMResponse:
                return await active_fake.handle_send(self, req)

            response = await pipeline.execute(request, fake_terminal)
            if not self._is_executing_tool_loop:
                self._commit_conversation_turns(response.content)
            return response

        observer = get_observability()

        async def provider_terminal(req: LLMRequest) -> LLMResponse:
            provider = self._resolve_provider()
            obs_model = req.model or provider.get_default_model()
            async with observer.observe_call(
                model=obs_model,
                provider=provider.name,
                messages=req.messages,
                metadata={**dict(req.extra), **dict(req.metadata)},
            ) as obs_ctx:
                resp = await provider.send(req)
                obs_ctx.set_response(resp)
                return resp

        response = await pipeline.execute(request, provider_terminal)

        # Record user/assistant completion in attached conversation only after success
        if not self._is_executing_tool_loop:
            self._commit_conversation_turns(response.content)

        return response

    async def stream(self) -> AsyncIterator[str]:
        """
        Dispatch the streaming completion request through middleware to the active provider.

        Yields string token chunks incrementally as they arrive over SSE.
        If an attached Conversation is present, persists buffered prompt turns once
        streaming begins cleanly and appends the assistant reply only on completion.
        """
        if not self._messages:
            raise ValueError("Cannot stream request: at least one message is required.")

        _provider_name, model_name, request = self._build_llm_request(include_tools=False)
        pipeline = self._build_pipeline()
        active_fake = AIFake.get_active()

        done_sentinel = object()
        chunk_queue: asyncio.Queue[object] = asyncio.Queue()
        chunk_consumed = asyncio.Event()
        abort_stream = asyncio.Event()
        terminal_started = False
        stream_completed = False
        accumulated_chunks: list[str] = []

        async def _pump_chunks(
            gen: AsyncIterator[str],
            on_first_chunk: Any | None = None,
        ) -> bool:
            nonlocal stream_completed
            first = True
            gen_iter = gen.__aiter__()
            while not abort_stream.is_set():
                try:
                    aborted, chunk = await _await_or_abort(
                        _next_stream_chunk(gen_iter),
                        abort_stream,
                    )
                except StopAsyncIteration:
                    stream_completed = True
                    break
                if aborted:
                    break
                if first and on_first_chunk is not None:
                    on_first_chunk()
                first = False
                chunk_text = str(chunk)
                accumulated_chunks.append(chunk_text)
                chunk_consumed.clear()
                chunk_queue.put_nowait(chunk_text)
                aborted, _ = await _await_or_abort(chunk_consumed.wait(), abort_stream)
                if aborted:
                    break
            return stream_completed

        async def stream_terminal(req: LLMRequest) -> LLMResponse:
            nonlocal terminal_started
            terminal_started = True
            prompt_chars = sum(len(m.content or "") for m in req.messages)
            input_tokens = 0 if prompt_chars == 0 else max(1, prompt_chars // 4)

            if active_fake is not None:
                gen = active_fake.handle_stream(self, req)
                try:
                    await _pump_chunks(gen)
                finally:
                    if not stream_completed and isinstance(gen, AsyncGenerator):
                        await gen.aclose()
                full_text = "".join(accumulated_chunks)
                output_tokens = _estimate_token_count(full_text)
                return LLMResponse(
                    content=full_text,
                    model=req.model or model_name or "fake-model",
                    usage=TokenUsage(
                        prompt_tokens=input_tokens,
                        completion_tokens=output_tokens,
                        total_tokens=input_tokens + output_tokens,
                    ),
                    finish_reason="stop" if stream_completed else "cancelled",
                )

            provider = self._resolve_provider()
            observer = get_observability()
            obs_model = req.model or provider.get_default_model()
            prompt_hash = compute_prompt_hash(req.messages)
            obs_ctx = ObservationContext(
                model=obs_model,
                provider=provider.name,
                prompt_hash=prompt_hash,
                metadata={"stream": True, **dict(req.extra), **dict(req.metadata)},
            )
            stream_start = time.perf_counter()
            gen = provider.stream(req)
            try:
                await _pump_chunks(
                    gen,
                    on_first_chunk=lambda: obs_ctx.record_first_token(stream_start),
                )
            except (asyncio.CancelledError, GeneratorExit):
                obs_ctx.status = "cancelled"
                obs_ctx.error_message = "Stream consumer disconnected or cancelled."
                raise
            except Exception as exc:
                obs_ctx.status = "error"
                obs_ctx.error_message = str(exc)
                raise
            finally:
                if not stream_completed and isinstance(gen, AsyncGenerator):
                    await gen.aclose()
                latency_ms = (time.perf_counter() - stream_start) * 1000.0
                obs_ctx.duration_ms = latency_ms
                if not stream_completed and obs_ctx.status == "success":
                    obs_ctx.status = "cancelled"
                full_text = "".join(accumulated_chunks)
                output_tokens = _estimate_token_count(full_text)
                obs_ctx.set_tokens(input_tokens, output_tokens)
                await observer.record(
                    model=obs_ctx.model,
                    provider=obs_ctx.provider,
                    input_tokens=obs_ctx.input_tokens,
                    output_tokens=obs_ctx.output_tokens,
                    latency_ms=latency_ms,
                    prompt_hash=obs_ctx.prompt_hash,
                    cost=obs_ctx.cost,
                    status=obs_ctx.status,
                    error_message=obs_ctx.error_message,
                    metadata=obs_ctx.metadata,
                    ttft_ms=obs_ctx.ttft_ms,
                    duration_ms=obs_ctx.duration_ms,
                )

            return LLMResponse(
                content=full_text,
                model=obs_model,
                usage=TokenUsage(
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                ),
                finish_reason="stop" if stream_completed else "cancelled",
            )

        async def _run_pipeline() -> LLMResponse:
            try:
                return await pipeline.execute(request, stream_terminal)
            finally:
                chunk_queue.put_nowait(done_sentinel)

        pipeline_task = asyncio.create_task(_run_pipeline())
        pipeline_task.add_done_callback(_consume_task_exception)

        try:
            while True:
                item = await chunk_queue.get()
                if item is done_sentinel:
                    break
                chunk_str = str(item)
                chunk_consumed.set()
                self._commit_conversation_turns(None)
                yield chunk_str

            pipeline_response = await pipeline_task
            if not terminal_started:
                self._commit_conversation_turns(None)
                if pipeline_response.content:
                    yield pipeline_response.content
                self._commit_conversation_turns(pipeline_response.content)
            else:
                self._commit_conversation_turns("".join(accumulated_chunks))
        except (asyncio.CancelledError, GeneratorExit):
            abort_stream.set()
            if not pipeline_task.done():
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await pipeline_task
            raise
        finally:
            abort_stream.set()
            if not pipeline_task.done():
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await pipeline_task


class AI:
    """
    High-level ergonomic facade for the SwiftPY AI native module.

    Target usage:
        @app.post("/chat/stream")
        async def chat_stream(body: ChatRequest) -> Response:
            async def tokens():
                async for chunk in (
                    AI.chat("claude-sonnet-4-6")
                    .system("You are a helpful assistant.")
                    .user(body.message)
                    .stream()
                ):
                    yield chunk
            return stream(tokens())
    """

    @classmethod
    def fake(cls, fixtures: FixtureType | None = None) -> AIFake:
        """
        Activate an AIFake test double that intercepts all LLM calls across the application.
        Zero real external API calls are made while active.
        """
        return AIFake(fixtures).activate()

    @classmethod
    def restore(cls) -> None:
        """Deactivate active test fake and restore normal provider execution."""
        active = AIFake.get_active()
        if active is not None:
            active.restore()

    @classmethod
    def add_middleware(cls, middleware: Any) -> None:
        """Register application-wide AI middleware."""
        AIClient.use_global_middleware(middleware)

    @classmethod
    def prompt(
        cls,
        name: str,
        /,
        model: str | None = None,
        provider: str | None = None,
        **variables: Any,
    ) -> AIClient:
        """Start a fluent AIClient initialized with the specified prompt template."""
        client = cls.chat(model=model, provider=provider)
        client.prompt(name, **variables)
        return client

    @classmethod
    async def aprompt(
        cls,
        name: str,
        /,
        model: str | None = None,
        provider: str | None = None,
        **variables: Any,
    ) -> AIClient:
        """Start a fluent AIClient asynchronously initialized with the specified prompt template."""
        client = cls.chat(model=model, provider=provider)
        await client.aprompt(name, **variables)
        return client

    @classmethod
    def chat(
        cls,
        model: str | None = None,
        provider: str | None = None,
        config: Config | None = None,
        http_pool: AIHttpPool | None = None,
    ) -> AIClient:
        """Create a fluent AIClient, automatically inferring provider from model when possible."""
        client = AIClient(config=config, http_pool=http_pool)
        if model:
            client.model(model)
            if not provider:
                lower_model = model.lower()
                if "claude" in lower_model:
                    client.provider("anthropic")
                elif any(p in lower_model for p in ("gpt", "o1", "o3", "davinci", "text-")):
                    client.provider("openai")
        if provider:
            client.provider(provider)
        return client

    @classmethod
    def client(
        cls,
        config: Config | None = None,
        http_pool: AIHttpPool | None = None,
        default_provider: str | None = None,
    ) -> AIClient:
        """Return a new AIClient builder."""
        return AIClient(
            config=config,
            http_pool=http_pool,
            default_provider=default_provider,
        )

    @classmethod
    async def embed(
        cls,
        text: str,
        model: str | None = None,
        provider: str | None = None,
        config: Config | None = None,
        http_pool: AIHttpPool | None = None,
    ) -> list[float]:
        """Generate an embedding vector for a single text."""
        client = EmbeddingsClient(config=config, http_pool=http_pool)
        return await client.embed(text, model=model, provider=provider)

    @classmethod
    async def embed_batch(
        cls,
        texts: Sequence[str],
        model: str | None = None,
        provider: str | None = None,
        chunk_size: int = 100,
        concurrency: int = 5,
        config: Config | None = None,
        http_pool: AIHttpPool | None = None,
    ) -> list[list[float]]:
        """
        Embed a collection of texts with automatic batch chunking and concurrency.
        """
        client = EmbeddingsClient(config=config, http_pool=http_pool)
        return await client.embed_batch(
            texts,
            model=model,
            provider=provider,
            chunk_size=chunk_size,
            concurrency=concurrency,
        )

    @classmethod
    def vectorstore(
        cls,
        adapter: str = "in_memory",
        collection: str = "default",
        dimensions: int = 1536,
        metric: str = "cosine",
        **kwargs: Any,
    ) -> VectorStore:
        """
        Instantiate a VectorStore adapter ('in_memory', 'pgvector', 'qdrant', 'lancedb', 'chroma', 'pinecone').
        """
        norm_adapter = adapter.lower().strip()
        if norm_adapter in ("in_memory", "memory"):
            return VectorStore.in_memory(collection=collection, dimensions=dimensions, metric=metric, **kwargs)
        if norm_adapter in ("pgvector", "postgres", "postgresql"):
            return VectorStore.pgvector(collection=collection, dimensions=dimensions, metric=metric, **kwargs)
        if norm_adapter == "qdrant":
            return VectorStore.qdrant(collection=collection, dimensions=dimensions, metric=metric, **kwargs)
        if norm_adapter == "lancedb":
            return VectorStore.lancedb(collection=collection, dimensions=dimensions, metric=metric, **kwargs)
        if norm_adapter == "chroma":
            return VectorStore.chroma(collection=collection, dimensions=dimensions, metric=metric, **kwargs)
        if norm_adapter == "pinecone":
            return VectorStore.pinecone(collection=collection, dimensions=dimensions, metric=metric, **kwargs)

        raise ValueError(
            f"Unknown vectorstore adapter '{adapter}'. "
            f"Expected one of 'in_memory', 'pgvector', 'qdrant', 'lancedb', 'chroma', 'pinecone'."
        )


def ai(
    config: Config | None = None,
    http_pool: AIHttpPool | None = None,
    default_provider: str | None = None,
) -> AIClient:
    """Convenience factory returning a new fluent AIClient builder."""
    return AIClient(
        config=config,
        http_pool=http_pool,
        default_provider=default_provider,
    )
