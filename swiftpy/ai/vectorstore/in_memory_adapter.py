from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from typing import Any

from swiftpy.ai.vectorstore.base import (
    SearchResult,
    VectorRecord,
    VectorStore,
    VectorStoreCapabilities,
)

ALLOWED_METRICS: frozenset[str] = frozenset({"cosine", "l2", "inner_product"})


def _matches_jsonb_containment(target: Any, filter_val: Any) -> bool:
    """
    Check if target contains filter_val, mirroring PostgreSQL JSONB @> semantics:
    - If filter_val is a dict, target must be a dict and for every (k, v) in filter_val,
      k must be in target and target[k] must contain v.
    - If filter_val is a list, target must be a list and every element in filter_val
      must match an element in target.
    - Otherwise, scalar equality target == filter_val.
    """
    if filter_val is None:
        return True
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
    return bool(target == filter_val)


def _validate_vector_numeric(vector: Sequence[float], dimensions: int | None = None) -> list[float]:
    """Validate that vector is a non-empty sequence of finite numbers matching dimensions."""
    if not isinstance(vector, (list, tuple)) or not vector:
        raise ValueError("Vector must be a non-empty sequence of floats.")
    if dimensions is not None and len(vector) != dimensions:
        raise ValueError(f"Vector dimension {len(vector)} does not match collection dimension {dimensions}")

    parsed: list[float] = []
    for idx, x in enumerate(vector):
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            raise TypeError(f"Vector element at index {idx} must be a float, got {type(x).__name__}")
        fx = float(x)
        if math.isnan(fx) or math.isinf(fx):
            raise ValueError(f"Vector element at index {idx} is NaN or Infinite.")
        parsed.append(fx)
    return parsed


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
    ) -> None:
        norm_metric = metric.lower().strip()
        if norm_metric not in ALLOWED_METRICS:
            raise ValueError(f"Unsupported metric '{metric}'. Expected one of {sorted(ALLOWED_METRICS)}")

        self._current_collection = collection_name
        self._collections: dict[str, dict[str, dict[str, Any]]] = {}
        self._dimensions: dict[str, int] = {}
        self._metrics: dict[str, str] = {collection_name: norm_metric}
        self._lock = asyncio.Lock()

        if dimensions is not None:
            if dimensions <= 0:
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
            pagination=True,
            batch_upsert=True,
            native_distance_metrics=("cosine", "l2", "inner_product"),
        )

    async def create_collection(
        self,
        name: str,
        dimensions: int,
        metric: str = "cosine",
    ) -> None:
        """Create or initialize a named collection with specified dimensions and metric."""
        if not name or not name.strip():
            raise ValueError("Collection name must be a non-empty string.")
        if dimensions <= 0:
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
        if not isinstance(id, str) or not id.strip():
            raise ValueError("Record id must be a non-empty string.")

        expected_dim = self._dimensions.get(self._current_collection)
        parsed_vec = _validate_vector_numeric(vector, expected_dim)

        active_metric = self._metrics.get(self._current_collection, "cosine")
        if active_metric == "cosine":
            norm = math.sqrt(sum(x * x for x in parsed_vec))
            if norm == 0.0:
                raise ValueError("Zero vector cannot be indexed with cosine distance metric.")

        async with self._lock:
            coll = self._collections.setdefault(self._current_collection, {})
            coll[id] = {
                "id": id,
                "vector": parsed_vec,
                "metadata": dict(metadata) if metadata else {},
                "content": content,
            }

    async def upsert_batch(
        self,
        records: Sequence[
            VectorRecord
            | dict[str, Any]
            | tuple[str, list[float], dict[str, Any] | None]
            | tuple[str, list[float], dict[str, Any] | None, str]
        ],
    ) -> None:
        """Insert or update multiple vector records in batch."""
        for rec in records:
            if isinstance(rec, VectorRecord):
                await self.upsert(
                    id=rec.id,
                    vector=rec.vector,
                    metadata=rec.metadata,
                    content=rec.content,
                )
            elif isinstance(rec, tuple):
                rec_id = rec[0]
                rec_vec = rec[1]
                rec_meta = rec[2] if len(rec) > 2 else None
                rec_content = rec[3] if len(rec) > 3 and isinstance(rec[3], str) else ""
                await self.upsert(
                    id=rec_id,
                    vector=rec_vec,
                    metadata=rec_meta,
                    content=rec_content,
                )
            elif isinstance(rec, dict):
                await self.upsert(
                    id=rec["id"],
                    vector=rec["vector"],
                    metadata=rec.get("metadata"),
                    content=rec.get("content", ""),
                )
            else:
                raise TypeError(f"Unsupported record format: {type(rec).__name__}")

    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search the active collection for nearest vectors using the collection's metric."""
        if limit <= 0:
            raise ValueError(f"Limit must be a positive integer, got {limit}")

        expected_dim = self._dimensions.get(self._current_collection)
        query_vec = _validate_vector_numeric(vector, expected_dim)

        active_metric = self._metrics.get(self._current_collection, "cosine")
        if active_metric == "cosine":
            norm_q = math.sqrt(sum(x * x for x in query_vec))
            if norm_q == 0.0:
                raise ValueError("Zero vector cannot be used as query with cosine distance metric.")
        else:
            norm_q = 1.0

        async with self._lock:
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
                    norm_r = math.sqrt(sum(b * b for b in rec_vec))
                    score = dot / (norm_q * norm_r) if (norm_q > 0 and norm_r > 0) else 0.0
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
                        metadata=dict(meta),
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
        """Delete multiple vector records by ids. Return count of deleted items."""
        deleted_count = 0
        async with self._lock:
            coll = self._collections.get(self._current_collection)
            if coll is not None:
                for target_id in ids:
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
                metadata=dict(data["metadata"]),
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
        async with self._lock:
            self._collections.pop(name, None)
            self._dimensions.pop(name, None)
            self._metrics.pop(name, None)
