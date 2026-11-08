from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

try:
    import httpx
    _HTTP_ERRORS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.RequestError, OSError)
except ImportError:
    httpx = None
    _HTTP_ERRORS = (TimeoutError, OSError)

from swiftpy.ai.exceptions import (
    AIConnectionError,
    AIModuleDisabledError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.vectorstore.base import (
    ALLOWED_METRICS,
    SearchResult,
    VectorRecord,
    VectorStore,
    VectorStoreCapabilities,
    handle_adapter_http_error,
    normalize_record,
    validate_vector,
)
from swiftpy.core.config import Config

_CHROMA_METRIC_MAP = {
    "cosine": "cosine",
    "l2": "l2",
    "inner_product": "ip",
}


class ChromaAdapter(VectorStore):
    """
    Chroma vector store adapter via HTTP REST API through the shared async HTTP pool.
    """

    def __init__(
        self,
        collection_name: str = "default",
        dimensions: int | None = None,
        metric: str = "cosine",
        base_url: str | None = None,
        api_key: str | None = None,
        tenant: str | None = None,
        database: str | None = None,
        timeout: float = 30.0,
        config: Config | None = None,
        http_pool: AIHttpPool | None = None,
        **kwargs: Any,
    ) -> None:
        self._config = config or Config()
        if not self._config.get("ai.enabled", True):
            raise AIModuleDisabledError("AI module is disabled in configuration (ai.enabled is false).")

        norm_metric = metric.lower().strip()
        if norm_metric not in ALLOWED_METRICS:
            raise ValueError(f"Unsupported metric '{metric}'. Expected one of {sorted(ALLOWED_METRICS)}")

        self._collection = collection_name
        self._dimensions = dimensions
        self._metric = norm_metric

        cfg_base = self._config.get("ai.vectorstores.chroma.base_url", "http://localhost:8000")
        raw_base = (base_url or str(cfg_base)).rstrip("/")
        if not raw_base.endswith("/api/v1"):
            self._base_url = f"{raw_base}/api/v1"
        else:
            self._base_url = raw_base

        cfg_key = self._config.get("ai.vectorstores.chroma.api_key", "")
        self._api_key = api_key if api_key is not None else str(cfg_key)
        self._tenant = tenant or str(self._config.get("ai.vectorstores.chroma.tenant", "default_tenant"))
        self._database = database or str(self._config.get("ai.vectorstores.chroma.database", "default_database"))
        self._timeout = timeout

        self._http_pool = http_pool or AIHttpPool.get_instance()
        self._collection_id: str | None = None

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        """Return the declared capabilities supported by Chroma."""
        return VectorStoreCapabilities(
            metadata_filtering=True,
            nested_metadata_filtering=False,
            namespaces=False,
            pagination=True,
            batch_upsert=True,
            native_distance_metrics=("cosine", "l2", "inner_product"),
        )

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-Chroma-Token"] = self._api_key
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _get_or_create_collection_id(self, name: str | None = None) -> str:
        coll_name = name or self._collection
        if self._collection_id and not name:
            return self._collection_id

        client = self._http_pool.get_client()
        url = f"{self._base_url}/collections/{coll_name}"
        params = {"tenant": self._tenant, "database": self._database}

        try:
            resp = await client.get(url, params=params, headers=self._get_headers(), timeout=self._timeout)
            if resp.status_code == 200:
                coll_id = str(resp.json().get("id"))
                if not name or name == self._collection:
                    self._collection_id = coll_id
                return coll_id
            if resp.status_code == 404:
                # Create collection
                create_url = f"{self._base_url}/collections"
                chroma_metric = _CHROMA_METRIC_MAP.get(self._metric, "cosine")
                create_body = {
                    "name": coll_name,
                    "metadata": {"hnsw:space": chroma_metric},
                }
                c_resp = await client.post(
                    create_url,
                    json=create_body,
                    params=params,
                    headers=self._get_headers(),
                    timeout=self._timeout,
                )
                if c_resp.status_code in (200, 201):
                    coll_id = str(c_resp.json().get("id"))
                    if not name or name == self._collection:
                        self._collection_id = coll_id
                    return coll_id
                handle_adapter_http_error(c_resp.status_code, c_resp.text, "chroma")
            else:
                handle_adapter_http_error(resp.status_code, resp.text, "chroma")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[chroma] Connection error: {err}") from err

        raise RuntimeError(f"[chroma] Failed to resolve collection '{coll_name}'.")

    async def create_collection(
        self,
        name: str,
        dimensions: int,
        metric: str = "cosine",
    ) -> None:
        norm_metric = metric.lower().strip()
        if norm_metric not in ALLOWED_METRICS:
            raise ValueError(f"Unsupported metric '{metric}'. Expected one of {sorted(ALLOWED_METRICS)}")
        if dimensions <= 0:
            raise ValueError(f"Dimensions must be positive, got {dimensions}")

        self._collection = name
        self._dimensions = dimensions
        self._metric = norm_metric
        self._collection_id = None
        await self._get_or_create_collection_id(name)

    async def upsert(
        self,
        id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
        content: str = "",
    ) -> None:
        await self.upsert_batch([
            VectorRecord(id=id, vector=vector, metadata=metadata or {}, content=content)
        ])

    async def upsert_batch(
        self,
        records: Sequence[
            VectorRecord
            | dict[str, Any]
            | tuple[str, list[float], dict[str, Any] | None]
        ],
    ) -> None:
        if not records:
            return

        norm_records = [
            normalize_record(r, self._dimensions, self._metric) for r in records
        ]

        coll_id = await self._get_or_create_collection_id()
        url = f"{self._base_url}/collections/{coll_id}/upsert"

        payload = {
            "ids": [r.id for r in norm_records],
            "embeddings": [r.vector for r in norm_records],
            "metadatas": [r.metadata for r in norm_records],
            "documents": [r.content for r in norm_records],
        }

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=payload,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 201):
                handle_adapter_http_error(resp.status_code, resp.text, "chroma")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[chroma] Connection error: {err}") from err

    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if limit <= 0:
            raise ValueError(f"Limit must be a positive integer, got {limit}")

        parsed_vec = validate_vector(vector, self._dimensions, self._metric)
        coll_id = await self._get_or_create_collection_id()
        url = f"{self._base_url}/collections/{coll_id}/query"

        body: dict[str, Any] = {
            "query_embeddings": [parsed_vec],
            "n_results": limit,
            "include": ["metadatas", "documents", "distances", "embeddings"],
        }

        if filter:
            items = list(filter.items())
            if len(items) == 1:
                body["where"] = {items[0][0]: items[0][1]}
            elif len(items) > 1:
                body["where"] = {"$and": [{k: v} for k, v in items]}

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "chroma")
            data = resp.json()
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[chroma] Connection error: {err}") from err

        results: list[SearchResult] = []
        ids_batch = data.get("ids", [[]])[0] if data.get("ids") else []
        distances_batch = data.get("distances", [[]])[0] if data.get("distances") else []
        metas_batch = data.get("metadatas", [[]])[0] if data.get("metadatas") else []
        docs_batch = data.get("documents", [[]])[0] if data.get("documents") else []
        vecs_batch = data.get("embeddings", [[]])[0] if data.get("embeddings") else []

        for idx, rec_id in enumerate(ids_batch):
            dist = float(distances_batch[idx]) if idx < len(distances_batch) else 0.0
            meta = metas_batch[idx] if idx < len(metas_batch) and metas_batch[idx] else {}
            doc = docs_batch[idx] if idx < len(docs_batch) and docs_batch[idx] else ""
            vec = vecs_batch[idx] if idx < len(vecs_batch) else None

            # Calculate normalized similarity score
            if self._metric == "cosine":
                score = 1.0 - dist
            elif self._metric == "l2":
                score = -math.sqrt(max(0.0, dist))
            else:  # inner_product
                score = 1.0 - dist

            results.append(
                SearchResult(
                    id=str(rec_id),
                    score=score,
                    metadata=meta,
                    content=doc,
                    vector=vec,
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    async def get(self, id: str) -> SearchResult | None:
        if not id or not isinstance(id, str):
            return None

        coll_id = await self._get_or_create_collection_id()
        url = f"{self._base_url}/collections/{coll_id}/get"
        body = {"ids": [id], "include": ["metadatas", "documents", "embeddings"]}

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "chroma")
            data = resp.json()
            ids = data.get("ids", [])
            if not ids or ids[0] != id:
                return None

            meta = (data.get("metadatas") or [{}])[0] or {}
            doc = (data.get("documents") or [""])[0] or ""
            vec = (data.get("embeddings") or [None])[0]

            return SearchResult(
                id=id,
                score=1.0,
                metadata=meta,
                content=doc,
                vector=vec,
            )
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[chroma] Connection error: {err}") from err

    async def delete(self, id: str) -> bool:
        existing = await self.get(id)
        if existing is None:
            return False

        coll_id = await self._get_or_create_collection_id()
        url = f"{self._base_url}/collections/{coll_id}/delete"
        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json={"ids": [id]},
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 204):
                handle_adapter_http_error(resp.status_code, resp.text, "chroma")
            return True
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[chroma] Connection error: {err}") from err

    async def delete_batch(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0

        existing_count = 0
        for item_id in ids:
            if await self.get(item_id) is not None:
                existing_count += 1

        if existing_count == 0:
            return 0

        coll_id = await self._get_or_create_collection_id()
        url = f"{self._base_url}/collections/{coll_id}/delete"
        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json={"ids": list(ids)},
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 204):
                handle_adapter_http_error(resp.status_code, resp.text, "chroma")
            return existing_count
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[chroma] Connection error: {err}") from err

    async def count(self) -> int:
        coll_id = await self._get_or_create_collection_id()
        url = f"{self._base_url}/collections/{coll_id}/count"
        client = self._http_pool.get_client()
        try:
            resp = await client.get(
                url,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "chroma")
            return int(resp.json())
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[chroma] Connection error: {err}") from err

    async def drop_collection(self, name: str) -> None:
        url = f"{self._base_url}/collections/{name}"
        params = {"tenant": self._tenant, "database": self._database}
        client = self._http_pool.get_client()
        try:
            resp = await client.delete(
                url,
                params=params,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 204, 404):
                handle_adapter_http_error(resp.status_code, resp.text, "chroma")
            if name == self._collection:
                self._collection_id = None
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[chroma] Connection error: {err}") from err
