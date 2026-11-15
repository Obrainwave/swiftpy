from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.vectorstore.base import (
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
from swiftpy.core.config import Config
from swiftpy.core.container import get_container
from swiftpy.database.identifiers import (
    MAX_IDENTIFIER_LENGTH,
    quote_identifier,
    validate_identifier,
)
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection

METRIC_CONFIG: dict[str, dict[str, str]] = {
    "cosine": {
        "operator": "<=>",
        "opclass": "vector_cosine_ops",
        "score_sql": "(1.0 - (embedding <=> $1::vector))",
    },
    "l2": {
        "operator": "<->",
        "opclass": "vector_l2_ops",
        "score_sql": "(-(embedding <-> $1::vector))",
    },
    "inner_product": {
        "operator": "<#>",
        "opclass": "vector_ip_ops",
        "score_sql": "(-(embedding <#> $1::vector))",
    },
}


def _format_vector(vector: Sequence[float]) -> str:
    """Format a numeric sequence into a pgvector string representation: '[0.1,0.2,...]'."""
    return f"[{','.join(str(float(x)) for x in vector)}]"


def _bounded_pg_identifier(full_name: str, compact_prefix: str, suffix: str, hash_source: str) -> str:
    """Keep PostgreSQL identifier within MAX_IDENTIFIER_LENGTH (63B) deterministically."""
    if len(full_name.encode("utf-8")) <= MAX_IDENTIFIER_LENGTH:
        validate_identifier(full_name)
        return full_name
    digest = hashlib.sha256(hash_source.encode("utf-8")).hexdigest()[:10]
    safe_prefix = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in compact_prefix)
    head = "swiftpy_v_"
    tail = f"_{digest}{suffix}"
    max_body = MAX_IDENTIFIER_LENGTH - len(head) - len(tail)
    candidate = f"{head}{safe_prefix[:max(0, max_body)]}{tail}"
    validate_identifier(candidate)
    return candidate


class PgVectorAdapter(VectorStore):
    """
    PostgreSQL pgvector adapter for SwiftPY.

    Supports pgvector extension, HNSW indexing across declared distance metrics
    (cosine, l2, inner_product), JSONB metadata containment queries, and seamless
    participation in active database transactions.
    """

    def __init__(
        self,
        collection_name: str = "default",
        dimensions: int = 1536,
        metric: str = "cosine",
        pool: DatabasePool | asyncpg.Pool | None = None,
        config: Config | None = None,
        http_pool: AIHttpPool | None = None,
    ) -> None:
        validate_collection_name(collection_name)
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError(f"Dimensions must be a positive integer, got {dimensions}")
        norm_metric = metric.lower().strip()
        if norm_metric not in METRIC_CONFIG:
            raise ValueError(f"Unsupported metric '{metric}'. Expected one of {sorted(METRIC_CONFIG.keys())}")

        self._collection_name = collection_name
        self._dimensions = dimensions
        self._metric = norm_metric
        self._pool = pool
        self._config = config
        self._http_pool = http_pool
        self._owned_db_pool: DatabasePool | None = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._pool_lock = asyncio.Lock()

    @property
    def metric(self) -> str:
        return self._metric

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        """Return the declared capabilities supported by PgVectorAdapter."""
        return VectorStoreCapabilities(
            metadata_filtering=True,
            nested_metadata_filtering=True,
            namespaces=False,
            pagination=False,
            batch_upsert=True,
            native_distance_metrics=("cosine", "l2", "inner_product"),
        )

    def _get_table_name(self, name: str | None = None) -> str:
        coll = self._collection_name if name is None else validate_collection_name(name)
        table = f"swiftpy_vectors_{coll}"
        return _bounded_pg_identifier(table, coll, "", coll)

    def _get_index_name(self, name: str | None = None, metric: str | None = None) -> str:
        coll = self._collection_name if name is None else validate_collection_name(name)
        active_metric = metric or self._metric
        idx = f"swiftpy_vectors_{coll}_{active_metric}_hnsw_idx"
        return _bounded_pg_identifier(idx, coll, f"_{active_metric}_idx", f"{coll}:{active_metric}")

    async def aclose(self) -> None:
        """Close any internally created DatabasePool owned by this adapter instance."""
        async with self._pool_lock:
            if self._owned_db_pool is not None:
                await self._owned_db_pool.close()
                self._owned_db_pool = None

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        active = get_active_connection()
        if active is not None:
            yield active
            return

        pool: asyncpg.Pool
        if self._pool is not None:
            if isinstance(self._pool, DatabasePool):
                try:
                    pool = self._pool.get_pool()
                except RuntimeError:
                    pool = await self._pool.init()
            else:
                pool = self._pool
        else:
            container = get_container()
            if container.has(DatabasePool):
                db_pool = container.resolve(DatabasePool)
                try:
                    pool = db_pool.get_pool()
                except RuntimeError:
                    pool = await db_pool.init()
            else:
                async with self._pool_lock:
                    if self._owned_db_pool is None:
                        cfg = self._config
                        if cfg is None:
                            if container.has(Config):
                                cfg = container.resolve(Config)
                            else:
                                try:
                                    cfg = Config.get_instance()
                                except RuntimeError:
                                    cfg = Config()
                        self._owned_db_pool = DatabasePool(cfg)
                        pool = await self._owned_db_pool.init()
                    else:
                        try:
                            pool = self._owned_db_pool.get_pool()
                        except RuntimeError:
                            pool = await self._owned_db_pool.init()

        conn = await pool.acquire()
        try:
            yield conn
        finally:
            await pool.release(conn)

    async def create_collection(
        self,
        name: str,
        dimensions: int,
        metric: str = "cosine",
    ) -> None:
        """Create or initialize the collection table with pgvector extension and HNSW index."""
        validate_collection_name(name)
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError(f"Dimensions must be a positive integer, got {dimensions}")
        norm_metric = metric.lower().strip()
        if norm_metric not in METRIC_CONFIG:
            raise ValueError(f"Unsupported metric '{metric}'. Expected one of {sorted(METRIC_CONFIG.keys())}")

        table = self._get_table_name(name)
        quoted_table = quote_identifier(table)
        idx_name = self._get_index_name(name, metric=norm_metric)
        quoted_idx = quote_identifier(idx_name)
        opclass = METRIC_CONFIG[norm_metric]["opclass"]

        async with self._connection() as conn:
            # 1. Ensure pgvector extension exists
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")

            # 2. Check for dimension mismatch on pre-existing collection in active schema
            dim_sql = """
            SELECT pg_attribute.atttypmod
            FROM pg_attribute
            JOIN pg_class ON pg_class.oid = pg_attribute.attrelid
            JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            WHERE pg_class.relname = $1
              AND pg_namespace.nspname = current_schema()
              AND pg_attribute.attname = 'embedding';
            """
            existing_dim = await conn.fetchval(dim_sql, table)
            if existing_dim is not None and existing_dim > 0 and existing_dim != dimensions:
                raise ValueError(
                    f"Collection table '{table}' already exists with {existing_dim} dimensions, "
                    f"cannot reinitialize with {dimensions} dimensions."
                )

            # 3. Create vector storage table
            create_table_sql = f"""
            CREATE TABLE IF NOT EXISTS {quoted_table} (
                id VARCHAR(255) PRIMARY KEY,
                embedding vector({dimensions}) NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
            await conn.execute(create_table_sql)

            # 4. Create HNSW index for the chosen distance metric
            create_index_sql = f"""
            CREATE INDEX IF NOT EXISTS {quoted_idx}
            ON {quoted_table} USING hnsw (embedding {opclass});
            """
            await conn.execute(create_index_sql)

        self._collection_name = name
        self._dimensions = dimensions
        self._metric = norm_metric
        self._initialized = True

    async def _ensure_collection(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            await self.create_collection(self._collection_name, self._dimensions, self._metric)
            self._initialized = True

    async def upsert(
        self,
        id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
        content: str = "",
    ) -> None:
        """Insert or update a single vector record."""
        rec_id = validate_record_id(id)
        parsed_vec = validate_vector(vector, self._dimensions, self._metric)
        await self._ensure_collection()
        table = quote_identifier(self._get_table_name())

        sql = f"""
        INSERT INTO {table} (id, embedding, content, metadata)
        VALUES ($1, $2::vector, $3, $4::jsonb)
        ON CONFLICT (id) DO UPDATE SET
            embedding = EXCLUDED.embedding,
            content = EXCLUDED.content,
            metadata = EXCLUDED.metadata;
        """

        meta_json = json.dumps(metadata or {})
        vec_str = _format_vector(parsed_vec)

        async with self._connection() as conn:
            await conn.execute(sql, rec_id, vec_str, content, meta_json)

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

        formatted_rows: list[tuple[str, str, str, str]] = []
        for rec in records:
            norm_rec = normalize_record(rec, self._dimensions, self._metric)
            formatted_rows.append(
                (
                    norm_rec.id,
                    _format_vector(norm_rec.vector),
                    norm_rec.content,
                    json.dumps(norm_rec.metadata),
                )
            )

        await self._ensure_collection()
        table = quote_identifier(self._get_table_name())

        sql = f"""
        INSERT INTO {table} (id, embedding, content, metadata)
        VALUES ($1, $2::vector, $3, $4::jsonb)
        ON CONFLICT (id) DO UPDATE SET
            embedding = EXCLUDED.embedding,
            content = EXCLUDED.content,
            metadata = EXCLUDED.metadata;
        """

        async with self._connection() as conn:
            # Execute batch within an explicit transaction boundary for atomicity
            if hasattr(conn, "transaction"):
                async with conn.transaction():
                    await conn.executemany(sql, formatted_rows)
            else:
                await conn.executemany(sql, formatted_rows)

    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search nearest vectors using pgvector distance operator and HNSW indexing."""
        if limit <= 0:
            raise ValueError(f"Limit must be a positive integer, got {limit}")

        parsed_vec = validate_vector(vector, self._dimensions, self._metric)
        await self._ensure_collection()

        table = quote_identifier(self._get_table_name())
        vec_str = _format_vector(parsed_vec)
        metric_cfg = METRIC_CONFIG[self._metric]
        op = metric_cfg["operator"]
        score_sql = metric_cfg["score_sql"]

        if filter:
            sql = f"""
            SELECT id, content, metadata, {score_sql} AS score
            FROM {table}
            WHERE metadata @> $2::jsonb
            ORDER BY embedding {op} $1::vector
            LIMIT $3;
            """
            args: tuple[Any, ...] = (vec_str, json.dumps(filter), limit)
        else:
            sql = f"""
            SELECT id, content, metadata, {score_sql} AS score
            FROM {table}
            ORDER BY embedding {op} $1::vector
            LIMIT $2;
            """
            args = (vec_str, limit)

        async with self._connection() as conn:
            rows = await conn.fetch(sql, *args)

        results: list[SearchResult] = []
        for row in rows:
            raw_meta = row["metadata"]
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except (ValueError, TypeError):
                    meta = {}
            elif isinstance(raw_meta, dict):
                meta = dict(raw_meta)
            else:
                meta = {}

            results.append(
                SearchResult(
                    id=row["id"],
                    score=float(row["score"]),
                    metadata=meta,
                    content=row["content"],
                )
            )

        return results

    async def delete(self, id: str) -> bool:
        """Delete a vector record by id."""
        await self._ensure_collection()
        table = quote_identifier(self._get_table_name())
        sql = f"DELETE FROM {table} WHERE id = $1;"

        async with self._connection() as conn:
            result = await conn.execute(sql, id)

        return bool(str(result).strip() != "DELETE 0")

    async def delete_batch(self, ids: Sequence[str]) -> int:
        """Delete multiple deduplicated vector records by ids."""
        unique_ids = dedupe_preserve_order(ids)
        if not unique_ids:
            return 0

        await self._ensure_collection()
        table = quote_identifier(self._get_table_name())
        sql = f"DELETE FROM {table} WHERE id = ANY($1::varchar[]);"

        async with self._connection() as conn:
            result = await conn.execute(sql, unique_ids)

        try:
            return int(str(result).strip().split()[-1])
        except (ValueError, IndexError):
            return 0

    async def get(self, id: str) -> SearchResult | None:
        """Retrieve a single vector record by id."""
        await self._ensure_collection()
        table = quote_identifier(self._get_table_name())
        sql = f"SELECT id, content, metadata, embedding::text AS embedding FROM {table} WHERE id = $1;"

        async with self._connection() as conn:
            row = await conn.fetchrow(sql, id)

        if row is None:
            return None

        raw_meta = row["metadata"]
        if isinstance(raw_meta, str):
            try:
                meta = json.loads(raw_meta)
            except (ValueError, TypeError):
                meta = {}
        elif isinstance(raw_meta, dict):
            meta = dict(raw_meta)
        else:
            meta = {}

        vec: list[float] | None = None
        if row.get("embedding"):
            try:
                raw_s = str(row["embedding"]).strip("[] ")
                vec = [float(x.strip()) for x in raw_s.split(",") if x.strip()]
            except (ValueError, TypeError):
                vec = None

        return SearchResult(
            id=row["id"],
            score=1.0,
            metadata=meta,
            content=row["content"],
            vector=vec,
        )

    async def count(self) -> int:
        """Return the count of vector records in the active collection table."""
        await self._ensure_collection()
        table = quote_identifier(self._get_table_name())
        sql = f"SELECT COUNT(*) FROM {table};"

        async with self._connection() as conn:
            val = await conn.fetchval(sql)

        return int(val) if val is not None else 0

    async def drop_collection(self, name: str) -> None:
        """Drop a collection table and its associated index."""
        table = quote_identifier(self._get_table_name(name))
        sql = f"DROP TABLE IF EXISTS {table} CASCADE;"

        async with self._connection() as conn:
            await conn.execute(sql)

        if name == self._collection_name:
            self._initialized = False
