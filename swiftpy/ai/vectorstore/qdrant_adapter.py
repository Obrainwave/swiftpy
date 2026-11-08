from __future__ import annotations

import uuid
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

_METRIC_MAP = {
    "cosine": "Cosine",
    "l2": "Euclid",
    "inner_product": "Dot",
}


def _to_qdrant_id(orig_id: str) -> str:
    """Ensure ID conforms to Qdrant UUID or integer requirement, deterministic for strings."""
    try:
        uuid.UUID(orig_id)
        return orig_id
    except (ValueError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, orig_id))


class QdrantAdapter(VectorStore):
    """
    Qdrant vector store adapter via REST API through the shared async HTTP pool.
    """

    def __init__(
        self,
        collection_name: str = "default",
        dimensions: int | None = None,
        metric: str = "cosine",
        base_url: str | None = None,
        api_key: str | None = None,
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

        cfg_base = self._config.get("ai.vectorstores.qdrant.base_url", "http://localhost:6333")
        self._base_url = (base_url or str(cfg_base)).rstrip("/")
        cfg_key = self._config.get("ai.vectorstores.qdrant.api_key", "")
        self._api_key = api_key if api_key is not None else str(cfg_key)
        self._timeout = timeout

        self._http_pool = http_pool or AIHttpPool.get_instance()

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        """Return the declared capabilities supported by Qdrant."""
        return VectorStoreCapabilities(
            metadata_filtering=True,
            nested_metadata_filtering=True,
            namespaces=False,
            pagination=True,
            batch_upsert=True,
            native_distance_metrics=("cosine", "l2", "inner_product"),
        )

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
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

        qdrant_metric = _METRIC_MAP[norm_metric]
        url = f"{self._base_url}/collections/{name}"
        payload = {
            "vectors": {
                "size": dimensions,
                "distance": qdrant_metric,
            }
        }

        client = self._http_pool.get_client()
        try:
            resp = await client.put(
                url,
                json=payload,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 409):
                handle_adapter_http_error(resp.status_code, resp.text, "qdrant")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[qdrant] Connection error: {err}") from err

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

        points: list[dict[str, Any]] = []
        for r in norm_records:
            qid = _to_qdrant_id(r.id)
            payload = {
                "_orig_id": r.id,
                "_content": r.content,
                **r.metadata,
            }
            points.append({
                "id": qid,
                "vector": r.vector,
                "payload": payload,
            })

        url = f"{self._base_url}/collections/{self._collection}/points?wait=true"
        client = self._http_pool.get_client()
        try:
            resp = await client.put(
                url,
                json={"points": points},
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "qdrant")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[qdrant] Connection error: {err}") from err

    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if limit <= 0:
            raise ValueError(f"Limit must be a positive integer, got {limit}")

        parsed_vec = validate_vector(vector, self._dimensions, self._metric)
        url = f"{self._base_url}/collections/{self._collection}/points/search"

        body: dict[str, Any] = {
            "vector": parsed_vec,
            "limit": limit,
            "with_payload": True,
            "with_vector": True,
        }

        if filter:
            must_clauses: list[dict[str, Any]] = []
            for k, v in filter.items():
                must_clauses.append({"key": k, "match": {"value": v}})
            body["filter"] = {"must": must_clauses}

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "qdrant")
            data = resp.json().get("result", [])
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[qdrant] Connection error: {err}") from err

        results: list[SearchResult] = []
        for item in data:
            payload = item.get("payload") or {}
            orig_id = str(payload.get("_orig_id", item.get("id", "")))
            content = str(payload.get("_content", ""))
            meta = {k: v for k, v in payload.items() if k not in ("_orig_id", "_content")}
            raw_score = float(item.get("score", 0.0))
            score = -abs(raw_score) if self._metric == "l2" else raw_score
            vec = item.get("vector")
            results.append(
                SearchResult(
                    id=orig_id,
                    score=score,
                    metadata=meta,
                    content=content,
                    vector=vec,
                )
            )
        return results

    async def get(self, id: str) -> SearchResult | None:
        if not id or not isinstance(id, str):
            return None

        qid = _to_qdrant_id(id)
        url = f"{self._base_url}/collections/{self._collection}/points"
        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json={"ids": [qid], "with_payload": True, "with_vector": True},
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "qdrant")
            items = resp.json().get("result", [])
            if not items:
                return None
            item = items[0]
            payload = item.get("payload") or {}
            orig_id = str(payload.get("_orig_id", item.get("id", "")))
            content = str(payload.get("_content", ""))
            meta = {k: v for k, v in payload.items() if k not in ("_orig_id", "_content")}
            vec = item.get("vector")
            return SearchResult(
                id=orig_id,
                score=1.0,
                metadata=meta,
                content=content,
                vector=vec,
            )
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[qdrant] Connection error: {err}") from err

    async def delete(self, id: str) -> bool:
        existing = await self.get(id)
        if existing is None:
            return False

        qid = _to_qdrant_id(id)
        url = f"{self._base_url}/collections/{self._collection}/points/delete?wait=true"
        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json={"points": [qid]},
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "qdrant")
            return True
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[qdrant] Connection error: {err}") from err

    async def delete_batch(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0

        # Check existing count to accurately return number of deleted items
        existing_count = 0
        for item_id in ids:
            if await self.get(item_id) is not None:
                existing_count += 1

        if existing_count == 0:
            return 0

        qids = [_to_qdrant_id(i) for i in ids]
        url = f"{self._base_url}/collections/{self._collection}/points/delete?wait=true"
        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json={"points": qids},
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "qdrant")
            return existing_count
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[qdrant] Connection error: {err}") from err

    async def count(self) -> int:
        url = f"{self._base_url}/collections/{self._collection}/points/count"
        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json={"exact": True},
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "qdrant")
            return int(resp.json().get("result", {}).get("count", 0))
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[qdrant] Connection error: {err}") from err

    async def drop_collection(self, name: str) -> None:
        url = f"{self._base_url}/collections/{name}"
        client = self._http_pool.get_client()
        try:
            resp = await client.delete(
                url,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 404):
                handle_adapter_http_error(resp.status_code, resp.text, "qdrant")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[qdrant] Connection error: {err}") from err
