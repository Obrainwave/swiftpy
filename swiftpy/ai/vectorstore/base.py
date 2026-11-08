from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from swiftpy.ai.exceptions import (
    AIAuthenticationError,
    AIModuleDisabledError,
    AIProviderError,
    AIRateLimitError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.core.config import Config
from swiftpy.core.container import Container, get_container

ALLOWED_METRICS: frozenset[str] = frozenset({"cosine", "l2", "inner_product"})


@dataclass(frozen=True, slots=True)
class VectorStoreCapabilities:
    """Declared capabilities supported by a vector store adapter."""

    metadata_filtering: bool
    nested_metadata_filtering: bool
    namespaces: bool
    pagination: bool
    batch_upsert: bool
    native_distance_metrics: tuple[str, ...]


@dataclass(slots=True)
class SearchResult:
    """
    Represents a vector similarity search result.

    Public Scoring Contract:
    -----------------------
    In SwiftPY, `SearchResult.score` is normalized such that higher values
    consistently indicate greater similarity across all adapters:
    - 'cosine': Cosine similarity in range [-1.0, 1.0], where 1.0 is identical and -1.0 is opposite.
    - 'l2': Negated Euclidean distance (-||a - b||_2) in range (-inf, 0.0], where 0.0 is identical.
    - 'inner_product': Dot product sum (sum(a_i * b_i)).
    """

    id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    content: str = ""
    vector: list[float] | None = None


@dataclass(slots=True)
class VectorRecord:
    """Represents an individual vector record for insertion or update."""

    id: str
    vector: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)
    content: str = ""


def validate_vector(
    vector: Sequence[float],
    dimensions: int | None = None,
    metric: str = "cosine",
) -> list[float]:
    """
    Validate that vector is a non-empty sequence of finite floats matching dimensions.
    For cosine distance, strictly rejects zero-magnitude vectors.
    """
    if not isinstance(vector, (list, tuple)) or not vector:
        raise ValueError("Vector must be a non-empty sequence of floats.")
    if dimensions is not None and len(vector) != dimensions:
        raise ValueError(
            f"Vector dimension {len(vector)} does not match collection dimension {dimensions}"
        )

    parsed: list[float] = []
    norm_sq = 0.0
    for idx, x in enumerate(vector):
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            raise TypeError(
                f"Vector element at index {idx} must be a float, got {type(x).__name__}"
            )
        fx = float(x)
        if math.isnan(fx) or math.isinf(fx):
            raise ValueError(f"Vector element at index {idx} is NaN or Infinite.")
        parsed.append(fx)
        norm_sq += fx * fx

    if metric == "cosine" and norm_sq <= 1e-12:
        raise ValueError("Cannot calculate cosine similarity for a zero-magnitude vector.")

    return parsed


def normalize_record(
    record: VectorRecord | dict[str, Any] | tuple[Any, ...],
    dimensions: int | None = None,
    metric: str = "cosine",
) -> VectorRecord:
    """Normalize and validate a vector record input format."""
    if isinstance(record, VectorRecord):
        rec_id = record.id
        rec_vec = record.vector
        rec_meta = record.metadata
        rec_content = record.content
    elif isinstance(record, dict):
        if "id" not in record or "vector" not in record:
            raise ValueError("Record dict must contain 'id' and 'vector' keys.")
        rec_id = str(record["id"])
        rec_vec = record["vector"]
        rec_meta = dict(record.get("metadata") or {})
        rec_content = str(record.get("content", ""))
    elif isinstance(record, (list, tuple)) and len(record) >= 2:
        rec_id = str(record[0])
        rec_vec = record[1]
        rec_meta = dict(record[2]) if len(record) > 2 and record[2] is not None else {}
        rec_content = str(record[3]) if len(record) > 3 and record[3] is not None else ""
    else:
        raise TypeError(f"Unsupported record format: {type(record).__name__}")

    if not isinstance(rec_id, str) or not rec_id.strip():
        raise ValueError("Record id must be a non-empty string.")

    parsed_vec = validate_vector(rec_vec, dimensions, metric)
    return VectorRecord(
        id=rec_id,
        vector=parsed_vec,
        metadata=rec_meta,
        content=rec_content,
    )


_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(Bearer\s+)[^\s\"',]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"((?:api[-_]?key|x[-_]api[-_]?key)[\"'\s:=]+)[^\s\"',]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(token[\"'\s:=]+)[^\s\"',]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(password[\"'\s:=]+)[^\s\"',]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
    (re.compile(r"pcsk_[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
)


def sanitize_error_message(text: str) -> str:
    """Sanitize sensitive credentials, API keys, and tokens from error messages."""
    if not text:
        return ""
    sanitized = text
    for pattern, repl in _REDACT_PATTERNS:
        sanitized = pattern.sub(repl, sanitized)
    return sanitized


def handle_adapter_http_error(status_code: int, text: str, provider: str) -> None:
    """Translate raw HTTP error status codes into SwiftPY AI exceptions."""
    clean_text = sanitize_error_message(text)
    if status_code in (401, 403):
        raise AIAuthenticationError(
            f"[{provider}] Authentication failed (HTTP {status_code}): {clean_text}"
        )
    if status_code == 429:
        raise AIRateLimitError(
            f"[{provider}] Rate limit exceeded (HTTP 429): {clean_text}"
        )
    if status_code == 400:
        raise AIProviderError(
            f"[{provider}] Bad request (HTTP 400): {clean_text}"
        )
    if status_code == 404:
        raise AIProviderError(
            f"[{provider}] Resource not found (HTTP 404): {clean_text}"
        )
    if status_code == 409:
        raise AIProviderError(
            f"[{provider}] Conflict error (HTTP 409): {clean_text}"
        )
    if status_code == 413:
        raise AIProviderError(
            f"[{provider}] Payload too large (HTTP 413): {clean_text}"
        )
    if status_code == 422:
        raise AIProviderError(
            f"[{provider}] Unprocessable entity (HTTP 422): {clean_text}"
        )
    if status_code == 503:
        raise AIProviderError(
            f"[{provider}] Service unavailable (HTTP 503): {clean_text}"
        )
    if status_code >= 400:
        raise AIProviderError(
            f"[{provider}] API error (HTTP {status_code}): {clean_text}"
        )


_ADAPTER_REGISTRY: dict[str, type[VectorStore]] = {}


class VectorStore(ABC):
    """
    Abstract contract and unified facade for vector database stores.
    """

    @classmethod
    def register_adapter(cls, name: str, adapter_cls: type[VectorStore]) -> None:
        """Register a vector store adapter implementation."""
        _ADAPTER_REGISTRY[name.lower().strip()] = adapter_cls

    @classmethod
    def get_adapter_cls(cls, name: str) -> type[VectorStore] | None:
        """Retrieve a registered adapter class by name."""
        return _ADAPTER_REGISTRY.get(name.lower().strip())

    @classmethod
    def _create_from_di(
        cls,
        adapter_name: str,
        collection: str = "default",
        dimensions: int = 1536,
        metric: str = "cosine",
        **kwargs: Any,
    ) -> VectorStore:
        """
        Instantiate and configure an adapter using the DI container and application configuration.
        """
        try:
            container = get_container()
        except RuntimeError:
            container = Container()

        adapter_cls = cls.get_adapter_cls(adapter_name)
        if adapter_cls is None:
            raise ValueError(
                f"Unknown vector store adapter '{adapter_name}'. "
                f"Available adapters: {sorted(_ADAPTER_REGISTRY.keys())}"
            )

        if container.has(adapter_cls):
            instance = container.resolve(adapter_cls)
            if isinstance(instance, VectorStore):
                return instance

        config = container.resolve(Config) if container.can_resolve(Config) else Config()
        if not config.get("ai.enabled", True):
            raise AIModuleDisabledError("AI module is disabled in configuration (ai.enabled is false).")

        factory: Any = adapter_cls
        if adapter_name == "in_memory":
            return cast(
                VectorStore,
                factory(
                    collection_name=collection,
                    dimensions=dimensions,
                    metric=metric,
                    **kwargs,
                ),
            )

        http_pool: AIHttpPool | None = kwargs.pop("http_pool", None)
        if http_pool is None:
            if container.has(AIHttpPool):
                http_pool = container.resolve(AIHttpPool)
            else:
                http_pool = AIHttpPool.get_instance()

        cfg_section = config.get(f"ai.vectorstores.{adapter_name}", {})
        merged_kwargs: dict[str, Any] = dict(cfg_section) if isinstance(cfg_section, dict) else {}
        merged_kwargs.update(kwargs)
        merged_kwargs["collection_name"] = collection
        merged_kwargs["dimensions"] = dimensions
        merged_kwargs["metric"] = metric
        merged_kwargs["config"] = config
        merged_kwargs["http_pool"] = http_pool

        return cast(VectorStore, factory(**merged_kwargs))

    @classmethod
    def qdrant(
        cls,
        collection: str = "default",
        dimensions: int = 1536,
        metric: str = "cosine",
        **kwargs: Any,
    ) -> VectorStore:
        """Return a configured Qdrant vector store adapter from DI container."""
        return cls._create_from_di("qdrant", collection, dimensions, metric, **kwargs)

    @classmethod
    def lancedb(
        cls,
        collection: str = "default",
        dimensions: int = 1536,
        metric: str = "cosine",
        **kwargs: Any,
    ) -> VectorStore:
        """Return a configured LanceDB vector store adapter from DI container."""
        return cls._create_from_di("lancedb", collection, dimensions, metric, **kwargs)

    @classmethod
    def chroma(
        cls,
        collection: str = "default",
        dimensions: int = 1536,
        metric: str = "cosine",
        **kwargs: Any,
    ) -> VectorStore:
        """Return a configured Chroma vector store adapter from DI container."""
        return cls._create_from_di("chroma", collection, dimensions, metric, **kwargs)

    @classmethod
    def pinecone(
        cls,
        collection: str = "default",
        dimensions: int = 1536,
        metric: str = "cosine",
        **kwargs: Any,
    ) -> VectorStore:
        """Return a configured Pinecone vector store adapter from DI container."""
        return cls._create_from_di("pinecone", collection, dimensions, metric, **kwargs)

    @classmethod
    def in_memory(
        cls,
        collection: str = "default",
        dimensions: int = 1536,
        metric: str = "cosine",
        **kwargs: Any,
    ) -> VectorStore:
        """Return a configured InMemory vector store adapter from DI container."""
        return cls._create_from_di("in_memory", collection, dimensions, metric, **kwargs)

    @classmethod
    def pgvector(
        cls,
        collection: str = "default",
        dimensions: int = 1536,
        metric: str = "cosine",
        **kwargs: Any,
    ) -> VectorStore:
        """Return a configured pgvector vector store adapter from DI container."""
        return cls._create_from_di("pgvector", collection, dimensions, metric, **kwargs)

    @property
    @abstractmethod
    def capabilities(self) -> VectorStoreCapabilities:
        """Return the declared capabilities supported by this adapter."""

    @abstractmethod
    async def create_collection(
        self,
        name: str,
        dimensions: int,
        metric: str = "cosine",
    ) -> None:
        """Create a collection / table configured for vectors of the specified dimension and metric."""

    @abstractmethod
    async def upsert(
        self,
        id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
        content: str = "",
    ) -> None:
        """Insert or update a single vector record."""

    @abstractmethod
    async def upsert_batch(
        self,
        records: Sequence[
            VectorRecord
            | dict[str, Any]
            | tuple[str, list[float], dict[str, Any] | None]
        ],
    ) -> None:
        """Insert or update multiple vector records in batch."""

    @abstractmethod
    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search the collection for vectors nearest to the query vector."""

    @abstractmethod
    async def delete(self, id: str) -> bool:
        """Delete a vector record by id. Return True if deleted, False if not found."""

    @abstractmethod
    async def delete_batch(self, ids: Sequence[str]) -> int:
        """Delete multiple vector records by ids. Return the number of deleted records."""

    @abstractmethod
    async def get(self, id: str) -> SearchResult | None:
        """Retrieve a single vector record by id."""

    @abstractmethod
    async def count(self) -> int:
        """Return the total number of records in the active collection."""

    @abstractmethod
    async def drop_collection(self, name: str) -> None:
        """Drop a collection and all its stored records."""
