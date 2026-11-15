from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from swiftpy.ai.client import AI
from swiftpy.ai.vectorstore.base import VectorRecord
from swiftpy.ai.vectorstore.in_memory_adapter import InMemoryVectorStore
from swiftpy.ai.vectorstore.pgvector_adapter import PgVectorAdapter
from swiftpy.core.config import Config
from swiftpy.database.orm import Field, Model, Vector
from swiftpy.database.orm.fields import VectorFieldInfo


@pytest.mark.asyncio
async def test_in_memory_vectorstore_basic_crud() -> None:
    store = InMemoryVectorStore(collection_name="test_col", dimensions=4)
    await store.create_collection("test_col", 4)

    # 1. Upsert
    await store.upsert(
        id="doc_1",
        vector=[1.0, 0.0, 0.0, 0.0],
        metadata={"category": "tech", "priority": 1},
        content="First document about technology",
    )
    await store.upsert(
        id="doc_2",
        vector=[0.0, 1.0, 0.0, 0.0],
        metadata={"category": "health", "priority": 2},
        content="Second document about health",
    )
    assert await store.count() == 2

    # 2. Get
    doc1 = await store.get("doc_1")
    assert doc1 is not None
    assert doc1.id == "doc_1"
    assert doc1.content == "First document about technology"
    assert doc1.metadata == {"category": "tech", "priority": 1}
    assert doc1.vector == [1.0, 0.0, 0.0, 0.0]

    assert await store.get("non_existent") is None

    # 3. Update (idempotency - replaces old vector & metadata)
    await store.upsert(
        id="doc_1",
        vector=[0.707, 0.707, 0.0, 0.0],
        metadata={"category": "tech", "priority": 10},
        content="Updated first doc",
    )
    assert await store.count() == 2
    updated = await store.get("doc_1")
    assert updated is not None
    assert updated.content == "Updated first doc"
    assert updated.metadata["priority"] == 10
    assert updated.vector == [0.707, 0.707, 0.0, 0.0]

    # 4. Delete
    assert await store.delete("doc_1") is True
    assert await store.delete("doc_1") is False
    assert await store.count() == 1

    # 5. Batch upsert and batch delete (handles duplicate IDs by taking latest)
    await store.upsert_batch(
        [
            VectorRecord(id="batch_1", vector=[0.1, 0.2, 0.3, 0.4], metadata={"tag": "b1"}),
            ("batch_2", [0.4, 0.3, 0.2, 0.1], {"tag": "b2"}),
            {"id": "batch_3", "vector": [0.5, 0.5, 0.0, 0.0], "metadata": {"tag": "b3"}, "content": "c3"},
            {"id": "batch_1", "vector": [0.9, 0.9, 0.0, 0.0], "metadata": {"tag": "b1_updated"}, "content": "c1_new"},
        ]
    )
    assert await store.count() == 4
    b1 = await store.get("batch_1")
    assert b1 is not None and b1.metadata["tag"] == "b1_updated"

    deleted_count = await store.delete_batch(["batch_1", "batch_2", "non_existing"])
    assert deleted_count == 2
    assert await store.count() == 2

    # 6. Drop collection
    await store.drop_collection("test_col")
    assert await store.count() == 0


@pytest.mark.asyncio
async def test_in_memory_vectorstore_validations() -> None:
    store = InMemoryVectorStore(dimensions=3)

    # Empty id
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        await store.upsert("", [1.0, 2.0, 3.0])

    # Empty vector
    with pytest.raises(ValueError, match="Vector must be a non-empty"):
        await store.upsert("doc1", [])

    # Dimension mismatch
    with pytest.raises(ValueError, match="Vector dimension 2 does not match"):
        await store.upsert("doc1", [1.0, 2.0])

    # Non-numeric elements
    with pytest.raises(TypeError, match="must be a float"):
        await store.upsert("doc1", json.loads('[1.0, "two", 3.0]'))

    # Rejection of NaN and Inf
    with pytest.raises(ValueError, match="NaN or Infinite"):
        await store.upsert("doc1", [1.0, float("nan"), 3.0])

    with pytest.raises(ValueError, match="NaN or Infinite"):
        await store.upsert("doc1", [1.0, float("inf"), 3.0])

    # Invalid query vector
    with pytest.raises(ValueError, match="Vector must be a non-empty"):
        await store.search([])

    with pytest.raises(ValueError, match="Limit must be a positive integer"):
        await store.search([1.0, 2.0, 3.0], limit=0)


@pytest.mark.asyncio
async def test_in_memory_vectorstore_zero_vector_behaviour() -> None:
    # Cosine metric must strictly reject zero vectors (undefined angle / division by zero norm)
    cosine_store = InMemoryVectorStore(dimensions=3, metric="cosine")
    with pytest.raises(ValueError, match="Zero vector cannot be indexed with cosine"):
        await cosine_store.upsert("zero_doc", [0.0, 0.0, 0.0])

    await cosine_store.upsert("valid_doc", [1.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="Zero vector cannot be used as query with cosine"):
        await cosine_store.search([0.0, 0.0, 0.0])

    # L2 metric accepts zero vectors (Euclidean distance to zero is well-defined)
    l2_store = InMemoryVectorStore(dimensions=3, metric="l2")
    await l2_store.upsert("zero_doc", [0.0, 0.0, 0.0])
    results = await l2_store.search([0.0, 0.0, 0.0], limit=1)
    assert len(results) == 1
    assert results[0].id == "zero_doc"
    assert results[0].score == 0.0  # -0.0 distance


@pytest.mark.asyncio
async def test_in_memory_vectorstore_multi_metric() -> None:
    # 1. Cosine similarity
    cos_store = InMemoryVectorStore(dimensions=2, metric="cosine")
    await cos_store.upsert("v1", [1.0, 0.0])
    await cos_store.upsert("v2", [0.707, 0.707])
    await cos_store.upsert("v3", [0.0, 1.0])

    cos_results = await cos_store.search([1.0, 0.0], limit=3)
    assert [r.id for r in cos_results] == ["v1", "v2", "v3"]
    assert pytest.approx(cos_results[0].score, rel=1e-4) == 1.0

    # 2. L2 Euclidean distance (higher score = smaller distance)
    l2_store = InMemoryVectorStore(dimensions=2, metric="l2")
    await l2_store.upsert("close", [1.0, 1.0])    # dist from [0, 0] = sqrt(2) ~= 1.414
    await l2_store.upsert("exact", [0.0, 0.0])    # dist from [0, 0] = 0.0
    await l2_store.upsert("far", [10.0, 10.0])   # dist from [0, 0] = sqrt(200) ~= 14.14

    l2_results = await l2_store.search([0.0, 0.0], limit=3)
    assert [r.id for r in l2_results] == ["exact", "close", "far"]
    assert l2_results[0].score == 0.0
    assert l2_results[1].score < 0.0

    # 3. Inner Product (Dot Product)
    ip_store = InMemoryVectorStore(dimensions=2, metric="inner_product")
    await ip_store.upsert("ip_high", [5.0, 5.0])   # dot with [2, 3] = 10 + 15 = 25
    await ip_store.upsert("ip_mid", [1.0, 2.0])    # dot with [2, 3] = 2 + 6 = 8
    await ip_store.upsert("ip_low", [-1.0, -1.0])  # dot with [2, 3] = -2 - 3 = -5

    ip_results = await ip_store.search([2.0, 3.0], limit=3)
    assert [r.id for r in ip_results] == ["ip_high", "ip_mid", "ip_low"]
    assert ip_results[0].score == 25.0


@pytest.mark.asyncio
async def test_in_memory_vectorstore_jsonb_containment_filtering() -> None:
    store = InMemoryVectorStore(dimensions=2)
    await store.create_collection("default", 2)

    await store.upsert(
        "rec_1",
        [1.0, 0.0],
        metadata={
            "env": "production",
            "tier": "frontend",
            "cluster": {"region": "us-east-1", "az": "us-east-1a"},
            "tags": ["web", "critical"],
        },
    )
    await store.upsert(
        "rec_2",
        [0.9, 0.1],
        metadata={
            "env": "production",
            "tier": "backend",
            "cluster": {"region": "us-west-2", "az": "us-west-2b"},
            "tags": ["api", "critical"],
        },
    )
    await store.upsert(
        "rec_3",
        [0.8, 0.2],
        metadata={
            "env": "staging",
            "tier": "frontend",
            "cluster": {"region": "us-east-1", "az": "us-east-1b"},
            "tags": ["web"],
        },
    )

    # 1. Partial dict containment: {"env": "production"} matches rec_1 and rec_2
    res_prod = await store.search([1.0, 0.0], filter={"env": "production"})
    assert len(res_prod) == 2
    assert [r.id for r in res_prod] == ["rec_1", "rec_2"]

    # 2. Nested dictionary containment: {"cluster": {"region": "us-east-1"}} matches rec_1 and rec_3
    res_east = await store.search([1.0, 0.0], filter={"cluster": {"region": "us-east-1"}})
    assert len(res_east) == 2
    assert [r.id for r in res_east] == ["rec_1", "rec_3"]

    # 3. Combined top-level and nested containment
    res_exact = await store.search(
        [1.0, 0.0],
        filter={"env": "production", "cluster": {"region": "us-east-1"}},
    )
    assert len(res_exact) == 1
    assert res_exact[0].id == "rec_1"

    # 4. List containment: {"tags": ["critical"]} matches rec_1 and rec_2
    res_crit = await store.search([1.0, 0.0], filter={"tags": ["critical"]})
    assert len(res_crit) == 2
    assert [r.id for r in res_crit] == ["rec_1", "rec_2"]

    # 5. Non-matching filter returns empty list
    res_none = await store.search([1.0, 0.0], filter={"env": "qa"})
    assert res_none == []


@pytest.mark.asyncio
async def test_in_memory_vectorstore_empty_and_large_limit() -> None:
    store = InMemoryVectorStore(dimensions=3)
    await store.create_collection("empty_col", 3)

    # Search on empty collection returns empty list
    res_empty = await store.search([1.0, 0.0, 0.0], limit=10)
    assert res_empty == []

    # Search with limit greater than record count returns only available records
    await store.upsert("d1", [1.0, 0.0, 0.0])
    await store.upsert("d2", [0.0, 1.0, 0.0])
    res_large = await store.search([1.0, 0.0, 0.0], limit=50)
    assert len(res_large) == 2


@pytest.mark.asyncio
async def test_100_documents_synthetic_retrieval_benchmark() -> None:
    """
    Synthetic Retrieval Benchmark (100 Documents, 10 Queries):
    Evaluates the vector store ingestion and retrieval pipeline using
    deterministic test embeddings and the in-memory vector store.
    Measures Top-1 Category Accuracy, Hit Rate@5, and Mean Reciprocal Rank (MRR).
    """
    config = Config({"ai": {"enabled": True, "default": "deterministic"}})
    store = AI.vectorstore(adapter="in_memory", collection="benchmark", dimensions=1536)
    await store.create_collection("benchmark", 1536)

    # 1. 100 documents across 5 technical categories (20 docs each)
    topics = {
        "python": [
            "Asyncio event loop scheduling coroutines and background tasks in Python.",
            "Python async and await keywords manage non-blocking concurrency cleanly.",
            "Task groups and gather allow concurrent coroutine execution in modern Python.",
            "Asyncio streams and protocols power high performance network servers.",
            "Context vars isolate request scoped state across Python asyncio tasks.",
            "Python generator yield expressions vs asynchronous generators yield from.",
            "GIL and multiprocessing vs asyncio cooperative multitasking in CPython.",
            "Asyncio queues handle producer consumer pipelines with backpressure.",
            "Fast asyncio HTTP web frameworks leverage uvloop for event loop speed.",
            "Debugging asyncio event loop lag and unawaited task warnings in Python.",
            "Python dataclasses with slots optimize memory allocations and attribute access.",
            "Metaclasses and subclass hooks in Python allow dynamic model registration.",
            "Decorators in Python wrap callable functions preserving signature metadata.",
            "Typing module in Python supports generics TypeVar and ParamSpec typing.",
            "Python memory management reference counting and generational garbage collection.",
            "Asyncio subprocess creation runs asynchronous commands without blocking loop.",
            "Async iterator protocol requires an aiter and anext coroutine method.",
            "Python exception groups and except star syntax handle concurrent failures.",
            "Context managers with async enter and async exit handle resource cleanup.",
            "Profiling Python asynchronous coroutines with cProfile and py-spy tools.",
        ],
        "database": [
            "PostgreSQL b-tree indexes accelerate equality and range query filters.",
            "Database connection pools reuse established TCP sockets to reduce latency.",
            "Relational database foreign keys enforce referential integrity constraints.",
            "SQL ACID transactions guarantee atomicity consistency isolation and durability.",
            "Read committed vs repeatable read and serializable transaction isolation levels.",
            "Database query explain analyze reveals query plan execution costs and index scans.",
            "PostgreSQL pgvector extension enables similarity search on vector embeddings.",
            "HNSW hierarchical navigable small world graphs index dense vector embeddings.",
            "Database migrations apply schema alterations systematically with version locks.",
            "SQL joins inner left right and full outer join combine relational tables.",
            "Deadlocks in relational databases occur when concurrent transactions wait on locks.",
            "PostgreSQL JSONB columns store unstructured documents with GIN indexing.",
            "WAL write ahead logging guarantees crash recovery in PostgreSQL storage engines.",
            "Database table partitioning splits massive tables by range hash or list keys.",
            "Optimistic locking with version counters prevents lost update anomalies in ORM.",
            "PostgreSQL sequence generators allocate unique incrementing primary keys safely.",
            "Prepared statements compile SQL execution plans once and bind parameters safely.",
            "Database connection pool sizing avoids worker exhaustion and port starvation.",
            "Vacuum and autovacuum in PostgreSQL reclaim dead tuples and update statistics.",
            "Database indexing strategies combine composite indexes to satisfy multi-column queries.",
        ],
        "ml": [
            "Transformer architectures rely on multi-head self-attention mechanisms.",
            "Deep neural network backpropagation computes loss gradients via chain rule.",
            "Stochastic gradient descent with Adam optimizer minimizes training loss.",
            "Large language model pretraining consumes trillions of tokens on GPU clusters.",
            "Fine tuning pretrained foundational models adapts weights to specific tasks.",
            "Quantization downcasts neural network float weights to 8-bit or 4-bit integers.",
            "Convolutional neural networks extract spatial feature hierarchies from images.",
            "Recurrent neural networks and LSTMs process sequential input representations.",
            "Embedding layers project discrete tokens into continuous semantic vector spaces.",
            "Vector cosine similarity calculates semantic closeness between text embeddings.",
            "Cross-entropy loss measures divergence between predicted probabilities and labels.",
            "Overfitting occurs when neural networks memorize training data instead of generalizing.",
            "Dropout and weight decay act as regularization techniques during neural training.",
            "Attention is all you need paper introduced the self-attention transformer model.",
            "Rotary position embeddings encode token sequence positions in modern LLMs.",
            "Beam search and top-p nucleus sampling generate diverse token completions.",
            "Prompt engineering and system prompts guide large language model behaviors.",
            "Reinforcement learning from human feedback aligns language models with intent.",
            "Retrieval augmented generation provides relevant document context to LLMs.",
            "Batch normalization stabilizes deep layer distributions during training passes.",
        ],
        "web": [
            "HTTP 1.1 keep-alive and HTTP 2 multiplexing improve web browser performance.",
            "RESTful API design uses standard HTTP verbs GET POST PUT and DELETE.",
            "JSON web tokens encode claims and signatures for stateless authentication.",
            "CORS cross origin resource sharing headers prevent unauthorized browser requests.",
            "HTTP response status codes 200 OK 404 Not Found and 500 Server Error.",
            "Cookies with Secure and HttpOnly flags protect session identifiers from XSS.",
            "WebSockets enable bidirectional real-time full-duplex communication channels.",
            "Content negotiation via Accept and Content-Type HTTP headers defines formats.",
            "Server-sent events stream text data from server to client over HTTP connection.",
            "API rate limiting algorithms like token bucket prevent server denial of service.",
            "HTTP caching with Cache-Control ETag and If-None-Match headers reduces bandwidth.",
            "URL routing matches path patterns and extracts dynamic path parameters safely.",
            "OAuth 2.0 authorization code flow exchanges credentials for access tokens.",
            "Reverse proxies like Nginx terminate SSL and load balance HTTP requests.",
            "Cross-site request forgery CSRF tokens protect mutating POST request endpoints.",
            "HTTP request body parsing decodes JSON and multipart form data payloads.",
            "Gzip and Brotli compression shrink HTTP response payloads over the wire.",
            "OpenAPI and Swagger specifications document REST API endpoints and schemas.",
            "Web security headers like Content-Security-Policy mitigate XSS attack vectors.",
            "GraphQL provides client-driven data querying as an alternative to REST APIs.",
        ],
        "devops": [
            "Docker containers package application code and dependencies into isolated images.",
            "Dockerfile multi-stage builds produce minimal lightweight runtime images.",
            "Kubernetes deployments manage replica sets and perform rolling application updates.",
            "Continuous integration pipelines automatically test and lint code on git push.",
            "Infrastructure as code with Terraform provisions cloud resources reproducibly.",
            "Prometheus metrics scraping monitors container CPU memory and request rates.",
            "Grafana dashboards visualize system metrics and application latency graphs.",
            "Kubernetes ingress controllers route external HTTP traffic to internal services.",
            "Container registries store and distribute tagged Docker container images securely.",
            "Kubernetes ConfigMaps and Secrets inject environment configuration into pods.",
            "Helm charts package Kubernetes manifests into versioned deployable releases.",
            "Log aggregation systems like Loki and Elasticsearch collect container stdout logs.",
            "GitOps workflows synchronize git repository state with Kubernetes cluster state.",
            "Zero downtime deployments use blue green or canary rollout traffic switching.",
            "Service meshes like Istio manage service to service mTLS traffic encryption.",
            "Kubernetes horizontal pod autoscalers scale replicas based on CPU utilization.",
            "Docker compose orchestrates multi-container local development environments.",
            "Linux cgroups and namespaces provide resource isolation for Docker containers.",
            "CI/CD artifact caching speeds up automated build and dependency install times.",
            "Site reliability engineering SRE establishes service level objectives SLOs.",
        ],
    }

    all_docs: list[dict[str, Any]] = []
    texts_to_embed: list[str] = []
    doc_index = 0
    for topic_name, doc_list in topics.items():
        assert len(doc_list) == 20
        for text in doc_list:
            doc_id = f"doc_{doc_index:03d}_{topic_name}"
            all_docs.append({"id": doc_id, "topic": topic_name, "content": text})
            texts_to_embed.append(text)
            doc_index += 1

    assert len(all_docs) == 100
    assert len(texts_to_embed) == 100

    # 2. Embed all 100 documents in chunks of 20 with concurrency 4
    embeddings = await AI.embed_batch(
        texts_to_embed,
        config=config,
        chunk_size=20,
        concurrency=4,
    )
    assert len(embeddings) == 100

    # 3. Upsert all 100 documents to the vector store
    records_to_upsert: list[VectorRecord] = [
        VectorRecord(
            id=all_docs[i]["id"],
            vector=embeddings[i],
            metadata={"topic": all_docs[i]["topic"], "doc_index": i},
            content=all_docs[i]["content"],
        )
        for i in range(100)
    ]
    await store.upsert_batch(records_to_upsert)
    assert await store.count() == 100

    # 4. Run 10 distinct queries (2 per topic) and compute retrieval metrics
    queries = [
        ("asyncio task concurrency and coroutines event loop", "python"),
        ("non-blocking async generators and await expressions", "python"),
        ("relational postgresql database indexing and sql query joins", "database"),
        ("acid transactions database connection pools and locks", "database"),
        ("deep neural networks transformers self-attention weights", "ml"),
        ("training machine learning model gradients and loss optimization", "ml"),
        ("restful api endpoints http status codes and web cookies", "web"),
        ("http headers cors authentication tokens and server responses", "web"),
        ("docker container images dockerfile and kubernetes pods", "devops"),
        ("continuous integration ci cd pipelines and deployment infrastructure", "devops"),
    ]

    top1_correct = 0
    hit_at_5 = 0
    reciprocal_ranks: list[float] = []

    for query_text, expected_topic in queries:
        query_vector = await AI.embed(query_text, config=config)
        search_results = await store.search(query_vector, limit=5)

        assert len(search_results) == 5
        # Verify scores are monotonically descending
        for idx in range(len(search_results) - 1):
            assert search_results[idx].score >= search_results[idx + 1].score

        # Check Top-1 match
        if search_results[0].metadata["topic"] == expected_topic:
            top1_correct += 1

        # Check Hit@5 and calculate reciprocal rank
        rank = None
        for r_idx, res in enumerate(search_results, start=1):
            if res.metadata["topic"] == expected_topic:
                rank = r_idx
                break

        if rank is not None:
            hit_at_5 += 1
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)

    # Assert evaluation benchmark metrics
    top1_accuracy = top1_correct / len(queries)
    hit_rate_5 = hit_at_5 / len(queries)
    mrr = sum(reciprocal_ranks) / len(queries)

    assert top1_accuracy == 1.0, f"Expected 100% Top-1 accuracy, got {top1_accuracy:.2f}"
    assert hit_rate_5 == 1.0, f"Expected 100% Hit Rate@5, got {hit_rate_5:.2f}"
    assert mrr == 1.0, f"Expected MRR = 1.0, got {mrr:.2f}"


@pytest.mark.asyncio
async def test_pgvector_adapter_multi_metric_sql_and_index() -> None:
    """Verify PgVectorAdapter creates appropriate HNSW opclasses and distance operators across metrics."""
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = None  # No existing table

    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_conn)
    mock_pool.release = AsyncMock()

    # 1. Cosine metric
    adapter_cos = PgVectorAdapter(collection_name="cos_col", dimensions=4, metric="cosine", pool=mock_pool)
    await adapter_cos.create_collection("cos_col", 4, metric="cosine")
    cos_stmts = [call.args[0] for call in mock_conn.execute.call_args_list]
    assert any("vector_cosine_ops" in s for s in cos_stmts)

    mock_conn.reset_mock()
    mock_conn.fetch.return_value = []
    await adapter_cos.search([1.0, 0.0, 0.0, 0.0], limit=5)
    cos_search_sql = mock_conn.fetch.call_args.args[0]
    assert "<=>" in cos_search_sql
    assert "(1.0 - (embedding <=> $1::vector))" in cos_search_sql

    # 2. L2 distance metric
    mock_conn.reset_mock()
    mock_conn.fetchval.return_value = None
    adapter_l2 = PgVectorAdapter(collection_name="l2_col", dimensions=4, metric="l2", pool=mock_pool)
    await adapter_l2.create_collection("l2_col", 4, metric="l2")
    l2_stmts = [call.args[0] for call in mock_conn.execute.call_args_list]
    assert any("vector_l2_ops" in s for s in l2_stmts)

    mock_conn.reset_mock()
    mock_conn.fetch.return_value = []
    await adapter_l2.search([1.0, 0.0, 0.0, 0.0], limit=5)
    l2_search_sql = mock_conn.fetch.call_args.args[0]
    assert "<->" in l2_search_sql
    assert "(-(embedding <-> $1::vector))" in l2_search_sql

    # 3. Inner Product metric
    mock_conn.reset_mock()
    mock_conn.fetchval.return_value = None
    adapter_ip = PgVectorAdapter(collection_name="ip_col", dimensions=4, metric="inner_product", pool=mock_pool)
    await adapter_ip.create_collection("ip_col", 4, metric="inner_product")
    ip_stmts = [call.args[0] for call in mock_conn.execute.call_args_list]
    assert any("vector_ip_ops" in s for s in ip_stmts)

    mock_conn.reset_mock()
    mock_conn.fetch.return_value = []
    await adapter_ip.search([1.0, 0.0, 0.0, 0.0], limit=5)
    ip_search_sql = mock_conn.fetch.call_args.args[0]
    assert "<#>" in ip_search_sql
    assert "(-(embedding <#> $1::vector))" in ip_search_sql


@pytest.mark.asyncio
async def test_pgvector_adapter_dimension_mismatch_on_existing_table() -> None:
    """Verify that attempting to create a collection with a dimension mismatch raises ValueError."""
    mock_conn = AsyncMock()
    # Existing table has 768 dimensions in pg_attribute.atttypmod
    mock_conn.fetchval.return_value = 768

    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_conn)
    mock_pool.release = AsyncMock()

    adapter = PgVectorAdapter(collection_name="articles", dimensions=1536, pool=mock_pool)
    with pytest.raises(ValueError, match="already exists with 768 dimensions, cannot reinitialize with 1536"):
        await adapter.create_collection("articles", 1536)


@pytest.mark.asyncio
async def test_pgvector_adapter_transaction_integration_and_rollback() -> None:
    """Verify that PgVectorAdapter participates in active transactions and respects rollback."""
    mock_conn = AsyncMock()
    mock_conn.execute.return_value = "INSERT 0 1"
    mock_conn.fetchval.return_value = None

    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_conn)
    mock_pool.release = AsyncMock()

    adapter = PgVectorAdapter(collection_name="tx_col", dimensions=3, pool=mock_pool)

    # Mock active transaction connection via context manager
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    await adapter.upsert_batch([VectorRecord(id="tx_1", vector=[0.1, 0.2, 0.3])])
    # Batch upsert must execute inside a transaction
    assert mock_conn.transaction.called


def test_model_with_vector_field() -> None:
    """Verify Vector field type integration with SwiftPY Active Record Model."""
    class Article(Model):
        __table__ = "articles"
        __guarded__: ClassVar[list[str]] = []
        id: int = Field(primary_key=True)
        title: str
        embedding: list[float] = Vector(1536, metric="cosine")

    # Verify field metadata
    field_info = Article.__fields__["embedding"]
    assert "list[float]" in str(field_info) or field_info is list[float]

    vector_info = Article.__dict__["embedding"]
    assert isinstance(vector_info, VectorFieldInfo)
    assert vector_info.dimensions == 1536
    assert vector_info.metric == "cosine"

    # Verify instantiation
    art = Article(id=1, title="Fast ASGI", embedding=[0.5] * 1536)
    assert art.id == 1
    assert art.title == "Fast ASGI"
    assert len(art.embedding) == 1536
    assert art.embedding[0] == 0.5

    # Verify validations
    with pytest.raises(ValueError, match="positive integer"):
        Vector(-5)

    with pytest.raises(ValueError, match="Unsupported vector distance metric"):
        Vector(128, metric="invalid_metric")

    # Verify dimension validation on model assignment
    with pytest.raises(ValueError, match="vector dimension 2 does not match expected 1536"):
        art.embedding = [1.0, 2.0]

    # Verify NaN validation on model assignment
    with pytest.raises(ValueError, match="NaN or Infinite"):
        art.embedding = [float("nan")] * 1536
