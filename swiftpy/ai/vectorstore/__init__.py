from __future__ import annotations

from swiftpy.ai.vectorstore.base import (
    ALLOWED_METRICS,
    SearchResult,
    VectorRecord,
    VectorStore,
    VectorStoreCapabilities,
    handle_adapter_http_error,
    normalize_record,
    sanitize_error_message,
    validate_vector,
)
from swiftpy.ai.vectorstore.chroma_adapter import ChromaAdapter
from swiftpy.ai.vectorstore.in_memory_adapter import InMemoryVectorStore
from swiftpy.ai.vectorstore.lancedb_adapter import LanceDbAdapter
from swiftpy.ai.vectorstore.pgvector_adapter import PgVectorAdapter
from swiftpy.ai.vectorstore.pinecone_adapter import PineconeAdapter
from swiftpy.ai.vectorstore.qdrant_adapter import QdrantAdapter

# Register all built-in vector store adapters with the VectorStore facade
VectorStore.register_adapter("in_memory", InMemoryVectorStore)
VectorStore.register_adapter("pgvector", PgVectorAdapter)
VectorStore.register_adapter("postgres", PgVectorAdapter)
VectorStore.register_adapter("qdrant", QdrantAdapter)
VectorStore.register_adapter("lancedb", LanceDbAdapter)
VectorStore.register_adapter("chroma", ChromaAdapter)
VectorStore.register_adapter("pinecone", PineconeAdapter)

__all__ = [
    "ALLOWED_METRICS",
    "ChromaAdapter",
    "InMemoryVectorStore",
    "LanceDbAdapter",
    "PgVectorAdapter",
    "PineconeAdapter",
    "QdrantAdapter",
    "SearchResult",
    "VectorRecord",
    "VectorStore",
    "VectorStoreCapabilities",
    "handle_adapter_http_error",
    "normalize_record",
    "sanitize_error_message",
    "validate_vector",
]
