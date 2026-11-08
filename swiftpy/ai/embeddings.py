from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Sequence
from typing import Any

try:
    import httpx
    _HTTPX_ERRORS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.RequestError)
except ImportError:
    httpx = None
    _HTTPX_ERRORS = (TimeoutError, OSError)

from swiftpy.ai.exceptions import (
    AIAuthenticationError,
    AIConfigurationError,
    AIConnectionError,
    AIModuleDisabledError,
    AIProviderError,
    AIRateLimitError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.core.config import Config
from swiftpy.testing.fakes.ai_fake import AIFake

PROVIDER_MAX_BATCH_SIZE: dict[str, int] = {
    "openai": 2048,
}


def generate_deterministic_embedding(text: str, dimensions: int = 1536) -> list[float]:
    """
    Generate deterministic, normalized test embeddings using character n-grams and SHA-256 feature hashing.

    This provides dependency-free embeddings for development and functional testing;
    it is not a substitute for trained semantic embedding models.
    """
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

    # Normalize vector to unit length (L2 norm)
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0.0:
        vec = [x / norm for x in vec]

    return vec


class EmbeddingsClient:
    """Client for generating text embeddings with batch chunking and concurrency."""

    def __init__(
        self,
        config: Config | None = None,
        http_pool: AIHttpPool | None = None,
    ) -> None:
        if config is None:
            try:
                self._config = Config.get_instance()
            except RuntimeError:
                self._config = Config()
        else:
            self._config = config

        self._http_pool = http_pool or AIHttpPool.get_instance()

    def _resolve_provider_and_key(self, provider_override: str | None) -> tuple[str, str, str, str]:
        if not self._config.get("ai.enabled", True):
            raise AIModuleDisabledError("AI module is disabled in configuration.")

        provider = (provider_override or str(self._config.get("ai.default", "openai"))).lower()
        if provider != "openai" and provider != "deterministic":
            raise AIConfigurationError(
                f"Embedding provider '{provider}' is not supported. Use 'openai' or 'deterministic'."
            )

        raw_cfg = self._config.get(f"ai.providers.{provider}", {})
        prov_cfg: dict[str, Any] = raw_cfg if isinstance(raw_cfg, dict) else {}

        api_key = str(prov_cfg.get("api_key", "")).strip()
        base_url = str(prov_cfg.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        default_model = str(prov_cfg.get("embedding_model", "text-embedding-3-small"))

        return provider, api_key, base_url, default_model

    async def _embed_chunk_openai(
        self,
        texts: list[str],
        model: str,
        base_url: str,
        api_key: str,
    ) -> list[list[float]]:
        if not api_key:
            # Fallback to deterministic embedding for mock/test environments
            return [generate_deterministic_embedding(t) for t in texts]

        url = f"{base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "input": texts,
        }

        client = self._http_pool.get_client()
        try:
            response = await client.post(url, headers=headers, json=payload, timeout=60.0)
        except _HTTPX_ERRORS as exc:
            raise AIConnectionError(
                f"Failed to connect to embedding endpoint: {exc}",
                provider="openai",
            ) from exc

        if response.status_code in (401, 403):
            raise AIAuthenticationError(
                f"OpenAI embedding authentication failed: {response.text}",
                provider="openai",
                status_code=response.status_code,
                response_body=response.text,
            )
        if response.status_code == 429:
            raise AIRateLimitError(
                f"OpenAI embedding rate limit exceeded: {response.text}",
                provider="openai",
                status_code=response.status_code,
                response_body=response.text,
            )
        if response.status_code >= 400:
            raise AIProviderError(
                f"OpenAI embedding error ({response.status_code}): {response.text}",
                provider="openai",
                status_code=response.status_code,
                response_body=response.text,
            )

        try:
            data = response.json()
        except Exception as exc:
            raise AIProviderError("Embedding response is not valid JSON", provider="openai") from exc

        if not isinstance(data, dict):
            raise AIProviderError("Embedding response payload is not a JSON object", provider="openai")

        items = data.get("data")
        if not isinstance(items, list):
            raise AIProviderError("Embedding response missing 'data' list", provider="openai")

        if len(items) != len(texts):
            raise AIProviderError(
                f"Embedding response items count ({len(items)}) does not match request chunk count ({len(texts)})",
                provider="openai",
            )

        seen_indices: set[int] = set()
        vectors_by_index: dict[int, list[float]] = {}
        expected_dim: int | None = None

        for pos, item in enumerate(items):
            if not isinstance(item, dict):
                raise AIProviderError(f"Embedding item at position {pos} is not a dictionary", provider="openai")

            if "index" not in item:
                raise AIProviderError(f"Embedding item at position {pos} is missing required 'index' field", provider="openai")

            raw_idx = item["index"]
            if not isinstance(raw_idx, int) or isinstance(raw_idx, bool):
                raise AIProviderError(
                    f"Embedding item 'index' must be an integer, got {type(raw_idx).__name__}",
                    provider="openai",
                )

            if raw_idx < 0 or raw_idx >= len(texts):
                raise AIProviderError(
                    f"Embedding item index {raw_idx} is out of bounds for chunk of size {len(texts)}",
                    provider="openai",
                )

            if raw_idx in seen_indices:
                raise AIProviderError(f"Duplicate embedding index {raw_idx} received from provider", provider="openai")
            seen_indices.add(raw_idx)

            if "embedding" not in item:
                raise AIProviderError(f"Embedding item at index {raw_idx} is missing 'embedding' vector", provider="openai")

            raw_vec = item["embedding"]
            if not isinstance(raw_vec, list) or not raw_vec:
                raise AIProviderError(f"Embedding item at index {raw_idx} has invalid or empty vector", provider="openai")

            if expected_dim is None:
                expected_dim = len(raw_vec)
            elif len(raw_vec) != expected_dim:
                raise AIProviderError(
                    f"Inconsistent vector dimensions: expected {expected_dim}, got {len(raw_vec)} at index {raw_idx}",
                    provider="openai",
                )

            parsed_vec: list[float] = []
            for val in raw_vec:
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    fval = float(val)
                    if math.isnan(fval) or math.isinf(fval):
                        raise AIProviderError(
                            f"Embedding vector at index {raw_idx} contains NaN or infinite value",
                            provider="openai",
                        )
                    parsed_vec.append(fval)
                else:
                    raise AIProviderError(
                        f"Non-numeric value in embedding vector at index {raw_idx}",
                        provider="openai",
                    )

            vectors_by_index[raw_idx] = parsed_vec

        # Guarantee preservation of original document order
        return [vectors_by_index[i] for i in range(len(texts))]

    async def embed(
        self,
        text: str,
        model: str | None = None,
        provider: str | None = None,
    ) -> list[float]:
        """Generate an embedding vector for a single text."""
        if not isinstance(text, str):
            raise TypeError(f"Expected string for embedding, got {type(text).__name__}")
        if not text.strip():
            raise ValueError("Cannot embed empty or whitespace-only text.")

        results = await self.embed_batch([text], model=model, provider=provider, chunk_size=1)
        return results[0]

    async def embed_batch(
        self,
        texts: Sequence[str],
        model: str | None = None,
        provider: str | None = None,
        chunk_size: int = 100,
        concurrency: int = 5,
    ) -> list[list[float]]:
        """
        Embed a collection of texts, automatically chunking large lists into
        provider-safe batches and executing chunks concurrently with bounded parallelism.
        """
        if not texts:
            return []
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be a positive integer, got {chunk_size}")
        if concurrency <= 0:
            raise ValueError(f"concurrency must be a positive integer, got {concurrency}")

        for idx, t in enumerate(texts):
            if not isinstance(t, str):
                raise TypeError(f"Item at index {idx} must be a string, got {type(t).__name__}")
            if not t.strip():
                raise ValueError(f"Item at index {idx} is empty or whitespace only.")

        active_fake = AIFake.get_active()
        if active_fake is not None:
            return await active_fake.handle_embed(
                texts,
                model=model,
                provider=provider,
            )

        prov_name, api_key, base_url, default_model = self._resolve_provider_and_key(provider)
        target_model = model or default_model

        if prov_name == "deterministic":
            return [generate_deterministic_embedding(t) for t in texts]

        # Respect provider-specific maximum batch size limits
        max_allowed = PROVIDER_MAX_BATCH_SIZE.get(prov_name, chunk_size)
        effective_chunk_size = min(chunk_size, max_allowed)

        # Chunk the input texts into slices of effective_chunk_size
        text_list = list(texts)
        chunks: list[list[str]] = [
            text_list[i : i + effective_chunk_size]
            for i in range(0, len(text_list), effective_chunk_size)
        ]

        semaphore = asyncio.Semaphore(concurrency)

        async def _process_chunk(chunk: list[str]) -> list[list[float]]:
            async with semaphore:
                return await self._embed_chunk_openai(
                    chunk,
                    model=target_model,
                    base_url=base_url,
                    api_key=api_key,
                )

        # Create tasks and handle cancellation on failure
        tasks = [asyncio.create_task(_process_chunk(c)) for c in chunks]
        try:
            chunk_results = await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # Flatten in original order
        embeddings: list[list[float]] = []
        for res in chunk_results:
            embeddings.extend(res)

        return embeddings
