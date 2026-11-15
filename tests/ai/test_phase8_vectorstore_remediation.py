"""Phase 8 P8-V1 vector store remediation tests (VEC-1 through VEC-7)."""

from __future__ import annotations

import json
import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from swiftpy.ai.client import AI
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.vectorstore import pgvector_adapter as pg_mod
from swiftpy.ai.vectorstore.base import (
    VectorRecord,
    VectorStore,
)
from swiftpy.ai.vectorstore.chroma_adapter import ChromaAdapter
from swiftpy.ai.vectorstore.in_memory_adapter import InMemoryVectorStore
from swiftpy.ai.vectorstore.lancedb_adapter import LanceDbAdapter
from swiftpy.ai.vectorstore.pgvector_adapter import PgVectorAdapter
from swiftpy.ai.vectorstore.pinecone_adapter import PineconeAdapter
from swiftpy.ai.vectorstore.qdrant_adapter import QdrantAdapter
from swiftpy.core.config import Config
from swiftpy.core.container import Container, set_container


def _resp(status: int = 200, data: dict[str, Any] | list[Any] | None = None) -> httpx.Response:
    return httpx.Response(status, json=data if data is not None else {})


# ---------------------------------------------------------------------------
# VEC-1: LanceDB predicate injection & cross-adapter collection/SSRF validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vec1_lancedb_delete_uses_ids_not_sql_predicate_injection() -> None:
    captured_requests: list[tuple[str, str, dict[str, Any]]] = []
    rows: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode()) if request.content else {}
        captured_requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/insert"):
            for row in body.get("data", []):
                rows[str(row["id"])] = row
            return _resp(200, {"inserted": len(body.get("data", []))})
        if request.url.path.endswith("/query"):
            flt = body.get("filter")
            if isinstance(flt, dict) and "id" in flt:
                target = rows.get(str(flt["id"]))
                # Even if an attacker sends "' OR '1'='1", get() only checks that key,
                # OR if row literally exists under that id, /delete is invoked cleanly.
                return _resp(200, {"rows": [target] if target else []})
            return _resp(200, {"rows": list(rows.values())})
        if request.url.path.endswith("/delete"):
            # Simulate LanceDB honoring predicate if mistakenly sent vs explicit ids
            if "predicate" in body:
                rows.clear()
                return _resp(200, {"deleted": 2})
            deleted = 0
            for item_id in body.get("ids", []):
                if str(item_id) in rows:
                    rows.pop(str(item_id))
                    deleted += 1
            return _resp(200, {"deleted": deleted})
        if request.url.path.endswith("/count"):
            return _resp(200, {"count": len(rows)})
        return _resp(200, {"rows": []})

    pool = AIHttpPool(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    adapter = LanceDbAdapter(
        collection_name="safe_docs",
        dimensions=2,
        base_url="http://mock-lancedb:8080",
        api_key="test-key",
        http_pool=pool,
    )

    await adapter.upsert("doc1", [1.0, 0.0], content="keep 1")
    await adapter.upsert("doc2", [0.0, 1.0], content="keep 2")
    await adapter.upsert("' OR '1'='1", [0.5, 0.5], content="adversarial id")

    deleted = await adapter.delete("' OR '1'='1")
    assert deleted is True
    assert await adapter.count() == 2

    delete_calls = [req for req in captured_requests if req[1].endswith("/delete")]
    assert len(delete_calls) == 1
    assert "predicate" not in delete_calls[0][2]
    assert delete_calls[0][2].get("ids") == ["' OR '1'='1"]


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "../escape",
        "coll/../../etc/passwd",
        "evil.attacker.com#",
        "coll?query=1",
        "bad name",
        "bad'name",
    ],
)
@pytest.mark.asyncio
async def test_vec1_collection_name_rejects_traversal_and_ssrf_payloads(bad_name: str) -> None:
    pool = AIHttpPool(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: _resp())))

    with pytest.raises(ValueError):
        PineconeAdapter(collection_name=bad_name, api_key="k", environment="us-east-1", http_pool=pool)
    with pytest.raises(ValueError):
        QdrantAdapter(collection_name=bad_name, base_url="http://mock-qdrant:6333", http_pool=pool)
    with pytest.raises(ValueError):
        ChromaAdapter(collection_name=bad_name, base_url="http://mock-chroma:8000", http_pool=pool)
    with pytest.raises(ValueError):
        LanceDbAdapter(collection_name=bad_name, base_url="http://mock-lancedb:8080", api_key="k", http_pool=pool)
    with pytest.raises(ValueError):
        InMemoryVectorStore(collection_name=bad_name, dimensions=2)
    with pytest.raises(ValueError):
        PgVectorAdapter(collection_name=bad_name, dimensions=2)

    valid_pinecone = PineconeAdapter(
        collection_name="safe-idx",
        api_key="k",
        environment="us-east-1",
        http_pool=pool,
    )
    with pytest.raises(ValueError):
        await valid_pinecone.create_collection(bad_name, dimensions=2)


@pytest.mark.asyncio
async def test_vec1_pinecone_rejects_invalid_environment_and_url() -> None:
    pool = AIHttpPool(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: _resp())))
    with pytest.raises(ValueError):
        PineconeAdapter(
            collection_name="valid-idx",
            api_key="k",
            environment="us-east-1#evil.com",
            http_pool=pool,
        )
    with pytest.raises(ValueError):
        PineconeAdapter(
            collection_name="valid-idx",
            api_key="k",
            base_url="https://user:pass@evil.attacker.com/path",
            http_pool=pool,
        )


# ---------------------------------------------------------------------------
# VEC-2: LanceDB upsert mode != overwrite, dot metric mapping, consistent score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vec2_lancedb_upsert_preserves_rows_and_uses_dot_metric() -> None:
    seen_create_metrics: list[str] = []
    seen_insert_modes: list[str | None] = []
    seen_query_metrics: list[str | None] = []
    rows: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode()) if request.content else {}
        if request.url.path.endswith("/create"):
            seen_create_metrics.append(str(body.get("metric")))
            return _resp(200, {"status": "created"})
        if request.url.path.endswith("/insert"):
            mode = body.get("mode")
            seen_insert_modes.append(mode)
            if mode == "overwrite":
                rows.clear()
            for row in body.get("data", []):
                rows[str(row["id"])] = row
            return _resp(200, {"inserted": len(body.get("data", []))})
        if request.url.path.endswith("/query"):
            seen_query_metrics.append(body.get("metric"))
            # Simulate LanceDB dot metric returning dist = 1.0 - dot
            return _resp(
                200,
                {
                    "rows": [
                        {
                            "id": r["id"],
                            "content": r.get("content", ""),
                            "metadata": r.get("metadata", {}),
                            "_distance": 1.0 - sum(a * b for a, b in zip(body["vector"], r["vector"], strict=True)),
                            "vector": r["vector"],
                        }
                        for r in rows.values()
                    ]
                },
            )
        return _resp(200, {"count": len(rows)})

    pool = AIHttpPool(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    adapter = LanceDbAdapter(
        collection_name="docs",
        dimensions=2,
        metric="inner_product",
        base_url="http://mock-lancedb:8080",
        api_key="test-key",
        http_pool=pool,
    )

    await adapter.create_collection("docs", dimensions=2, metric="inner_product")
    assert seen_create_metrics == ["dot"]

    await adapter.upsert("r1", [2.0, 0.0], content="first")
    await adapter.upsert("r2", [1.0, 0.0], content="second")

    assert all(m != "overwrite" for m in seen_insert_modes)
    assert await adapter.count() == 2

    hits = await adapter.search([3.0, 0.0], limit=2)
    assert seen_query_metrics == ["dot"]
    assert len(hits) == 2
    assert hits[0].id == "r1"
    assert math.isclose(hits[0].score, 6.0, rel_tol=1e-6)


# ---------------------------------------------------------------------------
# VEC-3 & VEC-4: PgVectorAdapter factory config, HNSW <=63B, deferred state, schema scope, pool lifecycle
# ---------------------------------------------------------------------------


def test_vec3_vectorstore_pgvector_and_ai_vectorstore_accept_config() -> None:
    container = Container()
    set_container(container)
    cfg = Config({"ai": {"enabled": True}})
    container.instance(Config, cfg)

    vs_direct = VectorStore.pgvector(collection="docs", dimensions=4)
    assert isinstance(vs_direct, PgVectorAdapter)

    vs_facade = AI.vectorstore("pgvector", collection="docs", dimensions=4)
    assert isinstance(vs_facade, PgVectorAdapter)
    set_container(Container())


@pytest.mark.asyncio
async def test_vec4_pgvector_long_index_name_deferred_state_and_schema_scope() -> None:
    executed_sql: list[tuple[str, tuple[Any, ...]]] = []

    class FakeConn:
        async def execute(self, sql: str, *args: Any) -> str:
            executed_sql.append((sql, args))
            if "CREATE TABLE" in sql and "fail_collection" in sql:
                raise RuntimeError("simulated ddl failure")
            return "OK"

        async def fetchval(self, sql: str, *args: Any) -> Any:
            executed_sql.append((sql, args))
            return None

    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=FakeConn())
    mock_pool.release = AsyncMock()

    long_name_a = "very_long_vector_collection_name_prefix_alpha_00001"
    long_name_b = "very_long_vector_collection_name_prefix_alpha_00002"
    adapter = PgVectorAdapter(collection_name="initial", dimensions=8, metric="cosine", pool=mock_pool)

    # Index names must stay <= 63 bytes and avoid collision
    idx_a = adapter._get_index_name(long_name_a)
    idx_b = adapter._get_index_name(long_name_b)
    assert len(idx_a.encode("utf-8")) <= 63
    assert len(idx_b.encode("utf-8")) <= 63
    assert idx_a != idx_b

    # Failed create_collection must NOT mutate collection_name/dimensions/metric
    with pytest.raises(RuntimeError, match="simulated ddl failure"):
        await adapter.create_collection("fail_collection", dimensions=16, metric="l2")
    assert adapter._collection_name == "initial"
    assert adapter._dimensions == 8
    assert adapter._metric == "cosine"

    # Successful long collection scopes pg_attribute query with current_schema()
    await adapter.create_collection(long_name_a, dimensions=16, metric="l2")
    assert adapter._collection_name == long_name_a
    assert adapter._dimensions == 16
    assert adapter._metric == "l2"

    attr_queries = [sql for sql, _args in executed_sql if "pg_attribute" in sql]
    assert attr_queries
    assert "current_schema()" in attr_queries[-1]


@pytest.mark.asyncio
async def test_vec4_pgvector_internal_pool_reused_and_closed_via_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_container(Container())
    created_pools: list[Any] = []

    class DummyDbPool:
        def __init__(self, _config: Config) -> None:
            self.init_calls = 0
            self.close_calls = 0
            self._raw_pool: Any = None
            created_pools.append(self)

        def get_pool(self) -> Any:
            if self._raw_pool is None:
                raise RuntimeError("Pool not initialized")
            return self._raw_pool

        async def init(self) -> Any:
            self.init_calls += 1
            conn = MagicMock()
            conn.execute = AsyncMock(return_value="OK")
            conn.fetchval = AsyncMock(return_value=0)

            asyncpg_like = MagicMock()
            asyncpg_like.acquire = AsyncMock(return_value=conn)
            asyncpg_like.release = AsyncMock()
            self._raw_pool = asyncpg_like
            return asyncpg_like

        async def close(self) -> None:
            self.close_calls += 1
            self._raw_pool = None

    monkeypatch.setattr(pg_mod, "DatabasePool", DummyDbPool)
    adapter = PgVectorAdapter(collection_name="c1", dimensions=4)
    await adapter.count()
    await adapter.count()
    assert len(created_pools) == 1
    assert created_pools[0].init_calls == 1

    await adapter.aclose()
    assert created_pools[0].close_calls == 1


# ---------------------------------------------------------------------------
# VEC-5: Pinecone _base_url update on create_collection; Pinecone/Qdrant reserved keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vec5_pinecone_updates_base_url_on_create_collection() -> None:
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host or "")
        if request.url.path == "/indexes":
            return _resp(201, {"name": "idx-two", "host": "idx-two-123.svc.us-west-2.pinecone.io", "status": {"ready": True}})
        return _resp(200, {"upsertedCount": 1})

    pool = AIHttpPool(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    adapter = PineconeAdapter(
        collection_name="idx-one",
        dimensions=2,
        api_key="test-key",
        environment="us-east-1",
        http_pool=pool,
    )
    assert adapter._base_url == "https://idx-one.svc.us-east-1.pinecone.io"

    await adapter.create_collection("idx-two", dimensions=2)
    assert adapter._base_url == "https://idx-two-123.svc.us-west-2.pinecone.io"

    await adapter.upsert("p1", [1.0, 0.0], content="hello")
    assert seen_hosts[-1] == "idx-two-123.svc.us-west-2.pinecone.io"


@pytest.mark.asyncio
async def test_vec5_reserved_metadata_keys_do_not_overwrite_content_or_id() -> None:
    qdrant_points: dict[str, dict[str, Any]] = {}
    pinecone_vectors: dict[str, dict[str, Any]] = {}

    def qdrant_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode()) if request.content else {}
        if request.url.path.endswith("/points"):
            if request.method == "PUT":
                for pt in body.get("points", []):
                    qdrant_points[str(pt["id"])] = pt
                return _resp(200, {"status": "ok"})
            if request.method == "POST":
                found = [qdrant_points[pid] for pid in body.get("ids", []) if pid in qdrant_points]
                return _resp(200, {"result": found})
        if request.url.path.endswith("/points/search"):
            return _resp(
                200,
                {
                    "result": [
                        {"id": pt["id"], "score": 0.99, "payload": pt["payload"], "vector": pt["vector"]}
                        for pt in qdrant_points.values()
                    ]
                },
            )
        return _resp(200, {})

    def pinecone_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode()) if request.content else {}
        if request.url.path == "/vectors/upsert":
            for vec in body.get("vectors", []):
                pinecone_vectors[str(vec["id"])] = vec
            return _resp(200, {"upsertedCount": len(body.get("vectors", []))})
        if request.url.path == "/vectors/fetch":
            ids = request.url.params.get_list("ids")
            return _resp(200, {"vectors": {i: pinecone_vectors[i] for i in ids if i in pinecone_vectors}})
        if request.url.path == "/query":
            return _resp(
                200,
                {
                    "matches": [
                        {
                            "id": v["id"],
                            "score": 0.99,
                            "values": v["values"],
                            "metadata": v.get("metadata", {}),
                        }
                        for v in pinecone_vectors.values()
                    ]
                },
            )
        return _resp(200, {})

    q_pool = AIHttpPool(client=httpx.AsyncClient(transport=httpx.MockTransport(qdrant_handler)))
    p_pool = AIHttpPool(client=httpx.AsyncClient(transport=httpx.MockTransport(pinecone_handler)))

    qdrant = QdrantAdapter(collection_name="c1", dimensions=2, base_url="http://mock-qdrant:6333", http_pool=q_pool)
    pinecone = PineconeAdapter(collection_name="idx", dimensions=2, api_key="k", base_url="https://mock-pinecone.svc.us-east-1.pinecone.io", http_pool=p_pool)

    adversarial_meta = {"_content": "SPOOFED_CONTENT", "_orig_id": "SPOOFED_ID", "tag": "safe"}
    await qdrant.upsert("real-id", [1.0, 0.0], metadata=adversarial_meta, content="REAL_CONTENT")
    await pinecone.upsert("real-id", [1.0, 0.0], metadata=adversarial_meta, content="REAL_CONTENT")

    q_rec = await qdrant.get("real-id")
    p_rec = await pinecone.get("real-id")
    assert q_rec is not None
    assert q_rec.id == "real-id"
    assert q_rec.content == "REAL_CONTENT"
    assert q_rec.metadata == {"tag": "safe"}

    assert p_rec is not None
    assert p_rec.id == "real-id"
    assert p_rec.content == "REAL_CONTENT"
    assert p_rec.metadata == {"tag": "safe"}


# ---------------------------------------------------------------------------
# VEC-6 & VEC-7: Filtering, delete_batch deduplication & N+1, dimensions/atomicity, DI overrides, capabilities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vec6_and_vec7_in_memory_none_filter_dim_lock_atomicity_deepcopy() -> None:
    store = InMemoryVectorStore(collection_name="mem", dimensions=None)

    meta_input: dict[str, Any] = {"nested": {"flag": True}, "deleted_at": None}
    await store.upsert("with_null", [1.0, 0.0], metadata=meta_input, content="a")
    # Mutating caller dict must NOT affect store
    meta_input["nested"]["flag"] = False

    await store.upsert("missing_key", [0.9, 0.1], metadata={"nested": {"flag": False}}, content="b")
    await store.upsert("non_null", [0.8, 0.2], metadata={"deleted_at": "2026-01-01"}, content="c")

    # filter={"deleted_at": None} matches ONLY records with explicit null value
    null_hits = await store.search([1.0, 0.0], limit=10, filter={"deleted_at": None})
    assert [h.id for h in null_hits] == ["with_null"]
    assert null_hits[0].metadata["nested"]["flag"] is True

    # Mutating returned search metadata must NOT leak back into store
    null_hits[0].metadata["nested"]["flag"] = False
    recheck = await store.get("with_null")
    assert recheck is not None
    assert recheck.metadata["nested"]["flag"] is True

    # dimensions=None locked to 2 after first upsert
    with pytest.raises(ValueError):
        await store.upsert("bad_dim", [1.0, 0.0, 0.0])

    # upsert_batch is atomic: failure on record 2 must not persist record 1
    with pytest.raises(ValueError):
        await store.upsert_batch(
            [
                VectorRecord(id="atomic_1", vector=[0.0, 1.0], content="ok"),
                VectorRecord(id="atomic_2", vector=[0.0, 1.0, 2.0], content="bad dim"),
            ]
        )
    assert await store.get("atomic_1") is None

    # Deduplicated delete_batch counts unique IDs once
    deleted = await store.delete_batch(["with_null", "with_null", "missing_key", "nope"])
    assert deleted == 2


@pytest.mark.asyncio
async def test_vec6_http_adapters_dedupe_delete_batch_no_n_plus_1_and_capabilities() -> None:
    get_requests: list[str] = []

    def qdrant_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/points"):
            get_requests.append(request.url.path)
            return _resp(
                200,
                {
                    "result": [
                        {"id": "1", "payload": {"_orig_id": "id1", "_content": "c"}},
                    ]
                },
            )
        if request.url.path.endswith("/points/delete"):
            return _resp(200, {"status": "ok"})
        return _resp(200, {})

    def chroma_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/collections":
            return _resp(200, {"id": "coll-uuid", "name": "c"})
        if request.method == "POST" and request.url.path.endswith("/get"):
            get_requests.append(request.url.path)
            return _resp(200, {"ids": ["id1"]})
        if request.method == "POST" and request.url.path.endswith("/delete"):
            return _resp(200, ["id1"])
        return _resp(200, {})

    def pinecone_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vectors/fetch":
            get_requests.append(request.url.path)
            return _resp(200, {"vectors": {"id1": {"id": "id1", "values": [1.0, 0.0], "metadata": {}}}})
        if request.url.path == "/vectors/delete":
            return _resp(200, {})
        return _resp(200, {})

    qdrant = QdrantAdapter(
        collection_name="c",
        dimensions=2,
        base_url="http://mock-qdrant:6333",
        http_pool=AIHttpPool(client=httpx.AsyncClient(transport=httpx.MockTransport(qdrant_handler))),
    )
    chroma = ChromaAdapter(
        collection_name="c",
        dimensions=2,
        base_url="http://mock-chroma:8000",
        http_pool=AIHttpPool(client=httpx.AsyncClient(transport=httpx.MockTransport(chroma_handler))),
    )
    pinecone = PineconeAdapter(
        collection_name="c",
        dimensions=2,
        api_key="k",
        base_url="https://mock-pinecone.svc.us-east-1.pinecone.io",
        http_pool=AIHttpPool(client=httpx.AsyncClient(transport=httpx.MockTransport(pinecone_handler))),
    )

    for adapter in (qdrant, chroma, pinecone):
        get_requests.clear()
        count = await adapter.delete_batch(["id1", "id1", "missing"])
        assert count == 1
        # Single batched lookup, NOT N separate get() calls
        assert len(get_requests) == 1
        assert adapter.capabilities.pagination is False

    # Qdrant nested metadata & operator validation
    assert InMemoryVectorStore().capabilities.pagination is False
    assert PgVectorAdapter().capabilities.pagination is False
    with pytest.raises(ValueError):
        await pinecone.search([1.0, 0.0], filter={"field": {"$unknownOp": 1}})
    with pytest.raises(ValueError):
        await chroma.search([1.0, 0.0], filter={"field": {"$badOp": 1}})


def test_vec7_di_singleton_honors_explicit_override_args() -> None:
    container = Container()
    set_container(container)
    cfg = Config({"ai": {"enabled": True}})
    container.instance(Config, cfg)
    default_store = InMemoryVectorStore(collection_name="default", dimensions=1536, metric="cosine")
    container.instance(InMemoryVectorStore, default_store)

    # Matching default request reuses DI singleton
    same = VectorStore.in_memory(collection="default", dimensions=1536, metric="cosine")
    assert same is default_store

    # Explicit overrides bypass conflicting singleton instead of silently ignoring args
    custom = VectorStore.in_memory(collection="custom", dimensions=8, metric="l2")
    assert custom is not default_store
    assert isinstance(custom, InMemoryVectorStore)
    assert custom._current_collection == "custom"
    set_container(Container())
