from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from swiftpy.ai.vectorstore.base import SearchResult, VectorRecord
from swiftpy.ai.vectorstore.in_memory_adapter import InMemoryVectorStore


@dataclass(frozen=True, slots=True)
class VectorStoreCall:
    """Recorded invocation made against a VectorStoreFake."""

    method: str
    collection: str
    args: dict[str, Any] = field(default_factory=dict)


class VectorStoreFake(InMemoryVectorStore):
    """
    In-memory vector store test fake that records operations for assertions.
    """

    def __init__(
        self,
        collection_name: str = "default",
        dimensions: int | None = None,
        metric: str = "cosine",
        *,
        max_records: int | None = 100_000,
    ) -> None:
        super().__init__(
            collection_name=collection_name,
            dimensions=dimensions,
            metric=metric,
            max_records=max_records,
        )
        self.calls: list[VectorStoreCall] = []

    def reset(self) -> None:
        """Clear all stored vectors and recorded calls."""
        self.calls.clear()
        self._collections.clear()

    def recorded(self, method: str | None = None) -> list[VectorStoreCall]:
        """Return recorded invocations, optionally filtered by method name."""
        if method is None:
            return list(self.calls)
        return [call for call in self.calls if call.method == method]

    def assert_called(self, method: str, *, times: int | None = None) -> None:
        """Assert that `method` was invoked (and optionally how many times)."""
        matched = self.recorded(method)
        if not matched and (times is None or times > 0):
            raise AssertionError(f"Expected VectorStore method '{method}' to be called.")
        if times is not None and len(matched) != times:
            raise AssertionError(
                f"Expected VectorStore method '{method}' to be called {times} time(s), got {len(matched)}."
            )

    def assert_upserted(self, record_id: str | None = None) -> None:
        """Assert that an upsert/upsert_batch occurred (optionally for a specific id)."""
        upserts = [
            call for call in self.calls if call.method in ("upsert", "upsert_batch")
        ]
        if not upserts:
            raise AssertionError("Expected at least one vectorstore upsert call.")
        if record_id is not None:
            found = any(
                call.args.get("id") == record_id
                or record_id in call.args.get("ids", ())
                for call in upserts
            )
            if not found:
                raise AssertionError(
                    f"Expected record id '{record_id}' to be upserted, got {upserts!r}."
                )

    def assert_searched(self, *, times: int | None = None) -> None:
        """Assert that search() was invoked."""
        self.assert_called("search", times=times)

    async def create_collection(
        self,
        name: str,
        dimensions: int,
        metric: str = "cosine",
    ) -> None:
        await super().create_collection(name, dimensions, metric=metric)
        self.calls.append(
            VectorStoreCall(
                method="create_collection",
                collection=name,
                args={"name": name, "dimensions": dimensions, "metric": metric},
            )
        )

    async def upsert(
        self,
        id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
        content: str = "",
    ) -> None:
        await super().upsert(id, vector, metadata=metadata, content=content)
        self.calls.append(
            VectorStoreCall(
                method="upsert",
                collection=self._current_collection,
                args={"id": id, "dimensions": len(vector), "metadata": metadata},
            )
        )

    async def upsert_batch(
        self,
        records: Sequence[
            VectorRecord
            | dict[str, Any]
            | tuple[str, list[float], dict[str, Any] | None]
            | tuple[str, list[float], dict[str, Any] | None, str]
        ],
    ) -> None:
        ids: list[str] = []
        for rec in records:
            if isinstance(rec, VectorRecord):
                ids.append(rec.id)
            elif isinstance(rec, dict):
                ids.append(str(rec.get("id", "")))
            elif isinstance(rec, tuple) and rec:
                ids.append(str(rec[0]))
        await super().upsert_batch(records)
        self.calls.append(
            VectorStoreCall(
                method="upsert_batch",
                collection=self._current_collection,
                args={"count": len(records), "ids": tuple(ids)},
            )
        )

    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        results = await super().search(vector, limit=limit, filter=filter)
        self.calls.append(
            VectorStoreCall(
                method="search",
                collection=self._current_collection,
                args={
                    "limit": limit,
                    "filter": filter,
                    "result_count": len(results),
                },
            )
        )
        return results

    async def delete(self, id: str) -> bool:
        deleted = await super().delete(id)
        self.calls.append(
            VectorStoreCall(
                method="delete",
                collection=self._current_collection,
                args={"id": id, "deleted": deleted},
            )
        )
        return deleted

    async def delete_batch(self, ids: Sequence[str]) -> int:
        count = await super().delete_batch(ids)
        self.calls.append(
            VectorStoreCall(
                method="delete_batch",
                collection=self._current_collection,
                args={"ids": tuple(ids), "deleted_count": count},
            )
        )
        return count

    async def drop_collection(self, name: str) -> None:
        await super().drop_collection(name)
        self.calls.append(
            VectorStoreCall(
                method="drop_collection",
                collection=name,
                args={"name": name},
            )
        )


__all__ = ["VectorStoreCall", "VectorStoreFake"]
