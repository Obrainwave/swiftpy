from __future__ import annotations

import copy
import ipaddress
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import quote, urlparse

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
ALLOWED_FILTER_OPERATORS: frozenset[str] = frozenset(
    {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$and", "$or", "$exists"}
)
RESERVED_META_KEYS: frozenset[str] = frozenset({"_content", "_orig_id"})

_COLLECTION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PINECONE_ENV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PINECONE_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$")


def validate_collection_name(name: str) -> str:
    """Validate collection/index names to block path traversal, query injection, and host injection."""
    if not isinstance(name, str) or not _COLLECTION_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid collection name {name!r}. "
            "Collection names must start with an alphanumeric character and contain only [A-Za-z0-9_-] (1-128 chars)."
        )
    return name


def validate_record_id(record_id: str) -> str:
    """Validate a record identifier is a non-empty string free of null bytes or ASCII control chars."""
    if not isinstance(record_id, str) or not record_id or not record_id.strip():
        raise ValueError("Record id must be a non-empty string.")
    if "\x00" in record_id or any(ord(ch) < 32 for ch in record_id):
        raise ValueError("Record id must not contain control characters.")
    return record_id


def encode_path_segment(value: str) -> str:
    """Percent-encode a path segment for safe HTTP URL interpolation."""
    return quote(value, safe="")


def dedupe_preserve_order(ids: Sequence[str]) -> list[str]:
    """Deduplicate non-empty string IDs while preserving caller order."""
    return list(dict.fromkeys(ids))


def validate_pinecone_environment(environment: str) -> str:
    """Validate Pinecone environment identifier against strict DNS-label characters."""
    if not isinstance(environment, str) or not _PINECONE_ENV_RE.fullmatch(environment):
        raise ValueError(
            f"Invalid Pinecone environment {environment!r}. Expected alphanumeric/hyphen/underscore token."
        )
    return environment


def validate_pinecone_url(url: str, *, require_pinecone_host: bool = False) -> str:
    """Validate a Pinecone endpoint URL, rejecting credentials, fragments, queries, and SSRF targets."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Pinecone URL must be a non-empty string.")
    trimmed = url.strip().rstrip("/")
    if any(ch in trimmed for ch in ("#", "?", "@")) or any(ch.isspace() for ch in trimmed):
        raise ValueError(f"Invalid Pinecone URL {url!r}: contains disallowed characters.")
    parsed = urlparse(trimmed)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"Invalid Pinecone URL scheme in {url!r}.")
    if require_pinecone_host and parsed.scheme != "https":
        raise ValueError(f"Pinecone data-plane host URL must use https, got {url!r}.")
    if parsed.username or parsed.password or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"Invalid Pinecone URL components in {url!r}.")
    host = parsed.hostname
    if not host or not _PINECONE_HOST_RE.fullmatch(host):
        raise ValueError(f"Invalid Pinecone hostname in {url!r}.")
    # Block link-local / raw metadata IPs
    try:
        ip_obj = ipaddress.ip_address(host)
        if ip_obj.is_link_local:
            raise ValueError(f"Disallowed link-local Pinecone IP host in {url!r}.")
    except ValueError as ip_err:
        if "link-local" in str(ip_err):
            raise
    if require_pinecone_host and not host.endswith(".pinecone.io"):
        raise ValueError(f"Untrusted Pinecone host {host!r}: expected *.pinecone.io suffix.")
    return trimmed


def validate_filter_operators(
    flt: dict[str, Any],
    *,
    allowed_ops: frozenset[str] = ALLOWED_FILTER_OPERATORS,
    nested_allowed: bool = False,
) -> None:
    """Validate metadata filter dictionary keys and dollar-prefixed query operators."""
    if not isinstance(flt, dict):
        raise TypeError(f"Filter must be a dict, got {type(flt).__name__}.")
    for raw_key, value in flt.items():
        if not isinstance(raw_key, str) or not raw_key or "\x00" in raw_key:
            raise ValueError(f"Invalid filter key {raw_key!r}.")
        if raw_key.startswith("$"):
            if raw_key not in allowed_ops:
                raise ValueError(f"Unsupported filter operator {raw_key!r}.")
            if raw_key in ("$and", "$or"):
                if not isinstance(value, Sequence) or isinstance(value, (str, bytes, dict)):
                    raise ValueError(f"Operator {raw_key!r} expects a sequence of filter clauses.")
                for clause in value:
                    validate_filter_operators(clause, allowed_ops=allowed_ops, nested_allowed=nested_allowed)
            elif raw_key in ("$in", "$nin"):
                if not isinstance(value, Sequence) or isinstance(value, (str, bytes, dict)):
                    raise ValueError(f"Operator {raw_key!r} expects a sequence of scalar values.")
            elif raw_key == "$exists" and not isinstance(value, bool):
                raise ValueError("Operator '$exists' expects a boolean.")
            continue

        if isinstance(value, dict):
            if any(isinstance(k, str) and k.startswith("$") for k in value):
                for op_key, op_val in value.items():
                    if not isinstance(op_key, str) or not op_key.startswith("$") or op_key not in allowed_ops:
                        raise ValueError(f"Unsupported filter operator {op_key!r} on field {raw_key!r}.")
                    if op_key in ("$in", "$nin") and (
                        not isinstance(op_val, Sequence) or isinstance(op_val, (str, bytes, dict))
                    ):
                        raise ValueError(f"Operator {op_key!r} expects a sequence of values.")
            elif not nested_allowed:
                raise ValueError(
                    f"Nested dictionary filter on key {raw_key!r} is not supported by this adapter."
                )
            else:
                validate_filter_operators(value, allowed_ops=allowed_ops, nested_allowed=True)


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
    *,
    zero_norm_message: str | None = None,
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
        raise ValueError(
            zero_norm_message or "Cannot calculate cosine similarity for a zero-magnitude vector."
        )

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
        rec_meta = copy.deepcopy(dict(record.metadata)) if record.metadata else {}
        rec_content = record.content
    elif isinstance(record, dict):
        if "id" not in record or "vector" not in record:
            raise ValueError("Record dict must contain 'id' and 'vector' keys.")
        rec_id = str(record["id"])
        rec_vec = record["vector"]
        raw_meta = record.get("metadata")
        rec_meta = copy.deepcopy(dict(raw_meta)) if raw_meta else {}
        rec_content = str(record.get("content", ""))
    elif isinstance(record, (list, tuple)) and len(record) >= 2:
        rec_id = str(record[0])
        rec_vec = record[1]
        rec_meta = copy.deepcopy(dict(record[2])) if len(record) > 2 and record[2] is not None else {}
        rec_content = str(record[3]) if len(record) > 3 and record[3] is not None else ""
    else:
        raise TypeError(f"Unsupported record format: {type(record).__name__}")

    rec_id = validate_record_id(rec_id)

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

        has_explicit_override = (
            collection != "default"
            or dimensions != 1536
            or metric != "cosine"
            or bool(kwargs)
        )
        if container.has(adapter_cls) and not has_explicit_override:
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
