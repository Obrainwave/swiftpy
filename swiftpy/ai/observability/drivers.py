from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.identifiers import quote_identifier, validate_identifier
from swiftpy.database.pool import DatabasePool


def sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive secrets, tokens, passwords, and credentials from observation metadata."""
    redacted: dict[str, Any] = {}
    for k, v in metadata.items():
        lower_k = k.lower()
        if any(secret in lower_k for secret in ("api_key", "password", "secret", "token", "authorization")):
            redacted[k] = "[REDACTED]"
        elif isinstance(v, dict):
            redacted[k] = sanitize_metadata(v)
        else:
            redacted[k] = v
    return redacted


@dataclass
class AIObservation:
    """Represents a single telemetry observation for an LLM call."""

    id: str
    timestamp: datetime
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: float
    cost: float
    prompt_hash: str
    status: str = "success"
    error_message: str | None = None
    ttft_ms: float | None = None
    duration_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.metadata:
            self.metadata = sanitize_metadata(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        """Convert observation to a JSON-serializable dictionary."""
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AIObservation:
        """Construct observation from a dictionary."""
        raw_ts = data.get("timestamp")
        if isinstance(raw_ts, str):
            ts = datetime.fromisoformat(raw_ts)
        elif isinstance(raw_ts, datetime):
            ts = raw_ts
        else:
            ts = datetime.now(UTC)

        raw_ttft = data.get("ttft_ms")
        ttft_ms = float(raw_ttft) if raw_ttft is not None else None

        raw_duration = data.get("duration_ms")
        duration_ms = float(raw_duration) if raw_duration is not None else None

        return cls(
            id=str(data.get("id", "")),
            timestamp=ts,
            model=str(data.get("model", "")),
            provider=str(data.get("provider", "")),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            total_tokens=int(data.get("total_tokens", 0)),
            latency_ms=float(data.get("latency_ms", 0.0)),
            cost=float(data.get("cost", 0.0)),
            prompt_hash=str(data.get("prompt_hash", "")),
            status=str(data.get("status", "success")),
            error_message=data.get("error_message"),
            ttft_ms=ttft_ms,
            duration_ms=duration_ms,
            metadata=dict(data.get("metadata", {})),
        )


class BaseObservabilityDriver(ABC):
    """Abstract base class for all AI observability storage drivers."""

    @abstractmethod
    async def record(self, observation: AIObservation) -> None:
        """Persist a single AI observation record."""

    @abstractmethod
    async def get_observations(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        start_date: datetime | str | None = None,
        end_date: datetime | str | None = None,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
    ) -> list[AIObservation]:
        """Retrieve recent AI observations matching optional filters, ordered by timestamp descending."""

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        """Retrieve aggregated telemetry statistics across all recorded observations."""

    @abstractmethod
    async def clear(self) -> None:
        """Clear all stored observations (useful in test teardown)."""


def _parse_ts(val: datetime | str | None) -> datetime | None:
    if val is None or isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None


def _matches_filters(
    obs: AIObservation,
    start_date: datetime | str | None = None,
    end_date: datetime | str | None = None,
    provider: str | None = None,
    model: str | None = None,
    status: str | None = None,
) -> bool:
    ts_start = _parse_ts(start_date)
    ts_end = _parse_ts(end_date)

    # Normalize tz awareness if needed
    obs_ts = obs.timestamp
    if ts_start is not None:
        if ts_start.tzinfo is None and obs_ts.tzinfo is not None:
            ts_start = ts_start.replace(tzinfo=UTC)
        elif ts_start.tzinfo is not None and obs_ts.tzinfo is None:
            obs_ts = obs_ts.replace(tzinfo=UTC)
        if obs_ts < ts_start:
            return False

    if ts_end is not None:
        if ts_end.tzinfo is None and obs_ts.tzinfo is not None:
            ts_end = ts_end.replace(tzinfo=UTC)
        elif ts_end.tzinfo is not None and obs_ts.tzinfo is None:
            obs_ts = obs_ts.replace(tzinfo=UTC)
        if obs_ts > ts_end:
            return False

    if provider is not None and obs.provider.lower() != provider.lower():
        return False
    if model is not None and obs.model.lower() != model.lower():
        return False

    return not (status is not None and obs.status.lower() != status.lower())


class InMemoryObservabilityDriver(BaseObservabilityDriver):
    """
    In-memory observability driver storing telemetry records in memory.
    Ideal for unit tests and local development without external dependencies.
    """

    def __init__(self) -> None:
        self._records: list[AIObservation] = []
        self._lock = asyncio.Lock()

    async def record(self, observation: AIObservation) -> None:
        async with self._lock:
            self._records.append(observation)

    async def get_observations(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        start_date: datetime | str | None = None,
        end_date: datetime | str | None = None,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
    ) -> list[AIObservation]:
        async with self._lock:
            filtered = [
                r
                for r in reversed(self._records)
                if _matches_filters(
                    r,
                    start_date=start_date,
                    end_date=end_date,
                    provider=provider,
                    model=model,
                    status=status,
                )
            ]
            return filtered[offset : offset + limit]

    async def get_stats(self) -> dict[str, Any]:
        async with self._lock:
            records = list(self._records)

        total_calls = len(records)
        total_input_tokens = sum(r.input_tokens for r in records)
        total_output_tokens = sum(r.output_tokens for r in records)
        total_tokens = sum(r.total_tokens for r in records)
        total_cost = sum(r.cost for r in records)
        avg_latency_ms = (
            sum(r.latency_ms for r in records) / total_calls if total_calls > 0 else 0.0
        )
        successful_calls = sum(1 for r in records if r.status == "success")
        failed_calls = sum(1 for r in records if r.status != "success")

        by_provider: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}

        for r in records:
            p = r.provider or "unknown"
            if p not in by_provider:
                by_provider[p] = {"calls": 0, "tokens": 0, "cost": 0.0}
            by_provider[p]["calls"] += 1
            by_provider[p]["tokens"] += r.total_tokens
            by_provider[p]["cost"] = round(by_provider[p]["cost"] + r.cost, 6)

            m = r.model or "unknown"
            if m not in by_model:
                by_model[m] = {"calls": 0, "tokens": 0, "cost": 0.0}
            by_model[m]["calls"] += 1
            by_model[m]["tokens"] += r.total_tokens
            by_model[m]["cost"] = round(by_model[m]["cost"] + r.cost, 6)

        return {
            "total_calls": total_calls,
            "successful_calls": successful_calls,
            "failed_calls": failed_calls,
            "total_tokens": total_tokens,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cost": round(total_cost, 6),
            "avg_latency_ms": round(avg_latency_ms, 2),
            "by_provider": by_provider,
            "by_model": by_model,
        }

    async def clear(self) -> None:
        async with self._lock:
            self._records.clear()


def _append_file_sync(file_path: Path, content: str) -> None:
    """Synchronous file append helper offloaded to thread executor."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as f:
        f.write(content + "\n")


def _read_file_sync(file_path: Path) -> list[str]:
    """Synchronous file lines reader helper offloaded to thread executor."""
    if not file_path.is_file():
        return []
    with file_path.open("r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def _truncate_file_sync(file_path: Path) -> None:
    """Synchronous file truncate helper offloaded to thread executor."""
    if file_path.is_file():
        with file_path.open("w", encoding="utf-8") as f:
            f.truncate(0)


class FileObservabilityDriver(BaseObservabilityDriver):
    """
    File-based observability driver writing structured JSON lines to storage/logs/ai.log.
    Non-blocking: offloads sync file I/O to background threads via asyncio.to_thread.
    """

    def __init__(self, log_path: str | Path = "storage/logs/ai.log") -> None:
        self.log_path = Path(log_path)
        self._lock = asyncio.Lock()

    async def record(self, observation: AIObservation) -> None:
        line = json.dumps(observation.to_dict())
        async with self._lock:
            await asyncio.to_thread(_append_file_sync, self.log_path, line)

    async def get_observations(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        start_date: datetime | str | None = None,
        end_date: datetime | str | None = None,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
    ) -> list[AIObservation]:
        async with self._lock:
            lines = await asyncio.to_thread(_read_file_sync, self.log_path)

        observations: list[AIObservation] = []
        for line in reversed(lines):
            try:
                data = json.loads(line)
                obs = AIObservation.from_dict(data)
                if _matches_filters(
                    obs,
                    start_date=start_date,
                    end_date=end_date,
                    provider=provider,
                    model=model,
                    status=status,
                ):
                    observations.append(obs)
            except (json.JSONDecodeError, ValueError):
                continue

        return observations[offset : offset + limit]

    async def get_stats(self) -> dict[str, Any]:
        observations = await self.get_observations(limit=100000, offset=0)

        total_calls = len(observations)
        total_input_tokens = sum(r.input_tokens for r in observations)
        total_output_tokens = sum(r.output_tokens for r in observations)
        total_tokens = sum(r.total_tokens for r in observations)
        total_cost = sum(r.cost for r in observations)
        avg_latency_ms = (
            sum(r.latency_ms for r in observations) / total_calls
            if total_calls > 0
            else 0.0
        )
        successful_calls = sum(1 for r in observations if r.status == "success")
        failed_calls = sum(1 for r in observations if r.status != "success")

        by_provider: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}

        for r in observations:
            p = r.provider or "unknown"
            if p not in by_provider:
                by_provider[p] = {"calls": 0, "tokens": 0, "cost": 0.0}
            by_provider[p]["calls"] += 1
            by_provider[p]["tokens"] += r.total_tokens
            by_provider[p]["cost"] = round(by_provider[p]["cost"] + r.cost, 6)

            m = r.model or "unknown"
            if m not in by_model:
                by_model[m] = {"calls": 0, "tokens": 0, "cost": 0.0}
            by_model[m]["calls"] += 1
            by_model[m]["tokens"] += r.total_tokens
            by_model[m]["cost"] = round(by_model[m]["cost"] + r.cost, 6)

        return {
            "total_calls": total_calls,
            "successful_calls": successful_calls,
            "failed_calls": failed_calls,
            "total_tokens": total_tokens,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cost": round(total_cost, 6),
            "avg_latency_ms": round(avg_latency_ms, 2),
            "by_provider": by_provider,
            "by_model": by_model,
        }

    async def clear(self) -> None:
        async with self._lock:
            await asyncio.to_thread(_truncate_file_sync, self.log_path)


class DatabaseObservabilityDriver(BaseObservabilityDriver):
    """
    Database observability driver writing structured telemetry rows
    to the PostgreSQL 'ai_observations' relational table.
    """

    def __init__(
        self,
        pool: DatabasePool | asyncpg.Pool | None = None,
        table_name: str = "ai_observations",
    ) -> None:
        self._raw_pool = pool
        self.table_name = validate_identifier(table_name)
        self._table_checked = False
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        if isinstance(self._raw_pool, asyncpg.Pool):
            return self._raw_pool
        if isinstance(self._raw_pool, DatabasePool):
            return await self._raw_pool.init()
        try:
            container = get_container()
            db_pool = container.resolve(DatabasePool)
            return await db_pool.init()
        except Exception as exc:
            raise RuntimeError(
                f"DatabasePool could not be resolved for AI observability: {exc}"
            ) from exc

    async def ensure_table(self) -> None:
        """Create the ai_observations storage table and indexes if not existing."""
        if self._table_checked:
            return

        async with self._lock:
            if self._table_checked:
                return

            pool = await self._get_pool()
            table_q = quote_identifier(self.table_name)
            idx_ts_q = quote_identifier(f"idx_{self.table_name}_timestamp")
            idx_prompt_q = quote_identifier(f"idx_{self.table_name}_prompt_hash")
            ddl = f"""
            CREATE TABLE IF NOT EXISTS {table_q} (
                id VARCHAR(64) PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model VARCHAR(255) NOT NULL,
                provider VARCHAR(64) NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                cost DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                prompt_hash VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'success',
                error_message TEXT,
                ttft_ms DOUBLE PRECISION,
                duration_ms DOUBLE PRECISION,
                metadata JSONB
            );
            ALTER TABLE {table_q} ADD COLUMN IF NOT EXISTS ttft_ms DOUBLE PRECISION;
            ALTER TABLE {table_q} ADD COLUMN IF NOT EXISTS duration_ms DOUBLE PRECISION;
            CREATE INDEX IF NOT EXISTS {idx_ts_q} ON {table_q} (timestamp DESC);
            CREATE INDEX IF NOT EXISTS {idx_prompt_q} ON {table_q} (prompt_hash);
            """
            async with pool.acquire() as conn:
                await conn.execute(ddl)
            self._table_checked = True

    async def record(self, observation: AIObservation) -> None:
        await self.ensure_table()
        pool = await self._get_pool()
        sql = f"""
        INSERT INTO {quote_identifier(self.table_name)} (
            id, timestamp, model, provider, input_tokens, output_tokens,
            total_tokens, latency_ms, cost, prompt_hash, status, error_message,
            ttft_ms, duration_ms, metadata
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
        )
        ON CONFLICT (id) DO UPDATE SET
            status = EXCLUDED.status,
            error_message = EXCLUDED.error_message,
            ttft_ms = EXCLUDED.ttft_ms,
            duration_ms = EXCLUDED.duration_ms,
            metadata = EXCLUDED.metadata;
        """
        meta_json = json.dumps(observation.metadata) if observation.metadata else "{}"
        async with pool.acquire() as conn:
            await conn.execute(
                sql,
                observation.id,
                observation.timestamp,
                observation.model,
                observation.provider,
                observation.input_tokens,
                observation.output_tokens,
                observation.total_tokens,
                observation.latency_ms,
                observation.cost,
                observation.prompt_hash,
                observation.status,
                observation.error_message,
                observation.ttft_ms,
                observation.duration_ms,
                meta_json,
            )

    async def get_observations(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        start_date: datetime | str | None = None,
        end_date: datetime | str | None = None,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
    ) -> list[AIObservation]:
        await self.ensure_table()
        pool = await self._get_pool()

        where_clauses: list[str] = []
        params: list[Any] = []
        idx = 1

        if start_date is not None:
            ts_start = (
                datetime.fromisoformat(start_date)
                if isinstance(start_date, str)
                else start_date
            )
            where_clauses.append(f"timestamp >= ${idx}")
            params.append(ts_start)
            idx += 1

        if end_date is not None:
            ts_end = (
                datetime.fromisoformat(end_date)
                if isinstance(end_date, str)
                else end_date
            )
            where_clauses.append(f"timestamp <= ${idx}")
            params.append(ts_end)
            idx += 1

        if provider is not None:
            where_clauses.append(f"LOWER(provider) = LOWER(${idx})")
            params.append(provider)
            idx += 1

        if model is not None:
            where_clauses.append(f"LOWER(model) = LOWER(${idx})")
            params.append(model)
            idx += 1

        if status is not None:
            where_clauses.append(f"LOWER(status) = LOWER(${idx})")
            params.append(status)
            idx += 1

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        sql = f"""
        SELECT id, timestamp, model, provider, input_tokens, output_tokens,
               total_tokens, latency_ms, cost, prompt_hash, status, error_message,
               ttft_ms, duration_ms, metadata
        FROM {quote_identifier(self.table_name)}
        {where_sql}
        ORDER BY timestamp DESC
        LIMIT ${idx} OFFSET ${idx + 1};
        """
        params.extend([limit, offset])

        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        results: list[AIObservation] = []
        for r in rows:
            meta = r["metadata"]
            if isinstance(meta, str):
                try:
                    meta_dict = json.loads(meta)
                except (json.JSONDecodeError, TypeError, ValueError):
                    meta_dict = {}
            elif isinstance(meta, dict):
                meta_dict = meta
            else:
                meta_dict = {}

            results.append(
                AIObservation(
                    id=r["id"],
                    timestamp=r["timestamp"],
                    model=r["model"],
                    provider=r["provider"],
                    input_tokens=r["input_tokens"],
                    output_tokens=r["output_tokens"],
                    total_tokens=r["total_tokens"],
                    latency_ms=r["latency_ms"],
                    cost=r["cost"],
                    prompt_hash=r["prompt_hash"],
                    status=r["status"],
                    error_message=r["error_message"],
                    ttft_ms=r["ttft_ms"],
                    duration_ms=r["duration_ms"],
                    metadata=meta_dict,
                )
            )
        return results

    async def get_stats(self) -> dict[str, Any]:
        await self.ensure_table()
        pool = await self._get_pool()
        table_q = quote_identifier(self.table_name)
        sql_summary = f"""
        SELECT
            COUNT(*) as total_calls,
            COUNT(*) FILTER (WHERE status = 'success') as successful_calls,
            COUNT(*) FILTER (WHERE status != 'success') as failed_calls,
            COALESCE(SUM(total_tokens), 0) as total_tokens,
            COALESCE(SUM(input_tokens), 0) as total_input_tokens,
            COALESCE(SUM(output_tokens), 0) as total_output_tokens,
            COALESCE(SUM(cost), 0.0) as total_cost,
            COALESCE(AVG(latency_ms), 0.0) as avg_latency_ms
        FROM {table_q};
        """
        sql_provider = f"""
        SELECT
            provider,
            COUNT(*) as calls,
            COALESCE(SUM(total_tokens), 0) as tokens,
            COALESCE(SUM(cost), 0.0) as cost
        FROM {table_q}
        GROUP BY provider;
        """
        sql_model = f"""
        SELECT
            model,
            COUNT(*) as calls,
            COALESCE(SUM(total_tokens), 0) as tokens,
            COALESCE(SUM(cost), 0.0) as cost
        FROM {table_q}
        GROUP BY model;
        """

        async with pool.acquire() as conn:
            summary_row = await conn.fetchrow(sql_summary)
            provider_rows = await conn.fetch(sql_provider)
            model_rows = await conn.fetch(sql_model)

        by_provider: dict[str, dict[str, Any]] = {}
        for r in provider_rows:
            p = r["provider"] or "unknown"
            by_provider[p] = {
                "calls": int(r["calls"]),
                "tokens": int(r["tokens"]),
                "cost": round(float(r["cost"]), 6),
            }

        by_model: dict[str, dict[str, Any]] = {}
        for r in model_rows:
            m = r["model"] or "unknown"
            by_model[m] = {
                "calls": int(r["calls"]),
                "tokens": int(r["tokens"]),
                "cost": round(float(r["cost"]), 6),
            }

        if summary_row:
            total_calls = int(summary_row["total_calls"])
            successful_calls = int(summary_row["successful_calls"])
            failed_calls = int(summary_row["failed_calls"])
            total_tokens = int(summary_row["total_tokens"])
            total_input_tokens = int(summary_row["total_input_tokens"])
            total_output_tokens = int(summary_row["total_output_tokens"])
            total_cost = round(float(summary_row["total_cost"]), 6)
            avg_latency_ms = round(float(summary_row["avg_latency_ms"]), 2)
        else:
            total_calls = 0
            successful_calls = 0
            failed_calls = 0
            total_tokens = 0
            total_input_tokens = 0
            total_output_tokens = 0
            total_cost = 0.0
            avg_latency_ms = 0.0

        return {
            "total_calls": total_calls,
            "successful_calls": successful_calls,
            "failed_calls": failed_calls,
            "total_tokens": total_tokens,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cost": total_cost,
            "avg_latency_ms": avg_latency_ms,
            "by_provider": by_provider,
            "by_model": by_model,
        }

    async def clear(self) -> None:
        await self.ensure_table()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"TRUNCATE TABLE {quote_identifier(self.table_name)};")
