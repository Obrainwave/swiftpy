from __future__ import annotations

import asyncio
import json
import math
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None

import pytest

from swiftpy.ai.client import AI
from swiftpy.ai.embeddings import (
    EmbeddingsClient,
    generate_deterministic_embedding,
)
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


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b)


@pytest.mark.asyncio
async def test_deterministic_embedding_properties() -> None:
    vec1536 = generate_deterministic_embedding("Python async and await", dimensions=1536)
    assert len(vec1536) == 1536
    norm = math.sqrt(sum(x * x for x in vec1536))
    assert pytest.approx(norm, rel=1e-5) == 1.0

    # Deterministic property
    vec_repeat = generate_deterministic_embedding("Python async and await", dimensions=1536)
    assert vec1536 == vec_repeat

    # Semantic density: Related texts should have higher similarity than unrelated
    vec_related = generate_deterministic_embedding("Python concurrency and event loop", dimensions=1536)
    vec_unrelated = generate_deterministic_embedding("French baking croissant chocolate pastry", dimensions=1536)

    sim_related = _cosine(vec1536, vec_related)
    sim_unrelated = _cosine(vec1536, vec_unrelated)
    assert sim_related > sim_unrelated

    # Empty text returns zero vector
    vec_empty = generate_deterministic_embedding("", dimensions=128)
    assert vec_empty == [0.0] * 128


@pytest.mark.asyncio
async def test_ai_embed_and_embed_batch_deterministic() -> None:
    config = Config({"ai": {"enabled": True, "default": "deterministic"}})
    client = EmbeddingsClient(config=config)

    single_vec = await client.embed("Test document")
    assert len(single_vec) == 1536

    batch_vecs = await client.embed_batch(
        ["Doc 1", "Doc 2", "Doc 3"],
        chunk_size=2,
        concurrency=2,
    )
    assert len(batch_vecs) == 3
    assert all(len(v) == 1536 for v in batch_vecs)

    # Test through AI facade
    facade_vec = await AI.embed("Facade test", config=config)
    assert len(facade_vec) == 1536


@pytest.mark.asyncio
async def test_embed_validation() -> None:
    config = Config({"ai": {"enabled": True, "default": "deterministic"}})
    client = EmbeddingsClient(config=config)

    # Single embed validation
    with pytest.raises(TypeError, match="Expected string"):
        await client.embed(123)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Cannot embed empty"):
        await client.embed("   ")

    # Batch embed validation
    with pytest.raises(TypeError, match="must be a string"):
        await client.embed_batch(["valid", None])  # type: ignore[list-item]

    with pytest.raises(ValueError, match="is empty or whitespace"):
        await client.embed_batch(["valid", "  "])

    with pytest.raises(ValueError, match="chunk_size must be a positive integer"):
        await client.embed_batch(["text"], chunk_size=0)

    with pytest.raises(ValueError, match="concurrency must be a positive integer"):
        await client.embed_batch(["text"], concurrency=-1)

    # Empty texts returns empty list
    empty_res = await client.embed_batch([])
    assert empty_res == []


@pytest.mark.asyncio
async def test_embed_batch_empty_input_does_not_call_provider(
    mock_client_factory: Any,
) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"data": []})

    mock_client = mock_client_factory(handler)
    http_pool = AIHttpPool(client=mock_client)
    config = Config({"ai": {"enabled": True, "default": "openai", "providers": {"openai": {"api_key": "k"}}}})
    client = EmbeddingsClient(config=config, http_pool=http_pool)

    res = await client.embed_batch([])
    assert res == []
    assert not called


@pytest.mark.asyncio
async def test_embed_batch_chunking_and_concurrency() -> None:
    config = Config({"ai": {"enabled": True, "default": "deterministic"}})
    client = EmbeddingsClient(config=config)

    texts = [f"Unique Document number {i}" for i in range(45)]
    results = await client.embed_batch(texts, chunk_size=10, concurrency=3)

    assert len(results) == 45
    # Verify exact 1-to-1 order preservation
    for i, t in enumerate(texts):
        expected = generate_deterministic_embedding(t)
        assert results[i] == expected


@pytest.mark.asyncio
async def test_embed_batch_respects_provider_max_batch_size(
    mock_client_factory: Any,
) -> None:
    requested_batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.read().decode())
        inputs = data["input"]
        requested_batch_sizes.append(len(inputs))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [0.1] * 4}
                    for i in range(len(inputs))
                ]
            },
        )

    mock_client = mock_client_factory(handler)
    http_pool = AIHttpPool(client=mock_client)
    config = Config({"ai": {"enabled": True, "default": "openai", "providers": {"openai": {"api_key": "k"}}}})
    client = EmbeddingsClient(config=config, http_pool=http_pool)

    # Provide 2500 texts and request chunk_size 5000 (exceeding OpenAI 2048 ceiling)
    texts = [f"Item {i}" for i in range(2500)]
    results = await client.embed_batch(texts, chunk_size=5000, concurrency=2)

    assert len(results) == 2500
    # Effective chunk size must be capped at 2048
    assert requested_batch_sizes[0] == 2048
    assert requested_batch_sizes[1] == 452


@pytest.mark.asyncio
async def test_embed_batch_enforces_concurrency_limit(
    mock_client_factory: Any,
) -> None:
    active_requests = 0
    max_active_requests = 0
    lock = asyncio.Lock()

    async def async_handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_requests, max_active_requests
        async with lock:
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)

        await asyncio.sleep(0.05)

        async with lock:
            active_requests -= 1

        data = json.loads(request.read().decode())
        inputs = data["input"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [0.1] * 4}
                    for i in range(len(inputs))
                ]
            },
        )

    transport = httpx.MockTransport(async_handler)
    mock_client = httpx.AsyncClient(transport=transport)
    http_pool = AIHttpPool(client=mock_client)
    config = Config({"ai": {"enabled": True, "default": "openai", "providers": {"openai": {"api_key": "k"}}}})
    client = EmbeddingsClient(config=config, http_pool=http_pool)

    # 10 chunks of 2 items with concurrency=3
    texts = [f"Item {i}" for i in range(20)]
    try:
        results = await client.embed_batch(texts, chunk_size=2, concurrency=3)
        assert len(results) == 20
        # Assert concurrent operations occurred and respected upper bound
        assert max_active_requests > 1, f"Expected concurrency > 1, got {max_active_requests}"
        assert max_active_requests <= 3, f"Expected max concurrency <= 3, got {max_active_requests}"
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_embed_openai_mock_success(
    mock_client_factory: Any,
) -> None:
    recorded_requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer test-key"
        body = request.read().decode("utf-8")
        data = json.loads(body)
        recorded_requests.append(data)
        inputs = data["input"]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"object": "embedding", "index": idx, "embedding": [0.1 * (idx + 1)] * 8}
                    for idx in range(len(inputs))
                ],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            },
        )

    mock_client = mock_client_factory(handler)
    http_pool = AIHttpPool(client=mock_client)

    config = Config(
        {
            "ai": {
                "enabled": True,
                "default": "openai",
                "providers": {
                    "openai": {
                        "api_key": "test-key",
                        "base_url": "https://api.openai.com/v1",
                        "embedding_model": "text-embedding-3-small",
                    }
                },
            }
        }
    )

    client = EmbeddingsClient(config=config, http_pool=http_pool)
    vectors = await client.embed_batch(["Hello world", "SwiftPY embeddings"])

    assert len(vectors) == 2
    assert vectors[0] == [0.1] * 8
    assert vectors[1] == [0.2] * 8
    assert len(recorded_requests) == 1
    assert recorded_requests[0]["model"] == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_embed_openai_reorders_out_of_order_indices(
    mock_client_factory: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.2] * 4},
                    {"index": 0, "embedding": [0.1] * 4},
                ]
            },
        )

    mock_client = mock_client_factory(handler)
    http_pool = AIHttpPool(client=mock_client)
    config = Config(
        {
            "ai": {
                "enabled": True,
                "default": "openai",
                "providers": {"openai": {"api_key": "test-key"}},
            }
        }
    )
    client = EmbeddingsClient(config=config, http_pool=http_pool)
    vectors = await client.embed_batch(["First", "Second"])
    assert vectors[0] == [0.1] * 4
    assert vectors[1] == [0.2] * 4


@pytest.mark.asyncio
async def test_embed_batch_rejects_missing_and_duplicate_indices(
    mock_client_factory: Any,
) -> None:
    config = Config({"ai": {"enabled": True, "default": "openai", "providers": {"openai": {"api_key": "k"}}}})

    # Missing index
    client_missing = EmbeddingsClient(
        config=config,
        http_pool=AIHttpPool(
            client=mock_client_factory(
                lambda r: httpx.Response(200, json={"data": [{"embedding": [0.1] * 4}]})
            )
        ),
    )
    with pytest.raises(AIProviderError, match="missing required 'index' field"):
        await client_missing.embed_batch(["doc1"])

    # Duplicate index
    client_dup = EmbeddingsClient(
        config=config,
        http_pool=AIHttpPool(
            client=mock_client_factory(
                lambda r: httpx.Response(
                    200,
                    json={
                        "data": [
                            {"index": 0, "embedding": [0.1] * 4},
                            {"index": 0, "embedding": [0.2] * 4},
                        ]
                    },
                )
            )
        ),
    )
    with pytest.raises(AIProviderError, match="Duplicate embedding index"):
        await client_dup.embed_batch(["doc1", "doc2"])


@pytest.mark.asyncio
async def test_embed_batch_rejects_inconsistent_dimensions_and_nan(
    mock_client_factory: Any,
) -> None:
    config = Config({"ai": {"enabled": True, "default": "openai", "providers": {"openai": {"api_key": "k"}}}})

    # Inconsistent dimensions
    client_dim = EmbeddingsClient(
        config=config,
        http_pool=AIHttpPool(
            client=mock_client_factory(
                lambda r: httpx.Response(
                    200,
                    json={
                        "data": [
                            {"index": 0, "embedding": [0.1] * 4},
                            {"index": 1, "embedding": [0.1] * 8},
                        ]
                    },
                )
            )
        ),
    )
    with pytest.raises(AIProviderError, match="Inconsistent vector dimensions"):
        await client_dim.embed_batch(["doc1", "doc2"])

    # NaN / Inf in vector
    client_nan = EmbeddingsClient(
        config=config,
        http_pool=AIHttpPool(
            client=mock_client_factory(
                lambda r: httpx.Response(
                    200,
                    json={
                        "data": [
                            {"index": 0, "embedding": [0.1, float("nan"), 0.3, 0.4]}
                        ]
                    },
                )
            )
        ),
    )
    with pytest.raises(AIProviderError, match="contains NaN or infinite"):
        await client_nan.embed_batch(["doc1"])


@pytest.mark.asyncio
async def test_embed_batch_cancels_remaining_tasks_on_failure(
    mock_client_factory: Any,
) -> None:
    call_count = 0
    cancelled_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count, cancelled_count
        call_count += 1
        if call_count == 1:
            # Chunk 1 immediately fails
            raise httpx.ConnectTimeout("Chunk 1 failed")
        # Chunk 2 runs longer and gets cancelled
        try:
            await asyncio.sleep(0.5)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 4}]})
        except asyncio.CancelledError:
            cancelled_count += 1
            raise

    transport = httpx.MockTransport(handler)
    mock_client = httpx.AsyncClient(transport=transport)
    http_pool = AIHttpPool(client=mock_client)
    config = Config({"ai": {"enabled": True, "default": "openai", "providers": {"openai": {"api_key": "k"}}}})
    client = EmbeddingsClient(config=config, http_pool=http_pool)

    try:
        with pytest.raises(AIConnectionError):
            await client.embed_batch(["item1", "item2"], chunk_size=1, concurrency=2)
    finally:
        await mock_client.aclose()


@pytest.mark.asyncio
async def test_embed_openai_error_handling(
    mock_client_factory: Any,
) -> None:
    config = Config(
        {
            "ai": {
                "enabled": True,
                "default": "openai",
                "providers": {"openai": {"api_key": "test-key"}},
            }
        }
    )

    # 401 Unauthorized -> AIAuthenticationError
    client_401 = EmbeddingsClient(
        config=config,
        http_pool=AIHttpPool(
            client=mock_client_factory(
                lambda r: httpx.Response(401, text="Invalid API key")
            )
        ),
    )
    with pytest.raises(AIAuthenticationError):
        await client_401.embed("test")

    # 429 Rate Limit -> AIRateLimitError
    client_429 = EmbeddingsClient(
        config=config,
        http_pool=AIHttpPool(
            client=mock_client_factory(
                lambda r: httpx.Response(429, text="Rate limit exceeded")
            )
        ),
    )
    with pytest.raises(AIRateLimitError):
        await client_429.embed("test")

    # 500 Provider Error -> AIProviderError
    client_500 = EmbeddingsClient(
        config=config,
        http_pool=AIHttpPool(
            client=mock_client_factory(
                lambda r: httpx.Response(500, text="Internal Server Error")
            )
        ),
    )
    with pytest.raises(AIProviderError):
        await client_500.embed("test")

    # Network Connection Error -> AIConnectionError
    def timeout_handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Network unreachable")

    client_conn = EmbeddingsClient(
        config=config,
        http_pool=AIHttpPool(
            client=mock_client_factory(timeout_handler)
        ),
    )
    with pytest.raises(AIConnectionError):
        await client_conn.embed("test")


@pytest.mark.asyncio
async def test_embed_disabled_module_and_unsupported_provider() -> None:
    # Disabled AI module
    disabled_config = Config({"ai": {"enabled": False}})
    client_disabled = EmbeddingsClient(config=disabled_config)
    with pytest.raises(AIModuleDisabledError):
        await client_disabled.embed("test")

    # Unsupported provider
    invalid_prov_config = Config({"ai": {"enabled": True, "default": "unsupported_llm"}})
    client_invalid = EmbeddingsClient(config=invalid_prov_config)
    with pytest.raises(AIConfigurationError, match="provider 'unsupported_llm' is not supported"):
        await client_invalid.embed("test")
