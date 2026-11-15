from __future__ import annotations

import copy
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from swiftpy.ai.exceptions import (
    AIConnectionError,
    AIModuleDisabledError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.vectorstore.base import (
    ALLOWED_METRICS,
    RESERVED_META_KEYS,
    SearchResult,
    VectorRecord,
    VectorStore,
    VectorStoreCapabilities,
    dedupe_preserve_order,
    encode_path_segment,
    handle_adapter_http_error,
    normalize_record,
    validate_collection_name,
    validate_vector,
)
from swiftpy.core.config import Config

_HTTP_ERRORS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.RequestError, OSError)

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


def _flatten_qdrant_must(flt: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    """Flatten nested metadata filter dicts into Qdrant dot-path must conditions."""
    if not isinstance(flt, dict):
        raise TypeError(f"Filter must be a dict, got {type(flt).__name__}.")
    clauses: list[dict[str, Any]] = []
    for k, v in flt.items():
        if not isinstance(k, str) or not k or "\x00" in k:
            raise ValueError(f"Invalid filter key {k!r}.")
        if k.startswith("$"):
            raise ValueError(f"Qdrant metadata filters do not support Mongo-style operator {k!r}.")
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            if any(isinstance(sub_k, str) and sub_k.startswith("$") for sub_k in v):
                raise ValueError(f"Qdrant metadata filters do not support operator dicts on {full_key!r}.")
            if not v:
                raise ValueError(f"Empty nested filter dict on key {full_key!r} is not supported.")
            clauses.extend(_flatten_qdrant_must(v, prefix=full_key))
        elif v is None:
            clauses.append({"is_null": {"key": full_key}})
        else:
            clauses.append({"key": full_key, "match": {"value": v}})
    return clauses


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
        validate_collection_name(collection_name)
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
            pagination=False,
            batch_upsert=True,
            native_distance_metrics=("cosine", "l2", "inner_product"),
        )

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
        return headers

    def _collection_url(self, name: str | None = None, suffix: str = "") -> str:
        coll = self._collection if name is None else validate_collection_name(name)
        return f"{self._base_url}/collections/{encode_path_segment(coll)}{suffix}"

    async def create_collection(
        self,
        name: str,
        dimensions: int,
        metric: str = "cosine",
    ) -> None:
        validate_collection_name(name)
        norm_metric = metric.lower().strip()
        if norm_metric not in ALLOWED_METRICS:
            raise ValueError(f"Unsupported metric '{metric}'. Expected one of {sorted(ALLOWED_METRICS)}")
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError(f"Dimensions must be positive, got {dimensions}")

        qdrant_metric = _METRIC_MAP[norm_metric]
        url = self._collection_url(name)
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

        self._collection = name
        self._dimensions = dimensions
        self._metric = norm_metric

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
            user_meta = {
                k: copy.deepcopy(v)
                for k, v in r.metadata.items()
                if k not in RESERVED_META_KEYS
            }
            payload = {
                **user_meta,
                "_orig_id": r.id,
                "_content": r.content,
            }
            points.append({
                "id": qid,
                "vector": r.vector,
                "payload": payload,
            })

        url = self._collection_url(suffix="/points?wait=true")
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
        url = self._collection_url(suffix="/points/search")

        body: dict[str, Any] = {
            "vector": parsed_vec,
            "limit": limit,
            "with_payload": True,
            "with_vector": True,
        }

        if filter is not None:
            must_clauses = _flatten_qdrant_must(filter)
            if must_clauses:
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
            meta = {k: v for k, v in payload.items() if k not in RESERVED_META_KEYS}
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
        url = self._collection_url(suffix="/points")
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
            meta = {k: v for k, v in payload.items() if k not in RESERVED_META_KEYS}
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
        url = self._collection_url(suffix="/points/delete?wait=true")
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
        unique_ids = dedupe_preserve_order(ids)
        if not unique_ids:
            return 0

        candidate_qids = [_to_qdrant_id(item_id) for item_id in unique_ids]
        lookup_url = self._collection_url(suffix="/points")
        client = self._http_pool.get_client()
        try:
            lookup_resp = await client.post(
                lookup_url,
                json={"ids": candidate_qids, "with_payload": False, "with_vector": False},
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if lookup_resp.status_code == 404:
                return 0
            if lookup_resp.status_code != 200:
                handle_adapter_http_error(lookup_resp.status_code, lookup_resp.text, "qdrant")
            found_items = lookup_resp.json().get("result", [])
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[qdrant] Connection error: {err}") from err

        found_qids = dedupe_preserve_order(
            [str(item["id"]) for item in found_items if isinstance(item, dict) and item.get("id") is not None]
        )
        if not found_qids:
            return 0

        url = self._collection_url(suffix="/points/delete?wait=true")
        try:
            resp = await client.post(
                url,
                json={"points": found_qids},
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "qdrant")
            return len(found_qids)
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[qdrant] Connection error: {err}") from err

    async def count(self) -> int:
        url = self._collection_url(suffix="/points/count")
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
        url = self._collection_url(name)
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
