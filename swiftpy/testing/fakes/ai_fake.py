from __future__ import annotations

import asyncio
import contextvars
import hashlib
import math
import time
import types
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Self

from swiftpy.ai.observability.observer import (
    ObservationContext,
    compute_prompt_hash,
    get_observability,
)
from swiftpy.ai.providers.base import (
    LLMRequest,
    LLMResponse,
    Message,
    TokenUsage,
)

if TYPE_CHECKING:
    from swiftpy.ai.client import AIClient

FixtureType = (
    str
    | LLMResponse
    | Sequence[str | LLMResponse]
    | Mapping[str, str | LLMResponse]
    | Callable[[LLMRequest], str | LLMResponse]
)

_task_fake_stack: contextvars.ContextVar[tuple[AIFake, ...]] = contextvars.ContextVar(
    "_swiftpy_ai_fake_stack", default=()
)


def _deterministic_fake_vector(text: str, dimensions: int = 1536) -> list[float]:
    """Generate deterministic unit-normalized pseudo-embedding vector for test faking."""
    if not text or not text.strip():
        return [0.0] * dimensions

    vec = [0.0] * dimensions
    raw_words = [w.strip(".,;:!?\"'()[]{}").lower() for w in text.split() if w.strip()]

    for word in raw_words:
        tokens = [f"<{word}>"]
        word_len = len(word)
        for n in (3, 4, 5):
            if word_len >= n:
                for i in range(word_len - n + 1):
                    tokens.append(word[i : i + n])

        for tok in tokens:
            digest = hashlib.sha256(tok.encode("utf-8")).digest()
            for i in range(2):
                idx = int.from_bytes(digest[i * 4 : (i + 1) * 4], "big") % dimensions
                sign = 1.0 if digest[i] % 2 == 0 else -1.0
                vec[idx] += sign

    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0.0:
        vec = [x / norm for x in vec]

    return vec


@dataclass
class FakeCall:
    """Telemetry record for an intercepted fake LLM call."""

    request: LLMRequest
    messages: list[Message]
    model: str | None
    provider: str | None
    temperature: float | None
    max_tokens: int | None
    response: LLMResponse
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def prompt_text(self) -> str:
        """Concatenated contents of all request messages."""
        return "\n".join(m.content or "" for m in self.messages)

    @property
    def user_content(self) -> str:
        """Content of the most recent user message."""
        for m in reversed(self.messages):
            if m.role == "user":
                return m.content or ""
        return ""

    @property
    def system_content(self) -> str:
        """Content of the most recent system message."""
        for m in reversed(self.messages):
            if m.role == "system":
                return m.content or ""
        return ""


@dataclass
class FakeEmbeddingCall:
    """Telemetry record for an intercepted fake embedding call."""

    texts: list[str]
    model: str | None
    provider: str | None
    dimensions: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class AIFake:
    """
    Test fake that intercepts all LLM and embedding calls across the application.
    Returns configured canned fixtures, records all invocations,
    triggers observability telemetry without making real external API calls,
    isolates calls per asyncio task via ContextVar, and exposes expressive assertion helpers.
    """

    _global_fake: ClassVar[AIFake | None] = None

    def __init__(
        self,
        fixtures: FixtureType | None = None,
        embedding_fixtures: (
            Mapping[str, list[float]]
            | list[list[float]]
            | Callable[[Sequence[str]], list[list[float]]]
            | None
        ) = None,
    ) -> None:
        self._fixtures = fixtures
        self._embedding_fixtures = embedding_fixtures
        self._queue: list[str | LLMResponse] = []
        self._calls: list[FakeCall] = []
        self._embedding_calls: list[FakeEmbeddingCall] = []
        self._is_active = False

        self._init_fixtures(fixtures)

    def _init_fixtures(self, fixtures: FixtureType | None) -> None:
        if isinstance(fixtures, (list, tuple)):
            self._queue = list(fixtures)
        else:
            self._queue = []

    @classmethod
    def get_active(cls) -> AIFake | None:
        """
        Return the currently active fake instance.
        Resolves task-local active fake first, falling back to global fake if set.
        """
        stack = _task_fake_stack.get()
        if stack:
            return stack[-1]
        return cls._global_fake

    @classmethod
    def set_active(cls, fake: AIFake | None) -> None:
        """Set the active fake instance globally and in current task context."""
        cls._global_fake = fake
        if fake is None:
            _task_fake_stack.set(())
        else:
            stack = _task_fake_stack.get()
            if fake not in stack:
                _task_fake_stack.set((*stack, fake))

    def activate(self) -> Self:
        """Activate interception of all LLM and embedding calls in current task."""
        stack = _task_fake_stack.get()
        if self not in stack:
            _task_fake_stack.set((*stack, self))
        self._is_active = True
        return self

    def restore(self) -> None:
        """Deactivate interception and restore previous active fake or live execution."""
        stack = _task_fake_stack.get()
        if self in stack:
            idx = len(stack) - 1 - stack[::-1].index(self)
            _task_fake_stack.set(stack[:idx] + stack[idx + 1 :])
        if AIFake._global_fake is self:
            AIFake._global_fake = None
        self._is_active = False

    def reset(self) -> None:
        """Clear recorded calls and reset fixture queue."""
        self._calls.clear()
        self._embedding_calls.clear()
        self._init_fixtures(self._fixtures)

    def __enter__(self) -> Self:
        return self.activate()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.restore()

    async def __aenter__(self) -> Self:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)

    @property
    def calls(self) -> list[FakeCall]:
        """Defensive copy of recorded LLM calls."""
        return list(self._calls)

    @property
    def call_count(self) -> int:
        """Total number of intercepted LLM calls."""
        return len(self._calls)

    @property
    def embedding_calls(self) -> list[FakeEmbeddingCall]:
        """Defensive copy of recorded embedding calls."""
        return list(self._embedding_calls)

    @property
    def embedding_call_count(self) -> int:
        """Total number of intercepted embedding calls."""
        return len(self._embedding_calls)

    def _generate_response(self, request: LLMRequest, provider: str) -> LLMResponse:
        """Resolve next canned response for the given request."""
        model_name = request.model or "fake-model"

        if callable(self._fixtures):
            result = self._fixtures(request)
            if isinstance(result, LLMResponse):
                return result
            return self._build_response(str(result), model_name, provider)

        if isinstance(self._fixtures, Mapping):
            # Check model exact or prefix match
            for key, val in self._fixtures.items():
                if key.lower() == model_name.lower() or key.lower() in model_name.lower():
                    if isinstance(val, LLMResponse):
                        return val
                    return self._build_response(str(val), model_name, provider)
            # Check provider match
            if provider.lower() in self._fixtures:
                val = self._fixtures[provider.lower()]
                if isinstance(val, LLMResponse):
                    return val
                return self._build_response(str(val), model_name, provider)
            # Default fallback key if provided
            fallback_val = self._fixtures.get("*")
            if fallback_val is None:
                fallback_val = self._fixtures.get("default")
            if fallback_val is not None:
                if isinstance(fallback_val, LLMResponse):
                    return fallback_val
                return self._build_response(str(fallback_val), model_name, provider)

        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, LLMResponse):
                return item
            return self._build_response(str(item), model_name, provider)

        if isinstance(self._fixtures, str):
            return self._build_response(self._fixtures, model_name, provider)

        if isinstance(self._fixtures, LLMResponse):
            return self._fixtures

        # Default fallback response
        return self._build_response(
            "Fake LLM completion response.",
            model_name,
            provider,
        )

    def _build_response(self, content: str, model: str, provider: str) -> LLMResponse:
        prompt_tokens = 10
        completion_tokens = max(1, len(content) // 4)
        return LLMResponse(
            content=content,
            model=model,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason="stop",
            raw={"faked": True, "provider": provider},
        )

    async def handle_send(self, client: AIClient, request: LLMRequest) -> LLMResponse:
        """
        Intercept AIClient.send() call, record call, notify observability,
        and return canned response.
        """
        provider_name = client._provider_name or "fake_provider"
        model_name = request.model or client._model or "fake-model"

        observer = get_observability()
        async with observer.observe_call(
            model=model_name,
            provider=provider_name,
            messages=request.messages,
            metadata={"faked": True, **dict(request.extra)},
        ) as obs_ctx:
            # Simulate a brief asynchronous pause
            await asyncio.sleep(0.001)

            response = self._generate_response(request, provider_name)
            obs_ctx.set_response(response)

            record = FakeCall(
                request=request,
                messages=list(request.messages),
                model=model_name,
                provider=provider_name,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                response=response,
            )
            self._calls.append(record)

            return response

    async def handle_stream(
        self, client: AIClient, request: LLMRequest
    ) -> AsyncIterator[str]:
        """
        Intercept AIClient.stream() call, record call, notify observability,
        and yield canned tokens incrementally with TTFT tracking.
        """
        provider_name = client._provider_name or "fake_provider"
        model_name = request.model or client._model or "fake-model"

        observer = get_observability()
        prompt_hash = compute_prompt_hash(request.messages)
        obs_ctx = ObservationContext(
            model=model_name,
            provider=provider_name,
            prompt_hash=prompt_hash,
            metadata={"faked": True, "stream": True, **dict(request.extra)},
        )
        response = self._generate_response(request, provider_name)
        obs_ctx.set_response(response)

        record = FakeCall(
            request=request,
            messages=list(request.messages),
            model=model_name,
            provider=provider_name,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            response=response,
        )
        self._calls.append(record)

        content = response.content or ""
        words = content.split(" ")
        stream_start = time.perf_counter()
        first_chunk = True
        completed = False

        try:
            for i, word in enumerate(words):
                chunk = word if i == len(words) - 1 else f"{word} "
                await asyncio.sleep(0.001)
                if first_chunk:
                    obs_ctx.record_first_token(stream_start)
                    first_chunk = False
                yield chunk
            completed = True
        except (asyncio.CancelledError, GeneratorExit) as exc:
            obs_ctx.status = "cancelled"
            obs_ctx.error_message = str(exc) or "Stream consumer disconnected or cancelled."
            raise
        except Exception as exc:
            obs_ctx.status = "error"
            obs_ctx.error_message = str(exc)
            raise
        finally:
            latency_ms = (time.perf_counter() - stream_start) * 1000.0
            obs_ctx.duration_ms = latency_ms
            if not completed and obs_ctx.status == "success":
                obs_ctx.status = "cancelled"
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

    async def handle_embed(
        self,
        texts: Sequence[str],
        model: str | None = None,
        provider: str | None = None,
        dimensions: int = 1536,
    ) -> list[list[float]]:
        """
        Intercept embedding call, record call in telemetry, and return deterministic or fixture vectors.
        """
        text_list = list(texts)
        call = FakeEmbeddingCall(
            texts=text_list,
            model=model,
            provider=provider,
            dimensions=dimensions,
        )
        self._embedding_calls.append(call)

        if callable(self._embedding_fixtures):
            return self._embedding_fixtures(text_list)

        if isinstance(self._embedding_fixtures, Mapping):
            vectors: list[list[float]] = []
            for t in text_list:
                if t in self._embedding_fixtures:
                    vectors.append(list(self._embedding_fixtures[t]))
                elif "*" in self._embedding_fixtures:
                    vectors.append(list(self._embedding_fixtures["*"]))
                else:
                    vectors.append(_deterministic_fake_vector(t, dimensions=dimensions))
            return vectors

        if isinstance(self._embedding_fixtures, list) and self._embedding_fixtures:
            return list(self._embedding_fixtures[: len(text_list)])

        return [_deterministic_fake_vector(t, dimensions=dimensions) for t in text_list]

    # --------------------------------------------------------------------------
    # Assertion Helpers
    # --------------------------------------------------------------------------

    def assert_called(self, times: int | None = None) -> None:
        """
        Assert that the fake was invoked at least once, or exactly `times` times.
        """
        count = len(self._calls)
        if times is None:
            if count == 0:
                raise AssertionError("Expected AI to be called, but zero calls were made.")
        elif count != times:
            raise AssertionError(
                f"Expected AI to be called exactly {times} times, but was called {count} times."
            )

    def assert_called_once(self) -> None:
        """Assert that the fake was invoked exactly once."""
        self.assert_called(times=1)

    def assert_not_called(self) -> None:
        """Assert that no AI calls were made."""
        self.assert_called(times=0)

    def assert_called_with(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        prompt: str | None = None,
        user: str | None = None,
        system: str | None = None,
        messages: Sequence[Message | dict[str, Any]] | None = None,
    ) -> None:
        """
        Assert that at least one recorded call matched all specified parameters.
        """
        if not self._calls:
            raise AssertionError(
                "Expected AI call matching criteria, but zero calls were made."
            )

        matched = False
        reasons: list[str] = []

        for idx, call in enumerate(self._calls):
            mismatch = []
            if model is not None and call.model != model:
                mismatch.append(f"model: expected '{model}', got '{call.model}'")
            if provider is not None and call.provider != provider:
                mismatch.append(f"provider: expected '{provider}', got '{call.provider}'")
            if prompt is not None and prompt not in call.prompt_text:
                mismatch.append(f"prompt substring '{prompt}' not found in '{call.prompt_text}'")
            if user is not None and user not in call.user_content:
                mismatch.append(f"user content '{user}' not found in '{call.user_content}'")
            if system is not None and system not in call.system_content:
                mismatch.append(f"system content '{system}' not found in '{call.system_content}'")

            if messages is not None:
                if len(call.messages) != len(messages):
                    mismatch.append(
                        f"messages length: expected {len(messages)}, got {len(call.messages)}"
                    )
                else:
                    for i, (actual, expected) in enumerate(zip(call.messages, messages)):
                        if isinstance(expected, Message):
                            if (
                                actual.role != expected.role
                                or actual.content != expected.content
                            ):
                                mismatch.append(
                                    f"message[{i}]: expected {expected}, got {actual}"
                                )
                        elif (
                            isinstance(expected, dict)
                            and (
                                actual.role != expected.get("role")
                                or actual.content != expected.get("content")
                            )
                        ):
                            mismatch.append(
                                f"message[{i}]: expected {expected}, got {actual}"
                            )

            if not mismatch:
                matched = True
                break
            reasons.append(f"Call #{idx + 1}: " + "; ".join(mismatch))

        if not matched:
            details = "\n  ".join(reasons)
            raise AssertionError(
                f"No AI call matched criteria.\nActual calls mismatch details:\n  {details}"
            )

    def assert_embedding_called(self, times: int | None = None) -> None:
        """Assert that an embedding call was intercepted."""
        count = len(self._embedding_calls)
        if times is None:
            if count == 0:
                raise AssertionError("Expected embedding to be called, but zero calls were made.")
        elif count != times:
            raise AssertionError(
                f"Expected embedding to be called exactly {times} times, but was called {count} times."
            )

    def assert_embedding_called_once(self) -> None:
        """Assert that exactly one embedding call was made."""
        self.assert_embedding_called(times=1)

    def assert_embedding_not_called(self) -> None:
        """Assert that zero embedding calls were made."""
        self.assert_embedding_called(times=0)

    def assert_embedding_called_with(
        self,
        *,
        text: str | None = None,
        texts: Sequence[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        """Assert that at least one recorded embedding call matched criteria."""
        if not self._embedding_calls:
            raise AssertionError("Expected embedding call matching criteria, but zero calls were made.")

        matched = False
        reasons: list[str] = []

        for idx, call in enumerate(self._embedding_calls):
            mismatch = []
            if text is not None and text not in call.texts:
                mismatch.append(f"text '{text}' not found in call texts: {call.texts}")
            if texts is not None and list(texts) != call.texts:
                mismatch.append(f"texts: expected {list(texts)}, got {call.texts}")
            if model is not None and call.model != model:
                mismatch.append(f"model: expected '{model}', got '{call.model}'")
            if provider is not None and call.provider != provider:
                mismatch.append(f"provider: expected '{provider}', got '{call.provider}'")

            if not mismatch:
                matched = True
                break
            reasons.append(f"Embedding call #{idx + 1}: " + "; ".join(mismatch))

        if not matched:
            details = "\n  ".join(reasons)
            raise AssertionError(
                f"No embedding call matched criteria.\nActual embedding details:\n  {details}"
            )
