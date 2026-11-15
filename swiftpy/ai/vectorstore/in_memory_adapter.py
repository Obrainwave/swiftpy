from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Sequence
from typing import Any

from swiftpy.ai.vectorstore.base import (
    ALLOWED_METRICS,
    SearchResult,
    VectorRecord,
    VectorStore,
    VectorStoreCapabilities,
    dedupe_preserve_order,
    normalize_record,
    validate_collection_name,
    validate_record_id,
    validate_vector,
)


def _matches_jsonb_containment(target: Any, filter_val: Any) -> bool:
    """
    Check if target contains filter_val, mirroring PostgreSQL JSONB @> semantics:
    - If filter_val is a dict, target must be a dict and for every (k, v) in filter_val,
      k must be in target and target[k] must contain v.
    - If filter_val is a list, target must be a list and every element in filter_val
      must match an element in target.
    - Otherwise, scalar equality target == filter_val (including None without bool/int confusion).
    """
    if filter_val is None:
        return target is None
    if isinstance(filter_val, dict):
        if not isinstance(target, dict):
            return False
        for k, v in filter_val.items():
            if k not in target:
                return False
            if not _matches_jsonb_containment(target[k], v):
                return False
        return True
    if isinstance(filter_val, list):
        if not isinstance(target, list):
            return False
        for item in filter_val:
            if not any(_matches_jsonb_containment(target_elem, item) for target_elem in target):
                return False
        return True
    if isinstance(target, bool) != isinstance(filter_val, bool):
        return False
    return bool(target == filter_val)


class InMemoryVectorStore(VectorStore):
    """
    Pure Python, in-memory vector store adapter.

    Requires zero external dependencies. Supports JSONB-style containment metadata
    filtering, multiple distance metrics (cosine, l2, inner_product), and strictly
    validates finite numerical representations.
    """

    def __init__(
        self,
        collection_name: str = "default",
        dimensions: int | None = None,
        metric: str = "cosine",
        *,
        max_records: int | None = 100_000,
    ) -> None:
        validate_collection_name(collection_name)
        norm_metric = metric.lower().strip()
        if norm_metric not in ALLOWED_METRICS:
            raise ValueError(f"Unsupported metric '{metric}'. Expected one of {sorted(ALLOWED_METRICS)}")
        if max_records is not None and (isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0):
            raise ValueError(f"max_records must be a positive integer or None, got {max_records!r}")

        self._current_collection = collection_name
        self._collections: dict[str, dict[str, dict[str, Any]]] = {}
        self._dimensions: dict[str, int] = {}
        self._metrics: dict[str, str] = {collection_name: norm_metric}
        self._max_records = max_records
        self._lock = asyncio.Lock()

        if dimensions is not None:
            if isinstance(dimensions, bool) or dimensions <= 0:
                raise ValueError(f"Dimensions must be a positive integer, got {dimensions}")
            self._dimensions[collection_name] = dimensions
            self._collections[collection_name] = {}

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        """Return the declared capabilities supported by InMemoryVectorStore."""
        return VectorStoreCapabilities(
            metadata_filtering=True,
            nested_metadata_filtering=True,
            namespaces=False,
            pagination=False,
            batch_upsert=True,
            native_distance_metrics=("cosine", "l2", "inner_product"),
        )

    def _put_record_locked(
        self,
        coll: dict[str, dict[str, Any]],
        record: VectorRecord,
    ) -> None:
        # Refresh insertion order on overwrite so FIFO/LRU eviction behaves predictably.
        coll.pop(record.id, None)
        coll[record.id] = {
            "id": record.id,
            "vector": list(record.vector),
            "metadata": copy.deepcopy(record.metadata),
            "content": record.content,
        }
        if self._max_records is not None:
            while len(coll) > self._max_records:
                oldest_key = next(iter(coll))
                del coll[oldest_key]

    async def create_collection(
        self,
        name: str,
        dimensions: int,
        metric: str = "cosine",
    ) -> None:
        """Create or initialize a named collection with specified dimensions and metric."""
        validate_collection_name(name)
        if isinstance(dimensions, bool) or dimensions <= 0:
            raise ValueError(f"Dimensions must be a positive integer, got {dimensions}")
        norm_metric = metric.lower().strip()
        if norm_metric not in ALLOWED_METRICS:
            raise ValueError(f"Unsupported metric '{metric}'. Expected one of {sorted(ALLOWED_METRICS)}")

        async with self._lock:
            # Check for existing collection dimension mismatch
            if name in self._dimensions and self._dimensions[name] != dimensions:
                raise ValueError(
                    f"Collection '{name}' already exists with {self._dimensions[name]} dimensions, "
                    f"cannot reinitialize with {dimensions} dimensions."
                )

            self._current_collection = name
            self._dimensions[name] = dimensions
            self._metrics[name] = norm_metric
            if name not in self._collections:
                self._collections[name] = {}

    async def upsert(
        self,
        id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
        content: str = "",
    ) -> None:
        """Insert or update a single vector record."""
        rec_id = validate_record_id(id)
        async with self._lock:
            expected_dim = self._dimensions.get(self._current_collection)
            active_metric = self._metrics.get(self._current_collection, "cosine")
            parsed_vec = validate_vector(
                vector,
                expected_dim,
                active_metric,
                zero_norm_message="Zero vector cannot be indexed with cosine distance metric.",
            )
            if expected_dim is None:
                self._dimensions[self._current_collection] = len(parsed_vec)
            coll = self._collections.setdefault(self._current_collection, {})
            norm_rec = VectorRecord(
                id=rec_id,
                vector=parsed_vec,
                metadata=copy.deepcopy(dict(metadata)) if metadata else {},
                content=content,
            )
            self._put_record_locked(coll, norm_rec)

    async def upsert_batch(
        self,
        records: Sequence[
            VectorRecord
            | dict[str, Any]
            | tuple[str, list[float], dict[str, Any] | None]
            | tuple[str, list[float], dict[str, Any] | None, str]
        ],
    ) -> None:
        """Insert or update multiple vector records atomically in batch."""
        if not records:
            return
        async with self._lock:
            expected_dim = self._dimensions.get(self._current_collection)
            active_metric = self._metrics.get(self._current_collection, "cosine")
            normalized: list[VectorRecord] = []
            batch_dim = expected_dim
            for rec in records:
                parsed = normalize_record(rec, batch_dim, active_metric)
                if batch_dim is None:
                    batch_dim = len(parsed.vector)
                normalized.append(parsed)

            if expected_dim is None and batch_dim is not None:
                self._dimensions[self._current_collection] = batch_dim

            coll = self._collections.setdefault(self._current_collection, {})
            for item in normalized:
                self._put_record_locked(coll, item)

    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search the active collection for nearest vectors using the collection's metric."""
        if limit <= 0:
            raise ValueError(f"Limit must be a positive integer, got {limit}")

        async with self._lock:
            expected_dim = self._dimensions.get(self._current_collection)
            active_metric = self._metrics.get(self._current_collection, "cosine")
            query_vec = validate_vector(
                vector,
                expected_dim,
                active_metric,
                zero_norm_message="Zero vector cannot be used as query with cosine distance metric.",
            )
            norm_q_sq = sum(x * x for x in query_vec)
            norm_q = math.sqrt(norm_q_sq) if active_metric == "cosine" else 1.0

            coll = self._collections.get(self._current_collection, {})
            if not coll:
                return []

            candidates: list[SearchResult] = []
            for record in coll.values():
                meta = record["metadata"]
                if filter is not None and not _matches_jsonb_containment(meta, filter):
                    continue

                rec_vec: list[float] = record["vector"]
                if active_metric == "cosine":
                    dot = sum(a * b for a, b in zip(query_vec, rec_vec, strict=True))
                    norm_r_sq = sum(b * b for b in rec_vec)
                    if norm_q_sq > 1e-12 and norm_r_sq > 1e-12:
                        score = dot / (norm_q * math.sqrt(norm_r_sq))
                    else:
                        score = 0.0
                    score = max(-1.0, min(1.0, score))
                elif active_metric == "l2":
                    dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(query_vec, rec_vec, strict=True)))
                    # Higher score = closer match (negative Euclidean distance)
                    score = -dist
                elif active_metric == "inner_product":
                    score = sum(a * b for a, b in zip(query_vec, rec_vec, strict=True))
                else:
                    score = 0.0

                candidates.append(
                    SearchResult(
                        id=record["id"],
                        score=score,
                        metadata=copy.deepcopy(meta),
                        content=record["content"],
                        vector=list(rec_vec),
                    )
                )

        # Sort descending by score (highest similarity / closest distance first)
        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates[:limit]

    async def delete(self, id: str) -> bool:
        """Delete a vector record by id."""
        async with self._lock:
            coll = self._collections.get(self._current_collection)
            if coll is not None and id in coll:
                del coll[id]
                return True
            return False

    async def delete_batch(self, ids: Sequence[str]) -> int:
        """Delete multiple deduplicated vector records by ids. Return count of deleted items."""
        deleted_count = 0
        async with self._lock:
            coll = self._collections.get(self._current_collection)
            if coll is not None:
                for target_id in dedupe_preserve_order(ids):
                    if target_id in coll:
                        del coll[target_id]
                        deleted_count += 1
        return deleted_count

    async def get(self, id: str) -> SearchResult | None:
        """Retrieve a single vector record by id."""
        async with self._lock:
            coll = self._collections.get(self._current_collection)
            if coll is None or id not in coll:
                return None
            data = coll[id]
            return SearchResult(
                id=data["id"],
                score=1.0,
                metadata=copy.deepcopy(data["metadata"]),
                content=data["content"],
                vector=list(data["vector"]),
            )

    async def count(self) -> int:
        """Return the count of vector records in the active collection."""
        async with self._lock:
            coll = self._collections.get(self._current_collection)
            return len(coll) if coll else 0

    async def drop_collection(self, name: str) -> None:
        """Drop a collection and remove its records."""
        validate_collection_name(name)
        async with self._lock:
            self._collections.pop(name, None)
            self._dimensions.pop(name, None)
            self._metrics.pop(name, None)
