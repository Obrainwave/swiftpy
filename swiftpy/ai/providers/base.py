from __future__ import annotations

import asyncio
import json
import math
import random
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

from swiftpy.ai.exceptions import AIConnectionError, AIProviderError, AIRateLimitError
from swiftpy.ai.pool import AIHttpPool

INTERNAL_ATTRIBUTION_KEYS: frozenset[str] = frozenset({"user_id", "tenant_id", "user", "tenant"})

_T = TypeVar("_T")
_SK_TOKEN_RE = re.compile(r"sk-[A-Za-z0-9_-]{12,}")
_BEARER_TOKEN_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE)


def redact_sensitive_text(message: str, api_key: str = "") -> str:
    """Redact API keys, Bearer tokens, and sk-* credentials from text."""
    result = message
    if api_key and len(api_key) > 6:
        result = result.replace(api_key, "[REDACTED]")
    result = _SK_TOKEN_RE.sub("[REDACTED]", result)
    result = _BEARER_TOKEN_RE.sub("Bearer [REDACTED]", result)
    return result


def parse_retry_after(header_value: str | None) -> float | None:
    """Parse Retry-After header supporting both numeric seconds and HTTP-date."""
    if not header_value:
        return None
    raw = str(header_value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        if math.isfinite(seconds):
            return max(0.0, seconds)
        return None
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = (dt - datetime.now(UTC)).total_seconds()
    return max(0.0, round(delta, 2))


def extract_error_message(data: Any, fallback: str) -> str:
    """Safely extract provider error message from parsed JSON without assuming dict shape."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if msg:
                return str(msg)
        elif isinstance(err, str) and err:
            return err
        msg_top = data.get("message")
        if msg_top:
            return str(msg_top)
    return fallback


def strip_internal_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Return copy of extra parameters with framework attribution keys removed."""
    return {k: v for k, v in extra.items() if k not in INTERNAL_ATTRIBUTION_KEYS}

VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


@dataclass(frozen=True)
class ToolCall:
    """Represents a tool call requested by an LLM."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert ToolCall to OpenAI-compatible function call representation."""
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": (
                    json.dumps(self.arguments)
                    if isinstance(self.arguments, dict)
                    else str(self.arguments)
                ),
            },
        }


@dataclass(frozen=True)
class ToolResult:
    """Represents the execution result of an invoked tool."""

    tool_call_id: str
    name: str
    result: Any
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    """Represents a single message in an LLM conversation."""

    role: str
    content: str = ""
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or self.role.lower() not in VALID_ROLES:
            valid = ", ".join(sorted(f"'{r}'" for r in VALID_ROLES))
            raise ValueError(
                f"Invalid message role '{self.role}'. Expected one of: {valid}"
            )
        if not isinstance(self.content, str):
            raise TypeError(
                f"Message content must be a string, got {type(self.content).__name__}"
            )
        if not self.content.strip() and not self.tool_calls:
            raise ValueError(
                "Message content cannot be empty or whitespace only when no tool_calls are present"
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role.lower(), "content": self.content}
        if self.name:
            payload["name"] = self.name
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            payload["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        return payload


@dataclass(frozen=True)
class TokenUsage:
    """Telemetry detailing token consumption for a completion."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMRequest:
    """Unified request parameters passed to an LLM provider adapter."""

    messages: list[Message]
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    """Unified completion response returned by an LLM provider adapter."""

    content: str = ""
    role: str = "assistant"
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    finish_reason: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)



class ProviderInterface(ABC):
    """
    Contract every LLM provider adapter must implement.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        http_pool: AIHttpPool | None = None,
    ) -> None:
        self.config: dict[str, Any] = config or {}
        self._http_pool = http_pool or AIHttpPool.get_instance()

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g. 'openai', 'anthropic')."""

    @abstractmethod
    def get_default_model(self) -> str:
        """Return the default model name configured for this provider."""

    @abstractmethod
    async def send(self, request: LLMRequest) -> LLMResponse:
        """
        Execute a completion request against the provider's API.

        Maps the unified request shape onto the provider's native payload,
        executes the call via the shared HTTP pool, and transforms the response.
        """

    @abstractmethod
    def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        """
        Execute a streaming completion request against the provider's API.

        Yields string token chunks incrementally as they are emitted by the model.
        """

    def sanitize_error(self, message: str) -> str:
        """Sanitize error messages to avoid leaking API credentials in logs/exceptions."""
        return redact_sensitive_text(
            message,
            api_key=str(self.config.get("api_key", "")),
        )

    def _max_retries(self) -> int:
        raw = self.config.get("max_retries", 0)
        if isinstance(raw, bool):
            return 0
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    async def _call_with_retries(self, fn: Callable[[], Awaitable[_T]]) -> _T:
        """Execute an async provider operation honoring configured max_retries."""
        max_retries = self._max_retries()
        base_delay = float(self.config.get("retry_delay", 0.05))
        max_delay = float(self.config.get("max_retry_delay", 2.0))

        attempt = 0
        while True:
            try:
                return await fn()
            except (AIConnectionError, AIRateLimitError, AIProviderError) as exc:
                retryable = isinstance(exc, (AIConnectionError, AIRateLimitError)) or (
                    exc.status_code is not None and exc.status_code >= 500
                )
                if attempt >= max_retries or not retryable:
                    raise

                if isinstance(exc, AIRateLimitError) and exc.retry_after is not None:
                    delay = min(max(0.0, float(exc.retry_after)), max_delay)
                else:
                    delay = min(base_delay * (2**attempt), max_delay) + random.uniform(
                        0.0, min(0.02, base_delay)
                    )
                attempt += 1
                if delay > 0:
                    await asyncio.sleep(delay)
