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

_PINECONE_METRIC_MAP = {
    "cosine": "cosine",
    "l2": "euclidean",
    "inner_product": "dotproduct",
}


class PineconeAdapter(VectorStore):
    """
    Pinecone vector store adapter via REST API through the shared async HTTP pool.
    """

    def __init__(
        self,
        collection_name: str = "default",
        dimensions: int | None = None,
        metric: str = "cosine",
        base_url: str | None = None,
        api_key: str | None = None,
        environment: str | None = None,
        namespace: str = "",
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
        self._namespace = namespace
        self._timeout = timeout

        cfg_key = self._config.get("ai.vectorstores.pinecone.api_key", "")
        self._api_key = api_key if api_key is not None else str(cfg_key)
        self._environment = environment or str(self._config.get("ai.vectorstores.pinecone.environment", "us-east-1"))

        cfg_base = self._config.get("ai.vectorstores.pinecone.base_url", "")
        raw_base = base_url or str(cfg_base)
        if raw_base:
            self._base_url = raw_base.rstrip("/")
        else:
            self._base_url = f"https://{self._collection}.svc.{self._environment}.pinecone.io"

        self._http_pool = http_pool or AIHttpPool.get_instance()

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        """Return the declared capabilities supported by Pinecone."""
        return VectorStoreCapabilities(
            metadata_filtering=True,
            nested_metadata_filtering=False,
            namespaces=True,
            pagination=True,
            batch_upsert=True,
            native_distance_metrics=("cosine", "l2", "inner_product"),
        )

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Api-Key"] = self._api_key
        return headers

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

        pinecone_metric = _PINECONE_METRIC_MAP[norm_metric]
        url = "https://api.pinecone.io/indexes"
        payload = {
            "name": name,
            "dimension": dimensions,
            "metric": pinecone_metric,
            "spec": {
                "serverless": {
                    "cloud": "aws",
                    "region": self._environment,
                }
            },
        }

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=payload,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 201, 409):
                handle_adapter_http_error(resp.status_code, resp.text, "pinecone")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[pinecone] Connection error: {err}") from err

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

        vectors_payload: list[dict[str, Any]] = []
        for r in norm_records:
            meta = {
                "_content": r.content,
                **r.metadata,
            }
            vectors_payload.append({
                "id": r.id,
                "values": r.vector,
                "metadata": meta,
            })

        url = f"{self._base_url}/vectors/upsert"
        body = {
            "vectors": vectors_payload,
            "namespace": self._namespace,
        }

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "pinecone")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[pinecone] Connection error: {err}") from err

    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if limit <= 0:
            raise ValueError(f"Limit must be a positive integer, got {limit}")

        parsed_vec = validate_vector(vector, self._dimensions, self._metric)
        url = f"{self._base_url}/query"

        body: dict[str, Any] = {
            "vector": parsed_vec,
            "topK": limit,
            "includeMetadata": True,
            "includeValues": True,
            "namespace": self._namespace,
        }

        if filter:
            pine_filter: dict[str, Any] = {}
            for k, v in filter.items():
                pine_filter[k] = {"$eq": v} if not isinstance(v, dict) else v
            body["filter"] = pine_filter

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "pinecone")
            matches = resp.json().get("matches", [])
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[pinecone] Connection error: {err}") from err

        results: list[SearchResult] = []
        for item in matches:
            meta_raw = item.get("metadata") or {}
            content = str(meta_raw.get("_content", ""))
            clean_meta = {k: v for k, v in meta_raw.items() if k != "_content"}
            raw_score = float(item.get("score", 0.0))
            if self._metric == "l2":
                score = -math.sqrt(max(0.0, raw_score))
            else:
                score = raw_score
            values = item.get("values")
            results.append(
                SearchResult(
                    id=str(item.get("id", "")),
                    score=score,
                    metadata=clean_meta,
                    content=content,
                    vector=values,
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    async def get(self, id: str) -> SearchResult | None:
        if not id or not isinstance(id, str):
            return None

        url = f"{self._base_url}/vectors/fetch"
        params = {"ids": id, "namespace": self._namespace}

        client = self._http_pool.get_client()
        try:
            resp = await client.get(
                url,
                params=params,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "pinecone")
            vectors = resp.json().get("vectors", {})
            if id not in vectors:
                return None

            item = vectors[id]
            meta_raw = item.get("metadata") or {}
            content = str(meta_raw.get("_content", ""))
            clean_meta = {k: v for k, v in meta_raw.items() if k != "_content"}
            values = item.get("values")

            return SearchResult(
                id=id,
                score=1.0,
                metadata=clean_meta,
                content=content,
                vector=values,
            )
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[pinecone] Connection error: {err}") from err

    async def delete(self, id: str) -> bool:
        existing = await self.get(id)
        if existing is None:
            return False

        url = f"{self._base_url}/vectors/delete"
        body = {"ids": [id], "namespace": self._namespace}

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "pinecone")
            return True
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[pinecone] Connection error: {err}") from err

    async def delete_batch(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0

        existing_count = 0
        for item_id in ids:
            if await self.get(item_id) is not None:
                existing_count += 1

        if existing_count == 0:
            return 0

        url = f"{self._base_url}/vectors/delete"
        body = {"ids": list(ids), "namespace": self._namespace}

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "pinecone")
            return existing_count
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[pinecone] Connection error: {err}") from err

    async def count(self) -> int:
        url = f"{self._base_url}/describe_index_stats"
        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json={},
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "pinecone")
            stats = resp.json()
            if self._namespace and "namespaces" in stats:
                ns_info = stats["namespaces"].get(self._namespace, {})
                return int(ns_info.get("vectorCount", 0))
            return int(stats.get("totalVectorCount", 0))
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[pinecone] Connection error: {err}") from err

    async def drop_collection(self, name: str) -> None:
        url = f"https://api.pinecone.io/indexes/{name}"
        client = self._http_pool.get_client()
        try:
            resp = await client.delete(
                url,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 202, 404):
                handle_adapter_http_error(resp.status_code, resp.text, "pinecone")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[pinecone] Connection error: {err}") from err
