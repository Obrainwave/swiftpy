from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Awaitable
from typing import Protocol

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from swiftpy.ai.conversation import Conversation
from swiftpy.ai.exceptions import AIConnectionError
from swiftpy.core.config import Config
from swiftpy.support.redis_config import resolve_redis_params


class _ConversationRedisClient(Protocol):
    def get(self, name: str, /) -> Awaitable[bytes | str | None]: ...
    def set(
        self, name: str, value: str, /, ex: int | None = None
    ) -> Awaitable[bool | str | bytes | None]: ...
    def delete(self, name: str, /) -> Awaitable[int]: ...
    def exists(self, name: str, /) -> Awaitable[int]: ...
    def aclose(self) -> Awaitable[None]: ...


class ConversationStore:
    """Abstract protocol for conversation persistence."""

    async def save(
        self,
        session_id: str,
        conversation: Conversation,
        ttl: int | None = None,
    ) -> None:
        raise NotImplementedError

    async def load(self, session_id: str) -> Conversation:
        raise NotImplementedError

    async def delete(self, session_id: str) -> bool:
        raise NotImplementedError

    async def exists(self, session_id: str) -> bool:
        raise NotImplementedError


class InMemoryConversationStore(ConversationStore):
    """Bounded LRU + TTL-aware in-memory conversation store."""

    def __init__(
        self,
        max_entries: int = 1000,
        default_ttl: int | None = None,
    ) -> None:
        if max_entries <= 0:
            raise ValueError(f"max_entries must be greater than 0, got {max_entries}")
        self.max_entries = max_entries
        self.default_ttl = default_ttl
        self._data: OrderedDict[str, tuple[str, float | None]] = OrderedDict()

    def _get_valid_payload(self, session_id: str) -> str | None:
        entry = self._data.get(session_id)
        if entry is None:
            return None
        raw, expires_at = entry
        if expires_at is not None and time.monotonic() >= expires_at:
            del self._data[session_id]
            return None
        self._data.move_to_end(session_id)
        return raw

    async def save(
        self,
        session_id: str,
        conversation: Conversation,
        ttl: int | None = None,
    ) -> None:
        effective_ttl = self.default_ttl if ttl is None else ttl
        if effective_ttl is not None and effective_ttl < 0:
            self._data.pop(session_id, None)
            return

        expires_at = (
            time.monotonic() + effective_ttl
            if effective_ttl is not None and effective_ttl > 0
            else None
        )
        self._data[session_id] = (conversation.to_json(), expires_at)
        self._data.move_to_end(session_id)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    async def load(self, session_id: str) -> Conversation:
        raw = self._get_valid_payload(session_id)
        if raw is None:
            return Conversation()
        try:
            return Conversation.from_json(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Corrupted in-memory conversation data: {exc}") from exc

    async def delete(self, session_id: str) -> bool:
        return self._data.pop(session_id, None) is not None

    async def exists(self, session_id: str) -> bool:
        return self._get_valid_payload(session_id) is not None


class RedisConversationStore(ConversationStore):
    """
    Redis-backed conversation store serializing dialogue history keyed by session/user ID.
    """

    def __init__(
        self,
        redis_client: _ConversationRedisClient | None = None,
        config: Config | None = None,
        key_prefix: str = "ai:conversation",
        default_ttl: int = 86400,
    ) -> None:
        self._client: _ConversationRedisClient | None = redis_client
        self._config = config
        self._key_prefix = key_prefix
        self.default_ttl = default_ttl
        self._owns_client = redis_client is None

    def _format_key(self, session_id: str) -> str:
        # Sanitize session_id to prevent injection or whitespace issues
        clean_id = session_id.strip()
        return f"{self._key_prefix}:{clean_id}"

    async def get_client(self) -> _ConversationRedisClient:
        """Lazily initialize Redis client if not explicitly provided."""
        if self._client is None:
            cfg = self._config or Config.get_instance()
            params = resolve_redis_params(cfg, section="ai")
            client: _ConversationRedisClient = aioredis.from_url(
                params["url"], decode_responses=True
            )
            self._client = client
            return client
        return self._client

    async def save(
        self,
        session_id: str,
        conversation: Conversation,
        ttl: int | None = None,
    ) -> None:
        """
        Serialize and persist a conversation into Redis.

        If ttl is None, self.default_ttl is used.
        If ttl == 0, the key is saved with no expiration.
        If ttl < 0, any existing key is removed immediately.
        """
        client = await self.get_client()
        key = self._format_key(session_id)
        effective_ttl = self.default_ttl if ttl is None else ttl
        payload = conversation.to_json()

        try:
            if effective_ttl < 0:
                await client.delete(key)
            elif effective_ttl > 0:
                await client.set(key, payload, ex=effective_ttl)
            else:
                await client.set(key, payload)
        except RedisError as exc:
            raise AIConnectionError(
                f"Failed to save conversation to Redis: {exc}",
                provider="redis",
            ) from exc

    async def load(self, session_id: str) -> Conversation:
        """Retrieve and deserialize a conversation from Redis. Returns empty Conversation if missing."""
        client = await self.get_client()
        key = self._format_key(session_id)

        try:
            raw = await client.get(key)
        except RedisError as exc:
            raise AIConnectionError(
                f"Failed to load conversation from Redis: {exc}",
                provider="redis",
            ) from exc

        if raw is None or not str(raw).strip():
            return Conversation()

        try:
            return Conversation.from_json(str(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Corrupted conversation data in Redis for key '{key}': {exc}") from exc

    async def delete(self, session_id: str) -> bool:
        """Remove a conversation from Redis."""
        client = await self.get_client()
        key = self._format_key(session_id)
        try:
            deleted_count = await client.delete(key)
            return bool(deleted_count > 0)
        except RedisError as exc:
            raise AIConnectionError(
                f"Failed to delete conversation from Redis: {exc}",
                provider="redis",
            ) from exc

    async def exists(self, session_id: str) -> bool:
        """Check whether a conversation exists in Redis."""
        client = await self.get_client()
        key = self._format_key(session_id)
        try:
            return bool(await client.exists(key))
        except RedisError as exc:
            raise AIConnectionError(
                f"Failed to check conversation existence in Redis: {exc}",
                provider="redis",
            ) from exc

    async def close(self) -> None:
        """Close the Redis client connection if owned by this store."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
