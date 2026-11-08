from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from swiftpy.ai.conversation import Conversation
from swiftpy.ai.exceptions import AIConnectionError
from swiftpy.core.config import Config
from swiftpy.support.redis_config import resolve_redis_params


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
    """In-memory conversation store for testing and single-instance deployments."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._ttls: dict[str, int | None] = {}

    async def save(
        self,
        session_id: str,
        conversation: Conversation,
        ttl: int | None = None,
    ) -> None:
        self._data[session_id] = conversation.to_json()
        self._ttls[session_id] = ttl

    async def load(self, session_id: str) -> Conversation:
        raw = self._data.get(session_id)
        if raw is None:
            return Conversation()
        try:
            return Conversation.from_json(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Corrupted in-memory conversation data: {exc}") from exc

    async def delete(self, session_id: str) -> bool:
        if session_id in self._data:
            del self._data[session_id]
            self._ttls.pop(session_id, None)
            return True
        return False

    async def exists(self, session_id: str) -> bool:
        return session_id in self._data


class RedisConversationStore(ConversationStore):
    """
    Redis-backed conversation store serializing dialogue history keyed by session/user ID.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        config: Config | None = None,
        key_prefix: str = "ai:conversation",
        default_ttl: int = 86400,
    ) -> None:
        self._client = redis_client
        self._config = config
        self._key_prefix = key_prefix
        self.default_ttl = default_ttl
        self._owns_client = redis_client is None

    def _format_key(self, session_id: str) -> str:
        # Sanitize session_id to prevent injection or whitespace issues
        clean_id = session_id.strip()
        return f"{self._key_prefix}:{clean_id}"

    async def get_client(self) -> aioredis.Redis:
        """Lazily initialize Redis client if not explicitly provided."""
        if self._client is None:
            cfg = self._config or Config.get_instance()
            params = resolve_redis_params(cfg, section="ai")
            connection_kwargs: dict[str, Any] = {
                "host": params["host"],
                "port": params["port"],
                "decode_responses": True,
            }
            if params.get("password"):
                connection_kwargs["password"] = params["password"]
            if params.get("db") is not None:
                connection_kwargs["db"] = params["db"]

            self._client = aioredis.Redis(**connection_kwargs)
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
        """
        client = await self.get_client()
        key = self._format_key(session_id)
        effective_ttl = self.default_ttl if ttl is None else ttl
        payload = conversation.to_json()

        try:
            if effective_ttl > 0:
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
