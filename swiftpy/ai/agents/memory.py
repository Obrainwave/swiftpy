from __future__ import annotations

import abc
import asyncio
import datetime
import hashlib
import json
import logging
import time
import weakref
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from swiftpy.ai.conversation import Conversation
from swiftpy.ai.exceptions import AIConnectionError
from swiftpy.ai.providers.base import Message, ToolCall
from swiftpy.core.config import Config
from swiftpy.core.container import get_container
from swiftpy.database.identifiers import (
    MAX_IDENTIFIER_LENGTH,
    quote_identifier,
    validate_identifier,
)
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection
from swiftpy.support.redis_config import resolve_redis_params

logger = logging.getLogger("swiftpy.ai.agents.memory")


def _bounded_index_name(table_name: str, suffix: str = "_session") -> str:
    """Return a PostgreSQL identifier within MAX_IDENTIFIER_LENGTH (63 bytes)."""
    candidate = f"idx_{table_name}{suffix}"
    if len(candidate.encode()) <= MAX_IDENTIFIER_LENGTH:
        return validate_identifier(candidate)
    digest = hashlib.sha256(f"{table_name}:{suffix}".encode()).hexdigest()[:8]
    tail = f"_{digest}{suffix}"
    prefix_budget = MAX_IDENTIFIER_LENGTH - len(tail.encode())
    trimmed_prefix = f"idx_{table_name}"[:prefix_budget].rstrip("_")
    return validate_identifier(f"{trimmed_prefix}{tail}")


def _resolve_expiry(ttl: float | None, default_ttl: float | None) -> datetime.datetime | None:
    effective = ttl if ttl is not None else default_ttl
    if effective is None or effective <= 0:
        return None
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=float(effective))


def _serialize_tool_call(tc: ToolCall) -> dict[str, Any]:
    """Serialize a ToolCall into a JSON-compatible dictionary."""
    return {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.name,
            "arguments": (
                json.dumps(tc.arguments)
                if isinstance(tc.arguments, dict)
                else str(tc.arguments)
            ),
        },
    }


def _deserialize_tool_call(item: dict[str, Any]) -> ToolCall:
    """Deserialize a dictionary into a ToolCall instance."""
    tc_id = str(item.get("id") or "")
    fn = item.get("function")
    if isinstance(fn, dict):
        name = str(fn.get("name") or "")
        args_raw = fn.get("arguments")
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {"raw": args_raw}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {}
    else:
        name = str(item.get("name") or "")
        args_raw = item.get("arguments")
        args = args_raw if isinstance(args_raw, dict) else {}
    return ToolCall(id=tc_id, name=name, arguments=args)


def _serialize_message(msg: Message) -> dict[str, Any]:
    """Serialize a Message instance into a JSON-compatible dictionary."""
    payload: dict[str, Any] = {
        "role": msg.role.lower(),
        "content": msg.content,
    }
    if msg.name:
        payload["name"] = msg.name
    if msg.tool_call_id:
        payload["tool_call_id"] = msg.tool_call_id
    if msg.tool_calls:
        payload["tool_calls"] = [_serialize_tool_call(tc) for tc in msg.tool_calls]
    return payload


def _deserialize_message(data: dict[str, Any]) -> Message:
    """Deserialize a dictionary into a validated Message instance."""
    role = str(data.get("role", "user"))
    content = str(data.get("content", ""))
    name = data.get("name")
    tool_call_id = data.get("tool_call_id")
    raw_tool_calls = data.get("tool_calls")
    tool_calls: list[ToolCall] | None = None
    if isinstance(raw_tool_calls, list):
        tool_calls = [_deserialize_tool_call(item) for item in raw_tool_calls if isinstance(item, dict)]

    return Message(
        role=role,
        content=content,
        name=str(name) if name is not None else None,
        tool_calls=tool_calls,
        tool_call_id=str(tool_call_id) if tool_call_id is not None else None,
    )


class BaseMemoryBackend(abc.ABC):
    """Abstract protocol for agent memory backends."""

    @abc.abstractmethod
    async def load_messages(self, session_id: str) -> list[Message]:
        """Load ordered messages for a given session."""
        raise NotImplementedError

    @abc.abstractmethod
    async def save_messages(
        self,
        session_id: str,
        messages: Sequence[Message],
        ttl: int | None = None,
    ) -> None:
        """Persist ordered messages for a given session."""
        raise NotImplementedError

    @abc.abstractmethod
    async def append_message(
        self,
        session_id: str,
        message: Message,
        ttl: int | None = None,
    ) -> None:
        """Append a single message to the session's memory."""
        raise NotImplementedError

    @abc.abstractmethod
    async def clear(self, session_id: str) -> None:
        """Delete all messages and state for the given session."""
        raise NotImplementedError

    @abc.abstractmethod
    async def exists(self, session_id: str) -> bool:
        """Check if any memory exists for the given session."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        """Get a state variable for the session."""
        raise NotImplementedError

    @abc.abstractmethod
    async def set_state(self, session_id: str, key: str, value: Any) -> None:
        """Set a state variable for the session."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_all_state(self, session_id: str) -> dict[str, Any]:
        """Get the full state dictionary for the session."""
        raise NotImplementedError


class InMemoryMemoryBackend(BaseMemoryBackend):
    """
    In-memory agent memory backend for unit tests, local debugging,
    and ephemeral agent executions with optional TTL expiration.
    """

    def __init__(self, default_ttl: float | None = None) -> None:
        if default_ttl is not None and (
            isinstance(default_ttl, bool)
            or not isinstance(default_ttl, (int, float))
            or float(default_ttl) <= 0
        ):
            raise ValueError(f"default_ttl must be a positive number, got {default_ttl}")
        self.default_ttl = default_ttl
        self._messages: dict[str, list[dict[str, Any]]] = {}
        self._state: dict[str, dict[str, Any]] = {}
        self._msg_expires: dict[str, float] = {}
        self._state_expires: dict[str, float] = {}

    def _prune_expired(self, session_id: str) -> None:
        now = time.monotonic()
        msg_exp = self._msg_expires.get(session_id)
        if msg_exp is not None and now >= msg_exp:
            self._msg_expires.pop(session_id, None)
            self._messages.pop(session_id, None)
        state_exp = self._state_expires.get(session_id)
        if state_exp is not None and now >= state_exp:
            self._state_expires.pop(session_id, None)
            self._state.pop(session_id, None)

    def _apply_ttl(
        self,
        expiry_map: dict[str, float],
        session_id: str,
        ttl: float | None,
    ) -> None:
        effective = ttl if ttl is not None else self.default_ttl
        if effective is not None and effective > 0:
            expiry_map[session_id] = time.monotonic() + float(effective)
        else:
            expiry_map.pop(session_id, None)

    async def load_messages(self, session_id: str) -> list[Message]:
        self._prune_expired(session_id)
        stored = self._messages.get(session_id, [])
        return [_deserialize_message(item) for item in stored]

    async def save_messages(
        self,
        session_id: str,
        messages: Sequence[Message],
        ttl: int | None = None,
    ) -> None:
        self._messages[session_id] = [_serialize_message(m) for m in messages]
        self._apply_ttl(self._msg_expires, session_id, ttl)

    async def append_message(
        self,
        session_id: str,
        message: Message,
        ttl: int | None = None,
    ) -> None:
        self._prune_expired(session_id)
        if session_id not in self._messages:
            self._messages[session_id] = []
        self._messages[session_id].append(_serialize_message(message))
        self._apply_ttl(self._msg_expires, session_id, ttl)

    async def clear(self, session_id: str) -> None:
        self._messages.pop(session_id, None)
        self._state.pop(session_id, None)
        self._msg_expires.pop(session_id, None)
        self._state_expires.pop(session_id, None)

    async def exists(self, session_id: str) -> bool:
        self._prune_expired(session_id)
        return session_id in self._messages or session_id in self._state

    async def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        self._prune_expired(session_id)
        return self._state.get(session_id, {}).get(key, default)

    async def set_state(
        self,
        session_id: str,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> None:
        self._prune_expired(session_id)
        if session_id not in self._state:
            self._state[session_id] = {}
        self._state[session_id][key] = value
        self._apply_ttl(self._state_expires, session_id, ttl)

    async def get_all_state(self, session_id: str) -> dict[str, Any]:
        self._prune_expired(session_id)
        return dict(self._state.get(session_id, {}))


class RedisMemoryBackend(BaseMemoryBackend):
    """
    Redis-backed agent memory backend for distributed, horizontally scalable
    persistence across calls and worker nodes, with shared pooled clients and
    atomic hash-based state updates.
    """

    _POOLS: ClassVar[
        weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, aioredis.ConnectionPool]]
    ] = weakref.WeakKeyDictionary()

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        config: Config | None = None,
        key_prefix: str = "agent:memory",
        default_ttl: int | None = None,
    ) -> None:
        self._client = redis_client
        self._config = config
        self._key_prefix = key_prefix
        self.default_ttl = default_ttl
        self._owns_client = redis_client is None

    def _msg_key(self, session_id: str) -> str:
        clean = session_id.strip()
        return f"{self._key_prefix}:{clean}:messages"

    def _state_key(self, session_id: str) -> str:
        clean = session_id.strip()
        return f"{self._key_prefix}:{clean}:state"

    @classmethod
    def _get_or_create_pool(cls, url: str) -> aioredis.ConnectionPool:
        loop = asyncio.get_running_loop()
        loop_pools = cls._POOLS.get(loop)
        if loop_pools is None:
            loop_pools = {}
            cls._POOLS[loop] = loop_pools
        pool = loop_pools.get(url)
        if pool is None:
            pool = aioredis.ConnectionPool.from_url(
                url,
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=5.0,
            )
            loop_pools[url] = pool
        return pool

    async def get_client(self) -> aioredis.Redis:
        if self._client is None:
            cfg = self._config or Config.get_instance()
            params = resolve_redis_params(cfg, section="ai")
            redis_url = str(params["url"])
            pool = self._get_or_create_pool(redis_url)
            self._client = aioredis.Redis(connection_pool=pool)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose(close_connection_pool=False)
            except (RedisError, OSError) as exc:
                logger.debug("Failed to close Redis client gracefully: %s", exc)
            finally:
                self._client = None

    async def load_messages(self, session_id: str) -> list[Message]:
        client = await self.get_client()
        key = self._msg_key(session_id)
        try:
            items = await client.lrange(key, 0, -1)
        except RedisError as exc:
            raise AIConnectionError(f"Failed to load agent memory from Redis: {exc}") from exc

        if not items:
            return []
        result: list[Message] = []
        for raw in items:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    result.append(_deserialize_message(data))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Corrupted agent memory data in Redis for session '{session_id}': {exc}") from exc
        return result

    async def save_messages(
        self,
        session_id: str,
        messages: Sequence[Message],
        ttl: int | None = None,
    ) -> None:
        client = await self.get_client()
        key = self._msg_key(session_id)
        effective_ttl = ttl if ttl is not None else self.default_ttl
        payloads = [json.dumps(_serialize_message(m)) for m in messages]
        try:
            pipe = client.pipeline()
            pipe.delete(key)
            if payloads:
                pipe.rpush(key, *payloads)
            if effective_ttl is not None and effective_ttl > 0:
                pipe.expire(key, effective_ttl)
            await pipe.execute()
        except RedisError as exc:
            raise AIConnectionError(f"Failed to save agent memory to Redis: {exc}") from exc

    async def append_message(
        self,
        session_id: str,
        message: Message,
        ttl: int | None = None,
    ) -> None:
        client = await self.get_client()
        key = self._msg_key(session_id)
        effective_ttl = ttl if ttl is not None else self.default_ttl
        payload = json.dumps(_serialize_message(message))
        try:
            pipe = client.pipeline()
            pipe.rpush(key, payload)
            if effective_ttl is not None and effective_ttl > 0:
                pipe.expire(key, effective_ttl)
            await pipe.execute()
        except RedisError as exc:
            raise AIConnectionError(f"Failed to append agent message to Redis: {exc}") from exc

    async def clear(self, session_id: str) -> None:
        client = await self.get_client()
        msg_key = self._msg_key(session_id)
        state_key = self._state_key(session_id)
        try:
            await client.delete(msg_key, state_key)
        except RedisError as exc:
            raise AIConnectionError(f"Failed to clear agent memory in Redis: {exc}") from exc

    async def exists(self, session_id: str) -> bool:
        client = await self.get_client()
        msg_key = self._msg_key(session_id)
        state_key = self._state_key(session_id)
        try:
            return bool(await client.exists(msg_key, state_key))
        except RedisError as exc:
            raise AIConnectionError(f"Failed to check agent memory in Redis: {exc}") from exc

    async def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        client = await self.get_client()
        state_key = self._state_key(session_id)
        try:
            raw = await client.hget(state_key, key)
        except RedisError as exc:
            raise AIConnectionError(f"Failed to get agent state from Redis: {exc}") from exc
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return raw

    async def set_state(
        self,
        session_id: str,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> None:
        client = await self.get_client()
        state_key = self._state_key(session_id)
        payload = json.dumps(value)
        effective_ttl = ttl if ttl is not None else self.default_ttl
        try:
            pipe = client.pipeline()
            pipe.hset(state_key, key, payload)
            if effective_ttl is not None and effective_ttl > 0:
                pipe.expire(state_key, effective_ttl)
            await pipe.execute()
        except RedisError as exc:
            raise AIConnectionError(f"Failed to set agent state in Redis: {exc}") from exc

    async def get_all_state(self, session_id: str) -> dict[str, Any]:
        client = await self.get_client()
        state_key = self._state_key(session_id)
        try:
            raw_map = await client.hgetall(state_key)
        except RedisError as exc:
            raise AIConnectionError(f"Failed to get agent state from Redis: {exc}") from exc
        if not raw_map:
            return {}
        decoded: dict[str, Any] = {}
        for k, raw_val in raw_map.items():
            try:
                decoded[str(k)] = json.loads(raw_val)
            except (json.JSONDecodeError, TypeError, ValueError):
                decoded[str(k)] = raw_val
        return decoded


class DatabaseMemoryBackend(BaseMemoryBackend):
    """
    Database-backed agent memory backend storing persistent conversation
    and agent state in PostgreSQL using parameterized queries, active transaction
    connection reuse, bounded index names, and optional TTL expiration.
    """

    def __init__(
        self,
        pool: DatabasePool | None = None,
        table_name: str = "agent_memories",
        default_ttl: int | None = None,
    ) -> None:
        if default_ttl is not None and (
            isinstance(default_ttl, bool)
            or not isinstance(default_ttl, (int, float))
            or float(default_ttl) <= 0
        ):
            raise ValueError(f"default_ttl must be a positive number, got {default_ttl}")
        self._pool = pool
        self.default_ttl = default_ttl
        self.table_name = validate_identifier(table_name)
        messages_table = (
            f"{table_name}_messages"
            if not table_name.endswith("_messages")
            else table_name
        )
        state_table = (
            f"{table_name}_state"
            if not table_name.endswith("_state")
            else f"{table_name}_kv"
        )
        self.messages_table = validate_identifier(messages_table)
        self.state_table = validate_identifier(state_table)
        self._messages_index = _bounded_index_name(self.messages_table, "_session")
        self._table_checked = False

    def _get_pool(self) -> DatabasePool:
        if self._pool is not None:
            return self._pool
        try:
            container = get_container()
            self._pool = container.resolve(DatabasePool)
            return self._pool
        except Exception as exc:
            raise RuntimeError(f"DatabasePool could not be resolved for agent memory: {exc}") from exc

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        active = get_active_connection()
        if active is not None:
            yield active
            return

        db_pool = self._get_pool()
        try:
            raw_pool = db_pool.get_pool()
        except RuntimeError:
            raw_pool = await db_pool.init()

        async with raw_pool.acquire() as conn:
            yield conn

    async def ensure_table(self) -> None:
        """Create the storage tables and TTL columns if they do not already exist."""
        if self._table_checked:
            return
        msg_table_q = quote_identifier(self.messages_table)
        state_table_q = quote_identifier(self.state_table)
        idx_name_q = quote_identifier(self._messages_index)
        messages_sql = f"""
        CREATE TABLE IF NOT EXISTS {msg_table_q} (
            id BIGSERIAL PRIMARY KEY,
            session_id VARCHAR(255) NOT NULL,
            role VARCHAR(50) NOT NULL,
            content TEXT NOT NULL,
            name VARCHAR(255),
            tool_call_id VARCHAR(255),
            tool_calls JSONB,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ
        );
        ALTER TABLE {msg_table_q} ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
        CREATE INDEX IF NOT EXISTS {idx_name_q} ON {msg_table_q} (session_id, id);
        """
        state_sql = f"""
        CREATE TABLE IF NOT EXISTS {state_table_q} (
            session_id VARCHAR(255) NOT NULL,
            key VARCHAR(255) NOT NULL,
            value JSONB NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ,
            PRIMARY KEY (session_id, key)
        );
        ALTER TABLE {state_table_q} ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
        """
        async with self._connection() as conn:
            await conn.execute(messages_sql)
            await conn.execute(state_sql)
        if get_active_connection() is None:
            self._table_checked = True

    async def load_messages(self, session_id: str) -> list[Message]:
        await self.ensure_table()
        sql = f"""
        SELECT role, content, name, tool_call_id, tool_calls
        FROM {quote_identifier(self.messages_table)}
        WHERE session_id = $1
          AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
        ORDER BY id ASC;
        """
        async with self._connection() as conn:
            rows = await conn.fetch(sql, session_id)

        messages: list[Message] = []
        for r in rows:
            raw_tc = r["tool_calls"]
            tool_calls = None
            if raw_tc:
                if isinstance(raw_tc, str):
                    try:
                        raw_tc = json.loads(raw_tc)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        raw_tc = []
                if isinstance(raw_tc, list):
                    tool_calls = [_deserialize_tool_call(tc) for tc in raw_tc if isinstance(tc, dict)]
            messages.append(
                Message(
                    role=r["role"],
                    content=r["content"],
                    name=r["name"],
                    tool_call_id=r["tool_call_id"],
                    tool_calls=tool_calls,
                )
            )
        return messages

    async def save_messages(
        self,
        session_id: str,
        messages: Sequence[Message],
        ttl: int | None = None,
    ) -> None:
        await self.ensure_table()
        expires_at = _resolve_expiry(ttl, self.default_ttl)
        delete_sql = f"DELETE FROM {quote_identifier(self.messages_table)} WHERE session_id = $1;"
        insert_sql = f"""
        INSERT INTO {quote_identifier(self.messages_table)}
            (session_id, role, content, name, tool_call_id, tool_calls, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7);
        """
        async with self._connection() as conn, conn.transaction():
            await conn.execute(delete_sql, session_id)
            for msg in messages:
                tc_json = (
                    json.dumps([_serialize_tool_call(tc) for tc in msg.tool_calls])
                    if msg.tool_calls
                    else None
                )
                await conn.execute(
                    insert_sql,
                    session_id,
                    msg.role,
                    msg.content,
                    msg.name,
                    msg.tool_call_id,
                    tc_json,
                    expires_at,
                )

    async def append_message(
        self,
        session_id: str,
        message: Message,
        ttl: int | None = None,
    ) -> None:
        await self.ensure_table()
        expires_at = _resolve_expiry(ttl, self.default_ttl)
        insert_sql = f"""
        INSERT INTO {quote_identifier(self.messages_table)}
            (session_id, role, content, name, tool_call_id, tool_calls, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7);
        """
        tc_json = (
            json.dumps([_serialize_tool_call(tc) for tc in message.tool_calls])
            if message.tool_calls
            else None
        )
        async with self._connection() as conn:
            await conn.execute(
                insert_sql,
                session_id,
                message.role,
                message.content,
                message.name,
                message.tool_call_id,
                tc_json,
                expires_at,
            )

    async def clear(self, session_id: str) -> None:
        await self.ensure_table()
        async with self._connection() as conn, conn.transaction():
            await conn.execute(
                f"DELETE FROM {quote_identifier(self.messages_table)} WHERE session_id = $1;",
                session_id,
            )
            await conn.execute(
                f"DELETE FROM {quote_identifier(self.state_table)} WHERE session_id = $1;",
                session_id,
            )

    async def exists(self, session_id: str) -> bool:
        await self.ensure_table()
        sql = f"""
        SELECT (
            EXISTS(
                SELECT 1 FROM {quote_identifier(self.messages_table)}
                WHERE session_id = $1
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            )
            OR EXISTS(
                SELECT 1 FROM {quote_identifier(self.state_table)}
                WHERE session_id = $1
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            )
        ) AS has_memory;
        """
        async with self._connection() as conn:
            val = await conn.fetchval(sql, session_id)
        return bool(val)

    async def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        await self.ensure_table()
        sql = f"""
        SELECT value FROM {quote_identifier(self.state_table)}
        WHERE session_id = $1
          AND key = $2
          AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP);
        """
        async with self._connection() as conn:
            row = await conn.fetchrow(sql, session_id, key)
        if not row or row["value"] is None:
            return default
        raw = row["value"]
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                return raw
        return raw

    async def set_state(
        self,
        session_id: str,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> None:
        await self.ensure_table()
        expires_at = _resolve_expiry(ttl, self.default_ttl)
        payload = json.dumps(value)
        sql = f"""
        INSERT INTO {quote_identifier(self.state_table)}
            (session_id, key, value, updated_at, expires_at)
        VALUES ($1, $2, $3::jsonb, CURRENT_TIMESTAMP, $4)
        ON CONFLICT (session_id, key)
        DO UPDATE SET
            value = EXCLUDED.value,
            updated_at = CURRENT_TIMESTAMP,
            expires_at = EXCLUDED.expires_at;
        """
        async with self._connection() as conn:
            await conn.execute(sql, session_id, key, payload, expires_at)

    async def get_all_state(self, session_id: str) -> dict[str, Any]:
        await self.ensure_table()
        sql = f"""
        SELECT key, value FROM {quote_identifier(self.state_table)}
        WHERE session_id = $1
          AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP);
        """
        async with self._connection() as conn:
            rows = await conn.fetch(sql, session_id)
        result: dict[str, Any] = {}
        for r in rows:
            raw = r["value"]
            if isinstance(raw, str):
                try:
                    result[r["key"]] = json.loads(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    result[r["key"]] = raw
            else:
                result[r["key"]] = raw
        return result


class AgentMemory:
    """
    Session-scoped memory manager providing an ergonomic API for agents.
    Wraps an interchangeable backend (InMemory, Redis, Database).
    """

    def __init__(
        self,
        backend: BaseMemoryBackend | None = None,
        session_id: str = "default",
    ) -> None:
        self._backend: BaseMemoryBackend = backend if backend is not None else InMemoryMemoryBackend()
        self.session_id: str = session_id

    @property
    def backend(self) -> BaseMemoryBackend:
        """Return the underlying storage backend."""
        return self._backend

    async def get_messages(self) -> list[Message]:
        """Retrieve all ordered messages for this agent session."""
        return await self._backend.load_messages(self.session_id)

    async def save_messages(self, messages: Sequence[Message], ttl: int | None = None) -> None:
        """Persist ordered messages for this agent session."""
        await self._backend.save_messages(self.session_id, messages, ttl=ttl)

    async def add_message(self, message: Message, ttl: int | None = None) -> None:
        """Append a message to this agent session."""
        await self._backend.append_message(self.session_id, message, ttl=ttl)

    async def add_user(self, content: str) -> None:
        """Append a user message."""
        await self.add_message(Message(role="user", content=content))

    async def add_assistant(
        self,
        content: str,
        tool_calls: list[ToolCall] | None = None,
    ) -> None:
        """Append an assistant response message."""
        await self.add_message(Message(role="assistant", content=content, tool_calls=tool_calls))

    async def add_system(self, content: str) -> None:
        """Append a system message."""
        await self.add_message(Message(role="system", content=content))

    async def add_tool_result(self, content: str, name: str, tool_call_id: str) -> None:
        """Append a tool execution result message."""
        await self.add_message(Message(role="tool", content=content, name=name, tool_call_id=tool_call_id))

    async def clear(self) -> None:
        """Clear all messages and state for this agent session."""
        await self._backend.clear(self.session_id)

    async def exists(self) -> bool:
        """Check if memory exists for this agent session."""
        return await self._backend.exists(self.session_id)

    async def set_state(self, key: str, value: Any) -> None:
        """Set a state variable for this agent session."""
        await self._backend.set_state(self.session_id, key, value)

    async def get_state(self, key: str, default: Any = None) -> Any:
        """Get a state variable for this agent session."""
        return await self._backend.get_state(self.session_id, key, default)

    async def get_all_state(self) -> dict[str, Any]:
        """Get the full state dictionary for this agent session."""
        return await self._backend.get_all_state(self.session_id)

    async def to_conversation(self) -> Conversation:
        """Export session memory into a Conversation container."""
        messages = await self.get_messages()
        return Conversation(messages=messages)

    def fork(self, new_session_id: str) -> AgentMemory:
        """Return a new AgentMemory instance sharing the same backend under a different session ID."""
        return AgentMemory(backend=self._backend, session_id=new_session_id)


__all__ = [
    "AgentMemory",
    "BaseMemoryBackend",
    "DatabaseMemoryBackend",
    "InMemoryMemoryBackend",
    "RedisMemoryBackend",
]
