from __future__ import annotations

import time
from typing import Any

import redis.asyncio as aioredis

from swiftpy.helpers.helper import env


class TokenBlacklist:
    """
    Redis-backed token revocation list for JWT logout and invalidation.

    Keys are stored with a TTL matching the token's remaining lifespan,
    ensuring Redis automatically reclaims memory once the token would have
    expired normally.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        redis_url: str | None = None,
        prefix: str = "jwt_blacklist:",
        default_ttl: int = 604800,  # 7 days
    ) -> None:
        self._client = redis_client
        self._redis_url = redis_url
        self.prefix = prefix
        self.default_ttl = default_ttl
        self._owns_client = redis_client is None

    async def get_client(self) -> aioredis.Redis:
        """Lazily initialize and return the async Redis client."""
        if self._client is None:
            url = self._redis_url
            if not url:
                host = env("REDIS_HOST", "127.0.0.1")
                port = int(env("REDIS_PORT", "6379"))
                password = env("REDIS_PASSWORD", "")
                if password:
                    url = f"redis://:{password}@{host}:{port}/0"
                else:
                    url = f"redis://{host}:{port}/0"

            self._client = aioredis.from_url(url, decode_responses=True)
            self._owns_client = True

        return self._client

    async def revoke(self, jti: str, ttl: int | None = None) -> None:
        """
        Revoke a token by its unique JWT ID (jti) with an optional TTL in seconds.
        """
        if not jti:
            raise ValueError("JWT ID (jti) cannot be empty")

        client = await self.get_client()
        key = f"{self.prefix}{jti}"
        expiration = ttl if (ttl is not None and ttl > 0) else self.default_ttl

        await client.set(key, "revoked", ex=expiration)

    async def revoke_token(
        self,
        token: str,
        issuer: Any = None,
    ) -> None:
        """
        Revoke an issued JWT by parsing its jti and calculating the remaining TTL.
        """
        if issuer is not None and hasattr(issuer, "decode_token"):
            payload = issuer.decode_token(token, verify_exp=False)
        else:
            from swiftpy.auth.jwt_issuer import JWTIssuer

            payload = JWTIssuer.decode_unverified(token)

        jti = payload.get("jti")
        if not jti:
            raise ValueError("Token does not contain a 'jti' claim to revoke")

        exp = payload.get("exp")
        ttl: int | None = None
        if exp and isinstance(exp, (int, float)):
            remaining = int(exp - time.time())
            if remaining <= 0:
                # Token has already expired; naturally rejected by exp check, skip Redis allocation
                return
            ttl = remaining

        await self.revoke(jti, ttl=ttl)

    async def is_revoked(self, jti: str) -> bool:
        """
        Determine whether a JWT ID (jti) is listed in the revocation blacklist.
        """
        if not jti:
            return False

        client = await self.get_client()
        key = f"{self.prefix}{jti}"
        return bool(await client.exists(key))

    async def clear(self) -> None:
        """
        Remove all blacklisted tokens matching this prefix (primarily for tests/maintenance).
        """
        client = await self.get_client()
        match_pattern = f"{self.prefix}*"
        cursor = 0
        keys_to_delete: list[str] = []

        while True:
            cursor, keys = await client.scan(
                cursor=cursor, match=match_pattern, count=100
            )
            if keys:
                keys_to_delete.extend(
                    k.decode("utf-8") if isinstance(k, bytes) else str(k) for k in keys
                )
            if cursor == 0:
                break

        if keys_to_delete:
            await client.delete(*keys_to_delete)

    async def close(self) -> None:
        """Close the underlying Redis client connection pool."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
