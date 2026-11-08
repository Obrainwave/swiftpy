from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Self

from swiftpy.ai.embeddings import EmbeddingsClient
from swiftpy.ai.exceptions import (
    AIConfigurationError,
    AIModuleDisabledError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.providers.anthropic_provider import AnthropicProvider
from swiftpy.ai.providers.base import (
    LLMRequest,
    LLMResponse,
    Message,
    ProviderInterface,
)
from swiftpy.ai.providers.openai_provider import OpenAIProvider
from swiftpy.ai.vectorstore.base import VectorStore
from swiftpy.core.config import Config

if TYPE_CHECKING:
    from swiftpy.ai.conversation import Conversation


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

    _registry: ClassVar[dict[str, type[ProviderInterface]]] = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
    }

    def __init__(
        self,
        config: Config | None = None,
        http_pool: AIHttpPool | None = None,
        default_provider: str | None = None,
    ) -> None:
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
        self._attached_conversation: Conversation | None = None

    @classmethod
    def register_provider(
        cls,
        name: str,
        provider_cls: type[ProviderInterface],
    ) -> None:
        """Register a custom provider adapter class."""
        cls._registry[name.lower()] = provider_cls

    def system(self, content: str) -> Self:
        """Add a system message prompt."""
        msg = Message(role="system", content=content)
        self._messages.append(msg)
        if self._attached_conversation is not None:
            self._attached_conversation.add_message(msg)
        return self

    def user(self, content: str) -> Self:
        """Add a user message prompt."""
        msg = Message(role="user", content=content)
        self._messages.append(msg)
        if self._attached_conversation is not None:
            self._attached_conversation.add_message(msg)
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
        if not isinstance(temp, (int, float)) or not (0.0 <= float(temp) <= 2.0):
            raise ValueError(f"Temperature must be between 0.0 and 2.0, got {temp}")
        self._temperature = float(temp)
        return self

    def max_tokens(self, tokens: int) -> Self:
        """
        Set maximum tokens to generate.
        Must be a positive integer greater than 0.
        """
        if not isinstance(tokens, int) or tokens <= 0:
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
        if not isinstance(p, (int, float)) or not (0.0 <= float(p) <= 1.0):
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

    def extra(self, **kwargs: Any) -> Self:
        """Pass additional provider-specific parameters."""
        self._extra.update(kwargs)
        return self

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

        return provider_cls(config=provider_cfg, http_pool=self._http_pool)

    async def send(self) -> LLMResponse:
        """
        Dispatch the completion request to the active LLM provider.
        """
        if not self._messages:
            raise ValueError("Cannot send request: at least one message is required.")

        provider = self._resolve_provider()

        request = LLMRequest(
            messages=list(self._messages),
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            top_p=self._top_p,
            stop=self._stop,
            extra=dict(self._extra),
        )

        response = await provider.send(request)

        # Record assistant completion in attached conversation if present
        if self._attached_conversation is not None:
            self._attached_conversation.add_assistant(response.content)

        return response

    async def stream(self) -> AsyncIterator[str]:
        """
        Dispatch the streaming completion request to the active LLM provider.

        Yields string token chunks incrementally as they arrive over SSE.
        If an attached Conversation is present, appends the complete assistant
        reply to the conversation ONLY upon successful completion of the full stream.
        """
        if not self._messages:
            raise ValueError("Cannot stream request: at least one message is required.")

        provider = self._resolve_provider()

        request = LLMRequest(
            messages=list(self._messages),
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            top_p=self._top_p,
            stop=self._stop,
            extra=dict(self._extra),
        )

        accumulated_chunks: list[str] = []
        completed = False

        try:
            async for chunk in provider.stream(request):
                accumulated_chunks.append(chunk)
                yield chunk
            completed = True
        finally:
            # Only persist assistant reply if the stream completed normally (not cancelled)
            if completed and self._attached_conversation is not None:
                self._attached_conversation.add_assistant("".join(accumulated_chunks))


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
