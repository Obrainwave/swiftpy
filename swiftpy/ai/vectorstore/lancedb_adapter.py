from __future__ import annotations

import asyncio
import copy
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
    SearchResult,
    VectorRecord,
    VectorStore,
    VectorStoreCapabilities,
    dedupe_preserve_order,
    encode_path_segment,
    handle_adapter_http_error,
    normalize_record,
    validate_collection_name,
    validate_filter_operators,
    validate_vector,
)
from swiftpy.core.config import Config

_HTTP_ERRORS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.RequestError, OSError)

_METRIC_MAP: dict[str, str] = {
    "cosine": "cosine",
    "l2": "l2",
    "inner_product": "dot",
}


class LanceDbAdapter(VectorStore):
    """
    LanceDB vector store adapter via REST API through the shared async HTTP pool.
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

        cfg_base = self._config.get("ai.vectorstores.lancedb.base_url", "http://localhost:8080")
        self._base_url = (base_url or str(cfg_base)).rstrip("/")
        cfg_key = self._config.get("ai.vectorstores.lancedb.api_key", "")
        self._api_key = api_key if api_key is not None else str(cfg_key)
        self._timeout = timeout

        self._http_pool = http_pool or AIHttpPool.get_instance()

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        """Return the declared capabilities supported by LanceDB."""
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
            headers["x-api-key"] = self._api_key
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _table_url(self, name: str | None = None, suffix: str = "") -> str:
        coll = self._collection if name is None else validate_collection_name(name)
        seg = encode_path_segment(coll)
        return f"{self._base_url}/v1/table/{seg}{suffix}"

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

        url = self._table_url(name, "/create")
        payload = {
            "dimensions": dimensions,
            "metric": _METRIC_MAP[norm_metric],
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
                handle_adapter_http_error(resp.status_code, resp.text, "lancedb")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[lancedb] Connection error: {err}") from err

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

        url = self._table_url(suffix="/insert")
        rows: list[dict[str, Any]] = []
        for r in norm_records:
            rows.append({
                "id": r.id,
                "vector": r.vector,
                "content": r.content,
                "metadata": copy.deepcopy(r.metadata),
            })

        body = {
            "data": rows,
            "mode": "merge",
        }

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 201):
                handle_adapter_http_error(resp.status_code, resp.text, "lancedb")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[lancedb] Connection error: {err}") from err

    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if limit <= 0:
            raise ValueError(f"Limit must be a positive integer, got {limit}")

        parsed_vec = validate_vector(vector, self._dimensions, self._metric)
        url = self._table_url(suffix="/query")

        body: dict[str, Any] = {
            "vector": parsed_vec,
            "k": limit,
            "metric": _METRIC_MAP[self._metric],
        }

        if filter is not None:
            validate_filter_operators(filter, nested_allowed=True)
            if filter:
                body["filter"] = filter

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "lancedb")
            rows = resp.json().get("rows", [])
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[lancedb] Connection error: {err}") from err

        results: list[SearchResult] = []
        for row in rows:
            dist = float(row.get("_distance", 0.0))
            if self._metric == "cosine":
                score = 1.0 - dist
            elif self._metric == "l2":
                score = -dist
            else:  # inner_product ("dot")
                score = 1.0 - dist

            results.append(
                SearchResult(
                    id=str(row.get("id", "")),
                    score=score,
                    metadata=dict(row.get("metadata") or {}),
                    content=str(row.get("content", "")),
                    vector=row.get("vector"),
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    async def get(self, id: str) -> SearchResult | None:
        if not id or not isinstance(id, str):
            return None

        url = self._table_url(suffix="/query")
        body = {"filter": {"id": id}, "k": 1}

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
                handle_adapter_http_error(resp.status_code, resp.text, "lancedb")
            rows = resp.json().get("rows", [])
            if not rows:
                return None
            row = rows[0]
            if str(row.get("id")) != id:
                return None

            return SearchResult(
                id=id,
                score=1.0,
                metadata=dict(row.get("metadata") or {}),
                content=str(row.get("content", "")),
                vector=row.get("vector"),
            )
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[lancedb] Connection error: {err}") from err

    async def delete(self, id: str) -> bool:
        existing = await self.get(id)
        if existing is None:
            return False

        url = self._table_url(suffix="/delete")
        body = {"ids": [id]}

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 204):
                handle_adapter_http_error(resp.status_code, resp.text, "lancedb")
            return True
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[lancedb] Connection error: {err}") from err

    async def delete_batch(self, ids: Sequence[str]) -> int:
        unique_ids = dedupe_preserve_order(ids)
        if not unique_ids:
            return 0

        semaphore = asyncio.Semaphore(8)

        async def _exists(item_id: str) -> bool:
            async with semaphore:
                return await self.get(item_id) is not None

        exists_flags = await asyncio.gather(*(_exists(item_id) for item_id in unique_ids))
        existing_ids = [item_id for item_id, found in zip(unique_ids, exists_flags, strict=True) if found]
        if not existing_ids:
            return 0

        url = self._table_url(suffix="/delete")
        body = {"ids": existing_ids}

        client = self._http_pool.get_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 204):
                handle_adapter_http_error(resp.status_code, resp.text, "lancedb")
            return len(existing_ids)
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[lancedb] Connection error: {err}") from err

    async def count(self) -> int:
        url = self._table_url(suffix="/count")
        client = self._http_pool.get_client()
        try:
            resp = await client.get(
                url,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                handle_adapter_http_error(resp.status_code, resp.text, "lancedb")
            return int(resp.json().get("count", 0))
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[lancedb] Connection error: {err}") from err

    async def drop_collection(self, name: str) -> None:
        url = self._table_url(name)
        client = self._http_pool.get_client()
        try:
            resp = await client.delete(
                url,
                headers=self._get_headers(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 204, 404):
                handle_adapter_http_error(resp.status_code, resp.text, "lancedb")
        except _HTTP_ERRORS as err:
            raise AIConnectionError(f"[lancedb] Connection error: {err}") from err
