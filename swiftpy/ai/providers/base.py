from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from swiftpy.ai.pool import AIHttpPool

VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant"})


@dataclass(frozen=True)
class Message:
    """Represents a single message in an LLM conversation."""

    role: str
    content: str
    name: str | None = None

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
        if not self.content.strip():
            raise ValueError("Message content cannot be empty or whitespace only")

    def to_dict(self) -> dict[str, str]:
        payload = {"role": self.role.lower(), "content": self.content}
        if self.name:
            payload["name"] = self.name
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
    extra: dict[str, Any] = field(default_factory=dict)


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
        api_key = str(self.config.get("api_key", ""))
        if api_key and len(api_key) > 6:
            message = message.replace(api_key, "[REDACTED]")
        # General pattern redaction for sk- style tokens
        message = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "[REDACTED]", message)
        return message
