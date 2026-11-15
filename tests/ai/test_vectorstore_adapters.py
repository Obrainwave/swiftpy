from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from swiftpy.ai.client import AI
from swiftpy.ai.exceptions import (
    AIAuthenticationError,
    AIConnectionError,
    AIModuleDisabledError,
    AIProviderError,
    AIRateLimitError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.vectorstore import (
    ChromaAdapter,
    InMemoryVectorStore,
    LanceDbAdapter,
    PineconeAdapter,
    QdrantAdapter,
    VectorRecord,
    VectorStore,
    VectorStoreCapabilities,
    handle_adapter_http_error,
    sanitize_error_message,
)
from swiftpy.ai.vectorstore.qdrant_adapter import _to_qdrant_id
from swiftpy.core.config import Config
from swiftpy.core.container import Container, set_container

# ==============================================================================
# Lightweight Mock Transport & Response (supports both httpx and fallback)
# ==============================================================================

def _make_response(status_code: int, json_data: Any = None, text: str = "") -> httpx.Response:
    if json_data is not None:
        return httpx.Response(status_code, json=json_data)
    return httpx.Response(status_code, text=text)


# ==============================================================================
# In-Memory Mock Servers for Provider REST APIs
# ==============================================================================

def _math_metrics(q_vec: list[float], pt_vec: list[float]) -> tuple[float, float, float, float]:
    """
    Compute mathematically exact metrics between two vectors:
    Returns (cosine_sim, l2_dist, l2_sq, dot).
    """
    dot = sum(a * b for a, b in zip(q_vec, pt_vec, strict=False))
    norm_q = math.sqrt(sum(a * a for a in q_vec))
    norm_pt = math.sqrt(sum(b * b for b in pt_vec))
    cos_sim = dot / (norm_q * norm_pt) if norm_q > 0 and norm_pt > 0 else 0.0
    l2_sq = sum((a - b) ** 2 for a, b in zip(q_vec, pt_vec, strict=False))
    l2_dist = math.sqrt(max(0.0, l2_sq))
    return cos_sim, l2_dist, l2_sq, dot


class QdrantMockServer:
    """Mock state and HTTP responder for Qdrant REST API v1."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.points: dict[str, dict[str, dict[str, Any]]] = {}

    def handle(self, request: Any) -> Any:
        path = request.url.path
        method = request.method

        # 1. Create collection: PUT /collections/{name}
        if method == "PUT" and path.startswith("/collections/") and "/points" not in path:
            col_name = path.split("/")[-1]
            body = json.loads(request.read().decode()) if request.content else {}
            self.collections[col_name] = body
            self.points.setdefault(col_name, {})
            return _make_response(200, json_data={"result": True, "status": "ok"})

        # 2. Drop collection: DELETE /collections/{name}
        if method == "DELETE" and path.startswith("/collections/") and "/points" not in path:
            col_name = path.split("/")[-1]
            self.collections.pop(col_name, None)
            self.points.pop(col_name, None)
            return _make_response(200, json_data={"result": True, "status": "ok"})

        # 3. Upsert points: PUT /collections/{name}/points
        if method == "PUT" and "/points" in path:
            col_name = path.split("/collections/")[1].split("/points")[0]
            col_pts = self.points.setdefault(col_name, {})
            body = json.loads(request.read().decode())
            for pt in body.get("points", []):
                col_pts[str(pt["id"])] = pt
            return _make_response(200, json_data={"result": {"status": "completed"}, "status": "ok"})

        # 4. Search points: POST /collections/{name}/points/search
        if method == "POST" and path.endswith("/points/search"):
            col_name = path.split("/collections/")[1].split("/points/search")[0]
            col_pts = self.points.get(col_name, {})
            body = json.loads(request.read().decode())
            query_vec = body.get("vector", [])
            limit = int(body.get("limit", 10))
            flt = body.get("filter")

            distance_metric = (
                self.collections.get(col_name, {})
                .get("vectors", {})
                .get("distance", "Cosine")
            )

            scored: list[dict[str, Any]] = []
            for pt_id, pt in col_pts.items():
                payload = pt.get("payload", {})
                if flt and "must" in flt:
                    match = True
                    for clause in flt["must"]:
                        k = clause["key"]
                        v = clause["match"]["value"]
                        if payload.get(k) != v:
                            match = False
                            break
                    if not match:
                        continue

                pt_vec = pt.get("vector", [])
                cos_sim, l2_dist, _, dot = _math_metrics(query_vec, pt_vec)
                if distance_metric == "Euclid":
                    score = l2_dist
                elif distance_metric == "Dot":
                    score = dot
                else:
                    score = cos_sim

                scored.append({
                    "id": pt_id,
                    "score": score,
                    "payload": payload,
                    "vector": pt_vec,
                })

            if distance_metric == "Euclid":
                scored.sort(key=lambda x: x["score"])
            else:
                scored.sort(key=lambda x: x["score"], reverse=True)
            return _make_response(200, json_data={"result": scored[:limit], "status": "ok"})

        # 5. Get points: POST /collections/{name}/points
        if method == "POST" and path.endswith("/points"):
            col_name = path.split("/collections/")[1].split("/points")[0]
            col_pts = self.points.get(col_name, {})
            body = json.loads(request.read().decode())
            ids = [str(i) for i in body.get("ids", [])]
            found = [col_pts[i] for i in ids if i in col_pts]
            return _make_response(200, json_data={"result": found, "status": "ok"})

        # 6. Delete points: POST /collections/{name}/points/delete
        if method == "POST" and path.endswith("/points/delete"):
            col_name = path.split("/collections/")[1].split("/points/delete")[0]
            col_pts = self.points.get(col_name, {})
            body = json.loads(request.read().decode())
            for i in body.get("points", []):
                col_pts.pop(str(i), None)
            return _make_response(200, json_data={"result": {"status": "completed"}, "status": "ok"})

        # 7. Count points: POST /collections/{name}/points/count
        if method == "POST" and path.endswith("/points/count"):
            col_name = path.split("/collections/")[1].split("/points/count")[0]
            col_pts = self.points.get(col_name, {})
            return _make_response(200, json_data={"result": {"count": len(col_pts)}, "status": "ok"})

        return _make_response(404, json_data={"detail": "Not found"})


class ChromaMockServer:
    """Mock state and HTTP responder for Chroma HTTP REST API."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.coll_by_id: dict[str, str] = {}
        self.data: dict[str, dict[str, dict[str, Any]]] = {}

    def handle(self, request: Any) -> Any:
        path = request.url.path
        method = request.method

        # 1. Get collection by name: GET /api/v1/collections/{name}
        if method == "GET" and path.startswith("/api/v1/collections/") and "/count" not in path:
            name = path.split("/")[-1]
            if name in self.collections:
                return _make_response(200, json_data=self.collections[name])
            return _make_response(404, json_data={"detail": "Not found"})

        # 2. Create collection: POST /api/v1/collections
        if method == "POST" and path == "/api/v1/collections":
            body = json.loads(request.read().decode())
            name = body["name"]
            coll_id = f"uuid-{name}"
            info = {"id": coll_id, "name": name, "metadata": body.get("metadata", {})}
            self.collections[name] = info
            self.coll_by_id[coll_id] = name
            self.data.setdefault(coll_id, {})
            return _make_response(201, json_data=info)

        # 3. Drop collection: DELETE /api/v1/collections/{name}
        if method == "DELETE" and path.startswith("/api/v1/collections/"):
            name = path.split("/")[-1]
            coll = self.collections.pop(name, None)
            if coll:
                self.data.pop(coll["id"], None)
            return _make_response(200, json_data={"status": "deleted"})

        # 4. Upsert items: POST /api/v1/collections/{coll_id}/upsert
        if method == "POST" and path.endswith("/upsert"):
            coll_id = path.split("/")[-2]
            coll_data = self.data.setdefault(coll_id, {})
            body = json.loads(request.read().decode())
            ids = body["ids"]
            embeddings = body["embeddings"]
            metas = body.get("metadatas", [{}] * len(ids))
            docs = body.get("documents", [""] * len(ids))

            for idx, item_id in enumerate(ids):
                coll_data[str(item_id)] = {
                    "id": str(item_id),
                    "embedding": embeddings[idx],
                    "metadata": metas[idx],
                    "document": docs[idx],
                }
            return _make_response(200, json_data=True)

        # 5. Query items: POST /api/v1/collections/{coll_id}/query
        if method == "POST" and path.endswith("/query"):
            coll_id = path.split("/")[-2]
            coll_data = self.data.get(coll_id, {})
            coll_name = self.coll_by_id.get(coll_id, "")
            space = (
                self.collections.get(coll_name, {})
                .get("metadata", {})
                .get("hnsw:space", "cosine")
            )

            body = json.loads(request.read().decode())
            q_emb = body["query_embeddings"][0]
            n_results = int(body.get("n_results", 10))
            where = body.get("where")

            scored: list[dict[str, Any]] = []
            for item_id, item in coll_data.items():
                meta = item["metadata"]
                if where:
                    clauses = where.get("$and", [where])
                    match = True
                    for c in clauses:
                        for k, v in c.items():
                            if meta.get(k) != v:
                                match = False
                                break
                    if not match:
                        continue

                cos_sim, _, l2_sq, dot = _math_metrics(q_emb, item["embedding"])
                if space == "l2":
                    distance = l2_sq
                elif space == "ip":
                    distance = 1.0 - dot
                else:
                    distance = 1.0 - cos_sim

                scored.append({
                    "id": item_id,
                    "distance": distance,
                    "metadata": meta,
                    "document": item["document"],
                    "embedding": item["embedding"],
                })

            scored.sort(key=lambda x: x["distance"])
            top = scored[:n_results]

            return _make_response(200, json_data={
                "ids": [[x["id"] for x in top]],
                "distances": [[x["distance"] for x in top]],
                "metadatas": [[x["metadata"] for x in top]],
                "documents": [[x["document"] for x in top]],
                "embeddings": [[x["embedding"] for x in top]],
            })

        # 6. Get items: POST /api/v1/collections/{coll_id}/get
        if method == "POST" and path.endswith("/get"):
            coll_id = path.split("/")[-2]
            coll_data = self.data.get(coll_id, {})
            body = json.loads(request.read().decode())
            target_ids = [str(i) for i in body.get("ids", [])]

            found = [coll_data[i] for i in target_ids if i in coll_data]
            return _make_response(200, json_data={
                "ids": [x["id"] for x in found],
                "metadatas": [x["metadata"] for x in found],
                "documents": [x["document"] for x in found],
                "embeddings": [x["embedding"] for x in found],
            })

        # 7. Delete items: POST /api/v1/collections/{coll_id}/delete
        if method == "POST" and path.endswith("/delete"):
            coll_id = path.split("/")[-2]
            coll_data = self.data.get(coll_id, {})
            body = json.loads(request.read().decode())
            for i in body.get("ids", []):
                coll_data.pop(str(i), None)
            return _make_response(200, json_data=True)

        # 8. Count: GET /api/v1/collections/{coll_id}/count
        if method == "GET" and path.endswith("/count"):
            coll_id = path.split("/")[-2]
            coll_data = self.data.get(coll_id, {})
            return _make_response(200, json_data=len(coll_data))

        return _make_response(404, json_data={"detail": "Not found"})


class PineconeMockServer:
    """Mock state and HTTP responder for Pinecone REST API with full namespace isolation."""

    def __init__(self) -> None:
        self.indexes: dict[str, dict[str, Any]] = {}
        self.namespaces: dict[str, dict[str, dict[str, Any]]] = {"": {}}

    def handle(self, request: Any) -> Any:
        path = request.url.path
        method = request.method

        # 1. Create index: POST https://api.pinecone.io/indexes
        if method == "POST" and path == "/indexes":
            body = json.loads(request.read().decode())
            name = body["name"]
            self.indexes[name] = body
            return _make_response(201, json_data={"status": {"ready": True}})

        # 2. Drop index: DELETE https://api.pinecone.io/indexes/{name}
        if method == "DELETE" and path.startswith("/indexes/"):
            name = path.split("/")[-1]
            self.indexes.pop(name, None)
            self.namespaces.clear()
            self.namespaces[""] = {}
            return _make_response(202, json_data={"status": "terminating"})

        # 3. Upsert: POST /vectors/upsert
        if method == "POST" and path == "/vectors/upsert":
            body = json.loads(request.read().decode())
            ns = body.get("namespace", "")
            ns_vecs = self.namespaces.setdefault(ns, {})
            for v in body.get("vectors", []):
                ns_vecs[str(v["id"])] = v
            return _make_response(200, json_data={"upsertedCount": len(body.get("vectors", []))})

        # 4. Query: POST /query
        if method == "POST" and path == "/query":
            body = json.loads(request.read().decode())
            ns = body.get("namespace", "")
            ns_vecs = self.namespaces.get(ns, {})
            q_vec = body["vector"]
            top_k = int(body.get("topK", 10))
            flt = body.get("filter")
            metric = "cosine"
            if self.indexes:
                metric = next(iter(self.indexes.values())).get("metric", "cosine")

            scored: list[dict[str, Any]] = []
            for v_id, v in ns_vecs.items():
                meta = v.get("metadata", {})
                if flt:
                    match = True
                    for k, spec in flt.items():
                        expected = spec.get("$eq") if isinstance(spec, dict) else spec
                        if meta.get(k) != expected:
                            match = False
                            break
                    if not match:
                        continue

                cos_sim, _, l2_sq, dot = _math_metrics(q_vec, v.get("values", []))
                if metric == "euclidean":
                    score = l2_sq
                elif metric == "dotproduct":
                    score = dot
                else:
                    score = cos_sim

                scored.append({
                    "id": v_id,
                    "score": score,
                    "metadata": meta,
                    "values": v.get("values", []),
                })

            if metric == "euclidean":
                scored.sort(key=lambda x: x["score"])
            else:
                scored.sort(key=lambda x: x["score"], reverse=True)
            return _make_response(200, json_data={"matches": scored[:top_k], "namespace": ns})

        # 5. Fetch: GET /vectors/fetch
        if method == "GET" and path == "/vectors/fetch":
            query_str = request.url.query.decode()
            params = parse_qs(query_str)
            target_ids = params.get("ids", [])
            ns = params.get("namespace", [""])[0]
            ns_vecs = self.namespaces.get(ns, {})

            fetched: dict[str, Any] = {}
            for t_id in target_ids:
                if t_id in ns_vecs:
                    fetched[t_id] = ns_vecs[t_id]
            return _make_response(200, json_data={"vectors": fetched, "namespace": ns})

        # 6. Delete: POST /vectors/delete
        if method == "POST" and path == "/vectors/delete":
            body = json.loads(request.read().decode())
            ns = body.get("namespace", "")
            ns_vecs = self.namespaces.get(ns, {})
            for t_id in body.get("ids", []):
                ns_vecs.pop(str(t_id), None)
            return _make_response(200, json_data={})

        # 7. Stats: POST /describe_index_stats
        if method == "POST" and path == "/describe_index_stats":
            total = sum(len(vecs) for vecs in self.namespaces.values())
            ns_stats = {ns: {"vectorCount": len(vecs)} for ns, vecs in self.namespaces.items()}
            return _make_response(200, json_data={
                "totalVectorCount": total,
                "namespaces": ns_stats,
            })

        return _make_response(404, json_data={"detail": "Not found"})


class LanceDbMockServer:
    """Mock state and HTTP responder for LanceDB REST API."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {}
        self.table_configs: dict[str, dict[str, Any]] = {}

    def handle(self, request: Any) -> Any:
        path = request.url.path
        method = request.method

        # 1. Create table: POST /v1/table/{name}/create
        if method == "POST" and path.endswith("/create"):
            table_name = path.split("/v1/table/")[1].split("/create")[0]
            self.tables.setdefault(table_name, {})
            body = json.loads(request.read().decode()) if request.content else {}
            self.table_configs[table_name] = body
            return _make_response(200, json_data={"status": "created"})

        # 2. Drop table: DELETE /v1/table/{name}
        if method == "DELETE" and path.startswith("/v1/table/"):
            table_name = path.split("/v1/table/")[1]
            self.tables.pop(table_name, None)
            self.table_configs.pop(table_name, None)
            return _make_response(200, json_data={"status": "deleted"})

        # 3. Insert / Upsert: POST /v1/table/{name}/insert
        if method == "POST" and path.endswith("/insert"):
            table_name = path.split("/v1/table/")[1].split("/insert")[0]
            tbl = self.tables.setdefault(table_name, {})
            body = json.loads(request.read().decode())
            data = body.get("data", [])
            for row in data:
                tbl[str(row["id"])] = row
            return _make_response(200, json_data={"inserted": len(data)})

        # 4. Query / Search: POST /v1/table/{name}/query
        if method == "POST" and path.endswith("/query"):
            table_name = path.split("/v1/table/")[1].split("/query")[0]
            tbl = self.tables.get(table_name, {})
            metric = self.table_configs.get(table_name, {}).get("metric", "cosine")
            body = json.loads(request.read().decode())
            q_vec = body.get("vector")
            k = int(body.get("k", 10))
            flt = body.get("filter")

            scored: list[dict[str, Any]] = []
            for r_id, row in tbl.items():
                if flt and isinstance(flt, dict):
                    if "id" in flt and str(row.get("id")) != str(flt["id"]):
                        continue
                    meta = row.get("metadata", {})
                    match = True
                    for fk, fv in flt.items():
                        if fk != "id" and meta.get(fk) != fv:
                            match = False
                            break
                    if not match:
                        continue

                dist = 0.0
                if q_vec and "vector" in row:
                    cos_sim, l2_dist, _, dot = _math_metrics(q_vec, row["vector"])
                    if metric == "l2":
                        dist = l2_dist
                    elif metric == "dot":
                        dist = 1.0 - dot
                    else:
                        dist = 1.0 - cos_sim

                scored.append({
                    "id": r_id,
                    "content": row.get("content", ""),
                    "metadata": row.get("metadata", {}),
                    "_distance": dist,
                    "vector": row.get("vector"),
                })

            scored.sort(key=lambda x: x["_distance"])
            return _make_response(200, json_data={"rows": scored[:k]})

        # 5. Delete: POST /v1/table/{name}/delete
        if method == "POST" and path.endswith("/delete"):
            table_name = path.split("/v1/table/")[1].split("/delete")[0]
            tbl = self.tables.get(table_name, {})
            body = json.loads(request.read().decode())
            if "ids" in body:
                for i in body["ids"]:
                    tbl.pop(str(i), None)
            elif "predicate" in body:
                pred = body["predicate"]
                if "id = '" in pred:
                    del_id = pred.split("id = '")[1].split("'")[0]
                    tbl.pop(del_id, None)
            return _make_response(200, json_data={"deleted": 1})

        # 6. Count: GET /v1/table/{name}/count
        if method == "GET" and path.endswith("/count"):
            table_name = path.split("/v1/table/")[1].split("/count")[0]
            tbl = self.tables.get(table_name, {})
            return _make_response(200, json_data={"count": len(tbl)})

        return _make_response(404, json_data={"detail": "Not found"})


# ==============================================================================
# Helper to construct an adapter wired with mock HTTP transport
# ==============================================================================

def _make_mock_adapter(
    adapter_name: str,
    mock_handler: Callable[[Any], Any],
    collection: str = "default",
    dimensions: int = 4,
    metric: str = "cosine",
    namespace: str = "",
) -> VectorStore:
    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)

    pool = AIHttpPool(client=client)

    if adapter_name == "qdrant":
        return QdrantAdapter(
            collection_name=collection,
            dimensions=dimensions,
            metric=metric,
            base_url="http://mock-qdrant:6333",
            http_pool=pool,
        )
    if adapter_name == "chroma":
        return ChromaAdapter(
            collection_name=collection,
            dimensions=dimensions,
            metric=metric,
            base_url="http://mock-chroma:8000",
            http_pool=pool,
        )
    if adapter_name == "pinecone":
        return PineconeAdapter(
            collection_name=collection,
            dimensions=dimensions,
            metric=metric,
            namespace=namespace,
            base_url="https://mock-pinecone.svc.us-east-1.pinecone.io",
            http_pool=pool,
        )
    if adapter_name == "lancedb":
        return LanceDbAdapter(
            collection_name=collection,
            dimensions=dimensions,
            metric=metric,
            base_url="http://mock-lancedb:8080",
            http_pool=pool,
        )
    if adapter_name == "in_memory":
        return InMemoryVectorStore(
            collection_name=collection,
            dimensions=dimensions,
            metric=metric,
        )
    raise ValueError(f"Unknown adapter {adapter_name}")


# ==============================================================================
# Tests
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_name", ["qdrant", "chroma", "pinecone", "lancedb"])
async def test_all_four_adapters_identical_crud_and_search_behavior(
    adapter_name: str,
) -> None:
    """
    Test requirement:
    'run the same upsert and search operations against all four standalone adapters,
    behaviour must be identical from the developer's perspective, even though each
    provider's underlying API is different.'
    """
    if adapter_name == "qdrant":
        handler = QdrantMockServer().handle
    elif adapter_name == "chroma":
        handler = ChromaMockServer().handle
    elif adapter_name == "pinecone":
        handler = PineconeMockServer().handle
    elif adapter_name == "lancedb":
        handler = LanceDbMockServer().handle
    else:
        raise ValueError(adapter_name)

    store = _make_mock_adapter(adapter_name, handler, collection="test_col", dimensions=4)

    # 1. Create collection
    await store.create_collection("test_col", dimensions=4, metric="cosine")

    # 2. Upsert single records
    await store.upsert(
        id="doc_tech",
        vector=[1.0, 0.0, 0.0, 0.0],
        metadata={"category": "tech", "priority": 1},
        content="Document about technology",
    )
    await store.upsert(
        id="doc_health",
        vector=[0.0, 1.0, 0.0, 0.0],
        metadata={"category": "health", "priority": 2},
        content="Document about health",
    )
    assert await store.count() == 2

    # 3. Retrieve single record (Get)
    tech = await store.get("doc_tech")
    assert tech is not None
    assert tech.id == "doc_tech"
    assert tech.content == "Document about technology"
    assert tech.metadata["category"] == "tech"
    assert tech.metadata["priority"] == 1
    assert tech.vector == [1.0, 0.0, 0.0, 0.0]

    # Non-existent item returns None
    assert await store.get("non_existent") is None

    # 4. Idempotent Update
    await store.upsert(
        id="doc_tech",
        vector=[0.707, 0.707, 0.0, 0.0],
        metadata={"category": "tech", "priority": 99},
        content="Updated document about tech",
    )
    assert await store.count() == 2
    updated = await store.get("doc_tech")
    assert updated is not None
    assert updated.content == "Updated document about tech"
    assert updated.metadata["priority"] == 99

    # 5. Search nearest neighbor and score ordering
    results = await store.search([0.0, 1.0, 0.0, 0.0], limit=5)
    assert len(results) == 2
    # doc_health should rank first because dot product with [0, 1, 0, 0] is 1.0
    assert results[0].id == "doc_health"
    assert results[0].score > results[1].score
    assert results[0].content == "Document about health"

    # 6. Search with metadata filtering
    filtered = await store.search([0.0, 1.0, 0.0, 0.0], limit=5, filter={"category": "health"})
    assert len(filtered) == 1
    assert filtered[0].id == "doc_health"

    # 7. Delete single record
    assert await store.delete("doc_tech") is True
    assert await store.delete("doc_tech") is False  # Second delete returns False
    assert await store.get("doc_tech") is None
    assert await store.count() == 1

    # 8. Batch Upsert
    batch = [
        VectorRecord(id="batch_1", vector=[0.0, 0.0, 1.0, 0.0], metadata={"b": 1}, content="B1"),
        VectorRecord(id="batch_2", vector=[0.0, 0.0, 0.0, 1.0], metadata={"b": 2}, content="B2"),
    ]
    await store.upsert_batch(batch)
    assert await store.count() == 3

    # 9. Batch Delete
    deleted_count = await store.delete_batch(["batch_1", "batch_2", "non_existent"])
    assert deleted_count == 2
    assert await store.count() == 1

    # 10. Drop collection
    await store.drop_collection("test_col")


@pytest.mark.asyncio
async def test_vectorstore_facade_di_resolution() -> None:
    """
    Test requirement:
    'Implement the VectorStore facade: VectorStore.qdrant(), .lancedb(),
    .chroma(), .pinecone(), each returning a configured adapter from the DI container.'
    """
    container = Container()
    set_container(container)

    config = Config({
        "ai": {
            "enabled": True,
            "vectorstores": {
                "qdrant": {"base_url": "http://my-qdrant:6333", "api_key": "q-key"},
                "lancedb": {"base_url": "http://my-lance:8080", "api_key": "l-key"},
                "chroma": {"base_url": "http://my-chroma:8000", "api_key": "c-key"},
                "pinecone": {"base_url": "https://my-pine.io", "api_key": "p-key"},
            },
        }
    })
    container.instance(Config, config)

    # 1. Facade calls on VectorStore
    qdrant = VectorStore.qdrant(collection="coll_q", dimensions=128)
    assert isinstance(qdrant, QdrantAdapter)
    assert qdrant._base_url == "http://my-qdrant:6333"
    assert qdrant._api_key == "q-key"
    assert qdrant._collection == "coll_q"
    assert qdrant._dimensions == 128

    lancedb = VectorStore.lancedb(collection="coll_l", dimensions=256)
    assert isinstance(lancedb, LanceDbAdapter)
    assert lancedb._base_url == "http://my-lance:8080"
    assert lancedb._api_key == "l-key"
    assert lancedb._collection == "coll_l"

    chroma = VectorStore.chroma(collection="coll_c", dimensions=512)
    assert isinstance(chroma, ChromaAdapter)
    assert chroma._base_url == "http://my-chroma:8000/api/v1"
    assert chroma._api_key == "c-key"
    assert chroma._collection == "coll_c"

    pinecone = VectorStore.pinecone(collection="coll_p", dimensions=768)
    assert isinstance(pinecone, PineconeAdapter)
    assert pinecone._base_url == "https://my-pine.io"
    assert pinecone._api_key == "p-key"
    assert pinecone._collection == "coll_p"

    # 2. Facade calls through AI.vectorstore()
    ai_qdrant = AI.vectorstore("qdrant", collection="ai_q", dimensions=128)
    assert isinstance(ai_qdrant, QdrantAdapter)
    assert ai_qdrant._collection == "ai_q"

    ai_lance = AI.vectorstore("lancedb", collection="ai_l", dimensions=256)
    assert isinstance(ai_lance, LanceDbAdapter)

    ai_chroma = AI.vectorstore("chroma", collection="ai_c", dimensions=512)
    assert isinstance(ai_chroma, ChromaAdapter)

    ai_pinecone = AI.vectorstore("pinecone", collection="ai_p", dimensions=768)
    assert isinstance(ai_pinecone, PineconeAdapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_name", ["qdrant", "chroma", "pinecone", "lancedb"])
async def test_adapter_error_translations(adapter_name: str) -> None:
    """Verify that all adapters map HTTP status codes to SwiftPY AI exceptions."""
    # 401 Unauthorized -> AIAuthenticationError
    def auth_error_handler(request: Any) -> Any:
        return _make_response(401, text="Invalid API key")

    store = _make_mock_adapter(adapter_name, auth_error_handler)
    with pytest.raises(AIAuthenticationError, match="Authentication failed"):
        await store.upsert("doc", [1.0, 0.0, 0.0, 0.0])

    # 429 Rate Limit -> AIRateLimitError
    def rate_limit_handler(request: Any) -> Any:
        return _make_response(429, text="Rate limit exceeded")

    store_rl = _make_mock_adapter(adapter_name, rate_limit_handler)
    with pytest.raises(AIRateLimitError, match="Rate limit exceeded"):
        await store_rl.upsert("doc", [1.0, 0.0, 0.0, 0.0])

    # 500 Server Error -> AIProviderError
    def server_error_handler(request: Any) -> Any:
        return _make_response(500, text="Internal engine crash")

    store_500 = _make_mock_adapter(adapter_name, server_error_handler)
    with pytest.raises(AIProviderError, match="API error"):
        await store_500.upsert("doc", [1.0, 0.0, 0.0, 0.0])

    # Connection Error -> AIConnectionError
    def conn_error_handler(request: Any) -> Any:
        raise OSError("Network unreachable")

    store_conn = _make_mock_adapter(adapter_name, conn_error_handler)
    with pytest.raises(AIConnectionError, match="Connection error"):
        await store_conn.upsert("doc", [1.0, 0.0, 0.0, 0.0])


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_name", ["qdrant", "chroma", "pinecone", "lancedb"])
async def test_adapter_vector_validation(adapter_name: str) -> None:
    """Verify that all adapters reject NaN, Infinite, and dimension mismatches."""
    handler = QdrantMockServer().handle
    store = _make_mock_adapter(adapter_name, handler, dimensions=4)

    # 1. Dimension mismatch
    with pytest.raises(ValueError, match="does not match collection dimension 4"):
        await store.upsert("bad_dim", [1.0, 2.0])

    # 2. NaN rejection
    with pytest.raises(ValueError, match="NaN or Infinite"):
        await store.upsert("bad_nan", [1.0, float("nan"), 0.0, 0.0])

    # 3. Infinite rejection
    with pytest.raises(ValueError, match="NaN or Infinite"):
        await store.upsert("bad_inf", [1.0, float("inf"), 0.0, 0.0])

    # 4. Zero magnitude vector for cosine metric
    with pytest.raises(ValueError, match="zero-magnitude vector"):
        await store.upsert("zero_vec", [0.0, 0.0, 0.0, 0.0])


@pytest.mark.asyncio
async def test_disabled_ai_module_raises() -> None:
    """Verify that instantiating an adapter with AI disabled raises AIModuleDisabledError."""
    config = Config({"ai": {"enabled": False}})
    with pytest.raises(AIModuleDisabledError, match="AI module is disabled"):
        QdrantAdapter(config=config)
    with pytest.raises(AIModuleDisabledError, match="AI module is disabled"):
        ChromaAdapter(config=config)
    with pytest.raises(AIModuleDisabledError, match="AI module is disabled"):
        PineconeAdapter(config=config)
    with pytest.raises(AIModuleDisabledError, match="AI module is disabled"):
        LanceDbAdapter(config=config)


# ==============================================================================
# Advanced Review Verification Tests (Section 1 - 8)
# ==============================================================================

@pytest.mark.asyncio
async def test_all_adapter_capabilities() -> None:
    """Verify that all adapters expose their declared VectorStoreCapabilities."""
    dummy_pool = AIHttpPool(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    )

    qdrant = QdrantAdapter(http_pool=dummy_pool)
    chroma = ChromaAdapter(http_pool=dummy_pool)
    pinecone = PineconeAdapter(http_pool=dummy_pool)
    lancedb = LanceDbAdapter(http_pool=dummy_pool)
    in_mem = InMemoryVectorStore()

    for adapter in (qdrant, chroma, pinecone, lancedb, in_mem):
        caps = adapter.capabilities
        assert isinstance(caps, VectorStoreCapabilities)
        assert caps.metadata_filtering is True
        assert caps.batch_upsert is True
        assert "cosine" in caps.native_distance_metrics
        assert "l2" in caps.native_distance_metrics
        assert "inner_product" in caps.native_distance_metrics

    # Pinecone uniquely supports namespaces among external vector stores
    assert pinecone.capabilities.namespaces is True
    assert qdrant.capabilities.namespaces is False
    assert chroma.capabilities.namespaces is False
    assert lancedb.capabilities.namespaces is False
    assert in_mem.capabilities.namespaces is False


@pytest.mark.asyncio
async def test_pinecone_namespace_isolation() -> None:
    """Verify that Pinecone strictly isolates records across distinct namespaces."""
    server = PineconeMockServer()
    # Create two adapters sharing the same index mock but in different namespaces
    store_a = _make_mock_adapter("pinecone", server.handle, collection="default", namespace="tenant_alpha")
    store_b = _make_mock_adapter("pinecone", server.handle, collection="default", namespace="tenant_beta")

    # 1. Upsert identical ID in two distinct namespaces with different vectors and content
    await store_a.upsert(
        id="doc_1",
        vector=[1.0, 0.0, 0.0, 0.0],
        metadata={"tenant": "alpha"},
        content="Alpha content",
    )
    await store_b.upsert(
        id="doc_1",
        vector=[0.0, 1.0, 0.0, 0.0],
        metadata={"tenant": "beta"},
        content="Beta content",
    )

    # 2. Counts must reflect per-namespace totals
    assert await store_a.count() == 1
    assert await store_b.count() == 1

    # 3. Get in tenant_alpha returns Alpha content, not Beta content
    rec_a = await store_a.get("doc_1")
    assert rec_a is not None
    assert rec_a.content == "Alpha content"
    assert rec_a.metadata["tenant"] == "alpha"

    rec_b = await store_b.get("doc_1")
    assert rec_b is not None
    assert rec_b.content == "Beta content"
    assert rec_b.metadata["tenant"] == "beta"

    # 4. Search in tenant_alpha only returns Alpha's vector
    res_a = await store_a.search([1.0, 0.0, 0.0, 0.0], limit=5)
    assert len(res_a) == 1
    assert res_a[0].content == "Alpha content"

    res_b = await store_b.search([1.0, 0.0, 0.0, 0.0], limit=5)
    assert len(res_b) == 1
    assert res_b[0].content == "Beta content"
    # [1,0,0,0] vs [0,1,0,0] has cosine similarity 0.0
    assert abs(res_b[0].score - 0.0) < 1e-4

    # 5. Delete from tenant_alpha does NOT affect tenant_beta
    assert await store_a.delete("doc_1") is True
    assert await store_a.count() == 0
    assert await store_a.get("doc_1") is None

    # Beta remains untouched
    assert await store_b.count() == 1
    assert (await store_b.get("doc_1")) is not None


@pytest.mark.asyncio
async def test_qdrant_string_id_and_metadata_integrity() -> None:
    """Verify Qdrant string ID deterministic mapping, case sensitivity, and metadata cleanliness."""
    # 1. Deterministic UUID preservation
    orig_uuid = "12345678-1234-5678-1234-567812345678"
    assert _to_qdrant_id(orig_uuid) == orig_uuid

    # 2. Deterministic string mapping
    string_id = "user:profile:100"
    mapped_1 = _to_qdrant_id(string_id)
    mapped_2 = _to_qdrant_id(string_id)
    assert mapped_1 == mapped_2
    assert uuid.UUID(mapped_1).version == 5

    # 3. Case sensitivity
    assert _to_qdrant_id("DocumentA") != _to_qdrant_id("documenta")

    # 4. Internal metadata system keys (_orig_id, _content) do not leak into developer metadata
    server = QdrantMockServer()
    store = _make_mock_adapter("qdrant", server.handle, collection="sec_test", dimensions=4)
    await store.create_collection("sec_test", dimensions=4)

    await store.upsert(
        id="clean_meta_doc",
        vector=[1.0, 0.0, 0.0, 0.0],
        metadata={"category": "ai", "tag": "test"},
        content="Sensitive body",
    )

    item = await store.get("clean_meta_doc")
    assert item is not None
    assert item.id == "clean_meta_doc"
    assert item.content == "Sensitive body"
    assert item.metadata == {"category": "ai", "tag": "test"}
    assert "_orig_id" not in item.metadata
    assert "_content" not in item.metadata

    search_items = await store.search([1.0, 0.0, 0.0, 0.0], limit=1)
    assert len(search_items) == 1
    assert search_items[0].id == "clean_meta_doc"
    assert search_items[0].metadata == {"category": "ai", "tag": "test"}
    assert "_orig_id" not in search_items[0].metadata
    assert "_content" not in search_items[0].metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_name", ["qdrant", "chroma", "pinecone", "lancedb", "in_memory"])
async def test_all_adapters_analytical_vectors_and_edge_cases(adapter_name: str) -> None:
    """
    Test identical behavior across all adapters with known analytical vectors
    and important edge cases:
    - Empty collection search returns []
    - Batch with duplicate IDs keeps latest
    - Missing ID returns None
    - Missing delete returns False
    - Orthogonal, identical, opposite vector similarity
    """
    server = (
        QdrantMockServer() if adapter_name == "qdrant"
        else ChromaMockServer() if adapter_name == "chroma"
        else PineconeMockServer() if adapter_name == "pinecone"
        else LanceDbMockServer() if adapter_name == "lancedb"
        else None
    )
    handler = server.handle if server else lambda req: None
    store = _make_mock_adapter(adapter_name, handler, collection="math_test", dimensions=4, metric="cosine")

    # 1. Create collection
    await store.create_collection("math_test", dimensions=4, metric="cosine")

    # 2. Empty collection search returns empty list
    empty_results = await store.search([1.0, 0.0, 0.0, 0.0], limit=5)
    assert empty_results == []

    # 3. Analytical vectors:
    # Q = [1, 0, 0, 0]
    # Identical: [1, 0, 0, 0] -> cosine sim = 1.0
    # Orthogonal: [0, 1, 0, 0] -> cosine sim = 0.0
    # Opposite: [-1, 0, 0, 0] -> cosine sim = -1.0
    await store.upsert_batch([
        VectorRecord(id="v_ident", vector=[1.0, 0.0, 0.0, 0.0], content="identical"),
        VectorRecord(id="v_orth", vector=[0.0, 1.0, 0.0, 0.0], content="orthogonal"),
        VectorRecord(id="v_opp", vector=[-1.0, 0.0, 0.0, 0.0], content="opposite"),
    ])

    results = await store.search([1.0, 0.0, 0.0, 0.0], limit=10)
    assert len(results) == 3
    # Strict score order: identical > orthogonal > opposite
    assert results[0].id == "v_ident"
    assert results[1].id == "v_orth"
    assert results[2].id == "v_opp"

    assert abs(results[0].score - 1.0) < 1e-4
    assert abs(results[1].score - 0.0) < 1e-4
    assert abs(results[2].score - (-1.0)) < 1e-4

    # 4. Duplicate IDs in batch: last record wins
    await store.upsert_batch([
        VectorRecord(id="dup_test", vector=[0.0, 0.0, 1.0, 0.0], content="initial"),
        VectorRecord(id="dup_test", vector=[0.0, 0.0, 1.0, 0.0], content="overwritten"),
    ])
    dup_item = await store.get("dup_test")
    assert dup_item is not None
    assert dup_item.content == "overwritten"

    # 5. Missing ID behavior
    assert await store.get("missing_id_xyz") is None
    assert await store.delete("missing_id_xyz") is False

    # 6. Zero or negative limit strictly raises ValueError across all adapters
    with pytest.raises(ValueError, match="Limit must be a positive integer"):
        await store.search([1.0, 0.0, 0.0, 0.0], limit=0)
    with pytest.raises(ValueError, match="Limit must be a positive integer"):
        await store.search([1.0, 0.0, 0.0, 0.0], limit=-1)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_name", ["qdrant", "chroma", "pinecone", "lancedb", "in_memory"])
async def test_l2_euclidean_scoring_normalization(adapter_name: str) -> None:
    """
    Verify the public scoring contract for L2 (Euclidean) distance:
    - SearchResult.score is negated Euclidean distance: -||a - b||_2 <= 0.0
    - Higher score indicates closer match (0.0 is identical match)
    """
    server = (
        QdrantMockServer() if adapter_name == "qdrant"
        else ChromaMockServer() if adapter_name == "chroma"
        else PineconeMockServer() if adapter_name == "pinecone"
        else LanceDbMockServer() if adapter_name == "lancedb"
        else None
    )
    handler = server.handle if server else lambda req: None
    store = _make_mock_adapter(adapter_name, handler, collection="l2_test", dimensions=4, metric="l2")
    await store.create_collection("l2_test", dimensions=4, metric="l2")

    await store.upsert_batch([
        VectorRecord(id="l2_exact", vector=[1.0, 0.0, 0.0, 0.0], content="exact"),
        VectorRecord(id="l2_orth", vector=[0.0, 1.0, 0.0, 0.0], content="orth"),
        VectorRecord(id="l2_far", vector=[-1.0, 0.0, 0.0, 0.0], content="far"),
    ])

    results = await store.search([1.0, 0.0, 0.0, 0.0], limit=5)
    assert len(results) == 3

    # Ranking: exact match first, then orthogonal, then far
    assert results[0].id == "l2_exact"
    assert results[1].id == "l2_orth"
    assert results[2].id == "l2_far"

    # All scores must be <= 0.0 (negated Euclidean distance)
    assert results[0].score <= 0.0
    assert results[1].score < results[0].score
    assert results[2].score < results[1].score

    # Exact match distance is 0.0 -> score = 0.0
    assert abs(results[0].score - 0.0) < 1e-4
    # Orthogonal: dist = sqrt((1-0)^2 + (0-1)^2) = sqrt(2) ~ 1.414 -> score ~ -1.414
    assert abs(results[1].score - (-math.sqrt(2.0))) < 1e-3
    # Far: dist = sqrt((1 - (-1))^2) = 2.0 -> score = -2.0
    assert abs(results[2].score - (-2.0)) < 1e-3


@pytest.mark.asyncio
async def test_di_container_shared_http_pool_lifecycle() -> None:
    """Verify that all adapters resolved from DI container share the exact same AIHttpPool."""
    container = Container()
    set_container(container)

    config = Config({
        "ai": {
            "enabled": True,
            "vectorstores": {
                "qdrant": {"base_url": "http://pool-qdrant:6333"},
                "lancedb": {"base_url": "http://pool-lance:8080"},
                "chroma": {"base_url": "http://pool-chroma:8000"},
                "pinecone": {"base_url": "https://pool-pine.io"},
            },
        }
    })
    container.instance(Config, config)

    # Register a single shared AIHttpPool
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    shared_pool = AIHttpPool(client=mock_client)
    container.instance(AIHttpPool, shared_pool)

    qdrant = VectorStore.qdrant()
    chroma = VectorStore.chroma()
    pinecone = VectorStore.pinecone()
    lancedb = VectorStore.lancedb()

    assert isinstance(qdrant, QdrantAdapter)
    assert isinstance(chroma, ChromaAdapter)
    assert isinstance(pinecone, PineconeAdapter)
    assert isinstance(lancedb, LanceDbAdapter)

    # All adapters must share the same pool instance
    assert qdrant._http_pool is shared_pool
    assert chroma._http_pool is shared_pool
    assert pinecone._http_pool is shared_pool
    assert lancedb._http_pool is shared_pool

    # And get_client() resolves to the exact same client
    client1 = qdrant._http_pool.get_client()
    client2 = chroma._http_pool.get_client()
    client3 = pinecone._http_pool.get_client()
    client4 = lancedb._http_pool.get_client()
    assert client1 is client2 is client3 is client4

    # Close shared pool: caller-injected clients stay open for caller ownership
    await shared_pool.aclose()
    assert mock_client.is_closed is False
    assert shared_pool._client is None
    await mock_client.aclose()

    # Pool-owned clients created by AIHttpPool itself are closed on aclose()
    owned_pool = AIHttpPool()
    owned_client = owned_pool.get_client()
    await owned_pool.aclose()
    assert owned_client.is_closed is True


def test_handle_adapter_http_error_codes_and_sanitization() -> None:
    """Verify HTTP error code translations and sensitive credential sanitization."""
    # 1. 400 Bad request
    with pytest.raises(AIProviderError, match="Bad request \\(HTTP 400\\)"):
        handle_adapter_http_error(400, "Invalid schema", "qdrant")

    # 2. 401 Authentication
    with pytest.raises(AIAuthenticationError, match="Authentication failed \\(HTTP 401\\)"):
        handle_adapter_http_error(401, "Bearer secret_api_key_12345678", "qdrant")

    # 3. 403 Forbidden
    with pytest.raises(AIAuthenticationError, match="Authentication failed \\(HTTP 403\\)"):
        handle_adapter_http_error(403, "Access denied", "chroma")

    # 4. 404 Not Found
    with pytest.raises(AIProviderError, match="Resource not found \\(HTTP 404\\)"):
        handle_adapter_http_error(404, "Index not found", "pinecone")

    # 5. 409 Conflict
    with pytest.raises(AIProviderError, match="Conflict error \\(HTTP 409\\)"):
        handle_adapter_http_error(409, "Index already exists", "pinecone")

    # 6. 413 Payload Too Large
    with pytest.raises(AIProviderError, match="Payload too large \\(HTTP 413\\)"):
        handle_adapter_http_error(413, "Batch exceeded limit", "lancedb")

    # 7. 422 Unprocessable Entity
    with pytest.raises(AIProviderError, match="Unprocessable entity \\(HTTP 422\\)"):
        handle_adapter_http_error(422, "Dimension mismatch in vector", "qdrant")

    # 8. 429 Rate Limit
    with pytest.raises(AIRateLimitError, match="Rate limit exceeded \\(HTTP 429\\)"):
        handle_adapter_http_error(429, "Too many requests", "pinecone")

    # 9. 503 Service Unavailable
    with pytest.raises(AIProviderError, match="Service unavailable \\(HTTP 503\\)"):
        handle_adapter_http_error(503, "Cluster is restarting", "pinecone")

    # 10. Redaction of sensitive headers and tokens
    raw_error = "Authorization header Bearer sk-secret-token-12345678 failed with api-key: pcsk_live_9988776655"
    sanitized = sanitize_error_message(raw_error)
    assert "sk-secret-token-12345678" not in sanitized
    assert "pcsk_live_9988776655" not in sanitized
    assert "[REDACTED]" in sanitized
