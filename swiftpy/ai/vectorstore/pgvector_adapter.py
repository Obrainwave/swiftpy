from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from swiftpy.ai.vectorstore.base import (
    SearchResult,
    VectorRecord,
    VectorStore,
    VectorStoreCapabilities,
)
from swiftpy.core.config import Config
from swiftpy.core.container import get_container
from swiftpy.database.identifiers import quote_identifier, validate_identifier
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


def _validate_vector(vector: Sequence[float], dimensions: int, metric: str) -> list[float]:
    """Validate vector elements for finite values, dimensional correctness, and non-zero cosine norm."""
    if not isinstance(vector, (list, tuple)) or not vector:
        raise ValueError("Vector must be a non-empty sequence of floats.")
    if len(vector) != dimensions:
        raise ValueError(f"Vector dimension {len(vector)} does not match collection dimension {dimensions}")

    parsed: list[float] = []
    for idx, x in enumerate(vector):
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            raise TypeError(f"Vector element at index {idx} must be a number, got {type(x).__name__}")
        fx = float(x)
        if math.isnan(fx) or math.isinf(fx):
            raise ValueError(f"Vector element at index {idx} contains NaN or Infinite value.")
        parsed.append(fx)

    if metric == "cosine":
        norm = math.sqrt(sum(x * x for x in parsed))
        if norm == 0.0:
            raise ValueError("Zero vector cannot be used with cosine distance metric.")

    return parsed


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
    ) -> None:
        norm_metric = metric.lower().strip()
        if norm_metric not in METRIC_CONFIG:
            raise ValueError(f"Unsupported metric '{metric}'. Expected one of {sorted(METRIC_CONFIG.keys())}")

        self._collection_name = collection_name
        self._dimensions = dimensions
        self._metric = norm_metric
        self._pool = pool
        self._initialized = False
        self._lock = asyncio.Lock()

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
            pagination=True,
            batch_upsert=True,
            native_distance_metrics=("cosine", "l2", "inner_product"),
        )

    def _get_table_name(self, name: str | None = None) -> str:
        coll = name or self._collection_name
        table = f"swiftpy_vectors_{coll}"
        validate_identifier(table)
        return table

    def _get_index_name(self, name: str | None = None) -> str:
        coll = name or self._collection_name
        idx = f"swiftpy_vectors_{coll}_{self._metric}_hnsw_idx"
        validate_identifier(idx)
        return idx

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
            try:
                container = get_container()
                db_pool = container.resolve(DatabasePool)
                try:
                    pool = db_pool.get_pool()
                except RuntimeError:
                    pool = await db_pool.init()
            except (RuntimeError, LookupError, KeyError):
                try:
                    config = Config.get_instance()
                except RuntimeError:
                    config = Config()
                db_pool = DatabasePool(config)
                pool = await db_pool.init()

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
        if not name or not name.strip():
            raise ValueError("Collection name must be a non-empty string.")
        if dimensions <= 0:
            raise ValueError(f"Dimensions must be a positive integer, got {dimensions}")
        norm_metric = metric.lower().strip()
        if norm_metric not in METRIC_CONFIG:
            raise ValueError(f"Unsupported metric '{metric}'. Expected one of {sorted(METRIC_CONFIG.keys())}")

        self._collection_name = name
        self._dimensions = dimensions
        self._metric = norm_metric

        table = self._get_table_name(name)
        quoted_table = quote_identifier(table)
        idx_name = self._get_index_name(name)
        quoted_idx = quote_identifier(idx_name)
        opclass = METRIC_CONFIG[self._metric]["opclass"]

        async with self._connection() as conn:
            # 1. Ensure pgvector extension exists
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")

            # 2. Check for dimension mismatch on pre-existing collection
            dim_sql = """
            SELECT atttypmod
            FROM pg_attribute
            JOIN pg_class ON pg_class.oid = pg_attribute.attrelid
            WHERE pg_class.relname = $1 AND pg_attribute.attname = 'embedding';
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
        if not isinstance(id, str) or not id.strip():
            raise ValueError("Record id must be a non-empty string.")

        parsed_vec = _validate_vector(vector, self._dimensions, self._metric)
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
            await conn.execute(sql, id, vec_str, content, meta_json)

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
            if isinstance(rec, VectorRecord):
                rec_id = rec.id
                rec_vec = rec.vector
                rec_meta = rec.metadata
                rec_content = rec.content
            elif isinstance(rec, tuple):
                rec_id = rec[0]
                rec_vec = rec[1]
                rec_meta = rec[2] if len(rec) > 2 and rec[2] is not None else {}
                rec_content = rec[3] if len(rec) > 3 and isinstance(rec[3], str) else ""
            elif isinstance(rec, dict):
                rec_id = rec["id"]
                rec_vec = rec["vector"]
                rec_meta = rec.get("metadata") or {}
                rec_content = rec.get("content", "")
            else:
                raise TypeError(f"Unsupported record format: {type(rec).__name__}")

            if not isinstance(rec_id, str) or not rec_id.strip():
                raise ValueError("Record id must be a non-empty string.")

            parsed_vec = _validate_vector(rec_vec, self._dimensions, self._metric)
            formatted_rows.append(
                (
                    rec_id,
                    _format_vector(parsed_vec),
                    rec_content,
                    json.dumps(rec_meta),
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

        parsed_vec = _validate_vector(vector, self._dimensions, self._metric)
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
        """Delete multiple vector records by ids."""
        if not ids:
            return 0

        await self._ensure_collection()
        table = quote_identifier(self._get_table_name())
        sql = f"DELETE FROM {table} WHERE id = ANY($1::varchar[]);"

        async with self._connection() as conn:
            result = await conn.execute(sql, list(ids))

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
