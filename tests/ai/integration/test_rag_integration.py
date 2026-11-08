from __future__ import annotations

import os
import uuid

import pytest

from swiftpy.ai.client import ai
from swiftpy.ai.embeddings import EmbeddingsClient
from swiftpy.ai.rag.pipeline_builder import rag
from swiftpy.ai.rag.reranker import CohereReranker, CrossEncoderReranker
from swiftpy.ai.rag.result import RAGResult
from swiftpy.ai.rag.retriever import Retriever
from swiftpy.ai.vectorstore import PgVectorAdapter, VectorStore
from swiftpy.core.config import Config
from swiftpy.database.pool import DatabasePool


@pytest.mark.asyncio
async def test_openai_live_rag_integration() -> None:
    """
    Live end-to-end RAG pipeline test against OpenAI APIs.
    Gated by OPENAI_API_KEY environment variable.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set. Skipping live OpenAI RAG integration test.")

    store = VectorStore.in_memory(collection="live_openai_rag", dimensions=1536)
    embeddings = EmbeddingsClient()
    retriever = Retriever(vectorstore=store, embeddings=embeddings, top_k=2)

    await retriever.add_documents(
        documents=[
            "SwiftPY is an asynchronous Python ASGI web framework with built-in dependency injection and Active Record ORM.",
            "PostgreSQL pgvector allows fast vector similarity queries with HNSW and IVFFlat index types.",
        ],
        ids=["doc_swiftpy", "doc_pgvector"],
    )

    client = ai().provider("openai").model("gpt-4o-mini")
    pipeline = (
        rag()
        .retriever(retriever)
        .formatter("xml")
        .generator(client)
        .temperature(0.0)
        .max_tokens(100)
    )

    result: RAGResult = await pipeline.run("What features does SwiftPY provide?")

    assert isinstance(result.text, str)
    assert len(result.text) > 0
    assert len(result.sources) == 2
    assert result.sources[0].id == "doc_swiftpy"
    assert result.tokens.total_tokens > 0
    assert result.cost > 0.0
    assert "gpt-4o-mini" in result.model


@pytest.mark.asyncio
async def test_cohere_live_reranker_integration() -> None:
    """
    Live reranking test against Cohere Rerank API.
    Gated by COHERE_API_KEY environment variable.
    """
    api_key = os.getenv("COHERE_API_KEY")
    if not api_key:
        pytest.skip("COHERE_API_KEY not set. Skipping live Cohere Rerank integration test.")

    reranker = CohereReranker(api_key=api_key, model="rerank-v3.5")
    docs = [
        "A recipe for chocolate chip cookies and baking tips.",
        "PostgreSQL transactions and write-ahead logging ensure ACID compliance.",
        "Gardening advice for spring flowers and soil fertilization.",
    ]

    reranked = await reranker.rerank(
        query="PostgreSQL ACID database transactions",
        documents=docs,
        top_n=1,
    )

    assert len(reranked) == 1
    assert "PostgreSQL" in reranked[0].content
    assert reranked[0].score > 0.5


@pytest.mark.asyncio
async def test_pgvector_live_rag_integration() -> None:
    """
    Live RAG pipeline test against real PostgreSQL pgvector database.
    Gated by SWIFTPY_TEST_PGVECTOR or active DB pool.
    """
    run_live = bool(os.getenv("SWIFTPY_TEST_PGVECTOR") or os.getenv("RUN_INTEGRATION_TESTS"))
    if not run_live:
        pytest.skip("SWIFTPY_TEST_PGVECTOR not set. Skipping live pgvector RAG integration test.")

    db_pool = DatabasePool(Config())
    try:
        await db_pool.init()
    except (OSError, RuntimeError, TimeoutError):
        pytest.skip("Database pool connection failed. Skipping pgvector integration test.")

    coll_name = f"rag_pgv_{uuid.uuid4().hex[:8]}"
    adapter = PgVectorAdapter(
        collection_name=coll_name,
        dimensions=1536,
        metric="cosine",
    )
    await adapter.create_collection(coll_name, dimensions=1536, metric="cosine")

    try:
        retriever = Retriever(vectorstore=adapter, top_k=2)
        await retriever.add_documents(
            documents=[
                "Asyncio provides event loop concurrency in Python.",
                "Docker containers package application dependencies into isolated images.",
            ],
            ids=["pg_doc_1", "pg_doc_2"],
        )

        pipeline = (
            rag()
            .retriever(retriever)
            .reranker(CrossEncoderReranker())
            .formatter("numbered")
            .generator(lambda prompt, query=None, **kw: f"Answered query: {query}")
        )

        res = await pipeline.run("How does asyncio achieve concurrency?")
        assert res.sources[0].id == "pg_doc_1"
        assert "Answered query" in res.text

    finally:
        await adapter.drop_collection(coll_name)
