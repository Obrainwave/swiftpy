from __future__ import annotations

from collections.abc import AsyncIterator

from swiftpy import Application, Request, Response, json_response
from swiftpy.ai.rag.pipeline_builder import rag
from swiftpy.ai.rag.retriever import Retriever
from swiftpy.ai.vectorstore.base import VectorStore
from swiftpy.auth.jwt_guard import require_jwt
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.core.container import get_container
from swiftpy.database.query_builder import Query
from swiftpy.http.response import StreamingResponse

# Initialize in-memory VectorStore and RAG Pipeline for reproducible benchmarking
_rag_store = VectorStore.in_memory(collection="bench_rag", dimensions=1536)
_retriever = Retriever(vectorstore=_rag_store, top_k=3)
_rag_pipeline = (
    rag()
    .retriever(_retriever)
    .generator(
        lambda prompt, query, **kw: (
            f"Verified answer for query '{query}' based on retrieved context ({len(prompt)} chars)"
        )
    )
)
_rag_initialized = False


async def _ensure_rag_seeded() -> None:
    global _rag_initialized
    if not _rag_initialized:
        if await _rag_store.count() == 0:
            await _retriever.add_documents(
                documents=[
                    "SwiftPY is an enterprise-grade async ASGI Python web framework.",
                    "SwiftPY DI container features constructor auto-wiring and reflection caching.",
                    "SwiftPY routing offers static-first O(1) matching and dynamic regex parameter coercion.",
                    "SwiftPY database pool manages asyncpg connections without leaking resources.",
                    "SwiftPY RAG pipeline coordinates vector retrieval, reranking, and contextual LLM generation.",
                ],
                ids=["doc_core", "doc_di", "doc_router", "doc_db", "doc_rag"],
                metadatas=[
                    {"category": "framework"},
                    {"category": "di"},
                    {"category": "router"},
                    {"category": "db"},
                    {"category": "ai"},
                ],
            )
        _rag_initialized = True


async def benchmark_json(request: Request) -> Response:
    """1. Simple JSON Endpoint: returns fixed JSON payload."""
    return json_response({"status": "ok", "message": "benchmark-simple-json"})


async def benchmark_auth(request: Request) -> Response:
    """2. Authenticated Endpoint: validates JWT bearer token and returns claims."""
    user = await require_jwt(request)
    return json_response(
        {
            "status": "ok",
            "authenticated": True,
            "user_id": user.get("sub"),
        }
    )


async def benchmark_db(request: Request) -> Response:
    """3. DB-backed Endpoint: queries PostgreSQL connection pool via QueryBuilder."""
    record = await Query.table("benchmark_records").where("id", 1).first()
    return json_response(
        {
            "status": "ok",
            "record": record,
        }
    )


async def benchmark_auth_db(request: Request) -> Response:
    """4. Authenticated DB Endpoint: validates JWT bearer token and queries PostgreSQL."""
    user = await require_jwt(request)
    record = await Query.table("benchmark_records").where("id", 1).first()
    return json_response(
        {
            "status": "ok",
            "authenticated": True,
            "user_id": user.get("sub"),
            "record": record,
        }
    )


async def benchmark_stream(request: Request) -> StreamingResponse:
    """5. Streaming Endpoint: yields SSE chunked events."""

    async def _event_stream() -> AsyncIterator[bytes]:
        for i in range(5):
            yield f'data: {{"chunk": {i}, "status": "streaming"}}\n\n'.encode()

    return StreamingResponse(
        _event_stream(),
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


async def benchmark_rag(request: Request) -> Response:
    """6. RAG Endpoint: executes vector retrieval + context formatting + generation."""
    await _ensure_rag_seeded()
    query = "SwiftPY async routing and dependency injection"
    try:
        body = await request.json()
        if isinstance(body, dict) and "query" in body:
            query = str(body["query"])
    except Exception:  # noqa: BLE001, S110
        pass

    result = await _rag_pipeline.run(query)
    return json_response(
        {
            "status": "ok",
            "query": query,
            "answer": result.text,
            "sources": len(result.sources),
            "latency_ms": result.latency_ms,
        }
    )


async def benchmark_token(request: Request) -> Response:
    """Helper Endpoint: issue a valid JWT token for benchmark test clients."""
    container = get_container()
    issuer = container.resolve(JWTIssuer)
    token = issuer.issue_token(
        subject="benchmark_client_1",
        claims={"role": "tester", "name": "Benchmark User"},
        expires_in=86400,
    )
    return json_response({"token": token})


def register_benchmark_routes(app: Application) -> None:
    """Mount all benchmark endpoints on the application."""
    app.get("/benchmarks/json")(benchmark_json)
    app.get("/benchmarks/auth")(benchmark_auth)
    app.get("/benchmarks/db")(benchmark_db)
    app.get("/benchmarks/auth-db")(benchmark_auth_db)
    app.get("/benchmarks/stream")(benchmark_stream)
    app.post("/benchmarks/rag")(benchmark_rag)
    app.get("/benchmarks/rag")(benchmark_rag)
    app.get("/benchmarks/token")(benchmark_token)


__all__ = [
    "benchmark_auth",
    "benchmark_auth_db",
    "benchmark_db",
    "benchmark_json",
    "benchmark_rag",
    "benchmark_stream",
    "benchmark_token",
    "register_benchmark_routes",
]
