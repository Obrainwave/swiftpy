from __future__ import annotations

from swiftpy import env

CONFIG: dict[str, object] = {
    "enabled": env("AI_ENABLED", "true").lower() in ("true", "1", "yes"),
    "default": env("AI_DEFAULT_PROVIDER", "openai"),
    "providers": {
        "openai": {
            "api_key": env("OPENAI_API_KEY", ""),
            "model": env("OPENAI_MODEL", "gpt-4o"),
            "base_url": env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "timeout": float(env("OPENAI_TIMEOUT", "60.0")),
            "max_retries": int(env("OPENAI_MAX_RETRIES", "2")),
        },
        "anthropic": {
            "api_key": env("ANTHROPIC_API_KEY", ""),
            "model": env("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
            "base_url": env("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            "timeout": float(env("ANTHROPIC_TIMEOUT", "60.0")),
            "max_retries": int(env("ANTHROPIC_MAX_RETRIES", "2")),
            "anthropic_version": env("ANTHROPIC_VERSION", "2023-06-01"),
        },
    },
    "http": {
        "max_connections": int(env("AI_HTTP_MAX_CONNECTIONS", "100")),
        "max_keepalive_connections": int(env("AI_HTTP_MAX_KEEPALIVE", "20")),
        "keepalive_expiry": float(env("AI_HTTP_KEEPALIVE_EXPIRY", "30.0")),
        "timeout": float(env("AI_HTTP_TIMEOUT", "60.0")),
    },
    "vectorstores": {
        "default": env("AI_VECTORSTORE_DEFAULT", "in_memory"),
        "qdrant": {
            "base_url": env("QDRANT_BASE_URL", "http://localhost:6333"),
            "api_key": env("QDRANT_API_KEY", ""),
            "timeout": float(env("QDRANT_TIMEOUT", "30.0")),
        },
        "lancedb": {
            "base_url": env("LANCEDB_BASE_URL", "http://localhost:8080"),
            "api_key": env("LANCEDB_API_KEY", ""),
            "timeout": float(env("LANCEDB_TIMEOUT", "30.0")),
        },
        "chroma": {
            "base_url": env("CHROMA_BASE_URL", "http://localhost:8000"),
            "api_key": env("CHROMA_API_KEY", ""),
            "tenant": env("CHROMA_TENANT", "default_tenant"),
            "database": env("CHROMA_DATABASE", "default_database"),
            "timeout": float(env("CHROMA_TIMEOUT", "30.0")),
        },
        "pinecone": {
            "base_url": env("PINECONE_BASE_URL", ""),
            "api_key": env("PINECONE_API_KEY", ""),
            "environment": env("PINECONE_ENVIRONMENT", "us-east-1"),
            "index_name": env("PINECONE_INDEX_NAME", "default"),
            "timeout": float(env("PINECONE_TIMEOUT", "30.0")),
        },
        "pgvector": {
            "timeout": float(env("PGVECTOR_TIMEOUT", "30.0")),
        },
    },
    "rerankers": {
        "cohere": {
            "api_key": env("COHERE_API_KEY", ""),
            "model": env("COHERE_MODEL", "rerank-v3.5"),
            "base_url": env("COHERE_BASE_URL", "https://api.cohere.com/v2/rerank"),
            "timeout": float(env("COHERE_TIMEOUT", "30.0")),
        },
    },
}
