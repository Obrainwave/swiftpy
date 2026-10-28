from __future__ import annotations

import asyncio
import time

import redis.asyncio as aioredis

from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.core.config import Config
from swiftpy.support.redis_config import build_redis_url


class TokenBlacklist:
    """
    Redis-backed token revocation list for JWT logout and invalidation (Decision D-1, P2-A3).

    Keys are stored with a TTL capped at min(exp - now, max_token_lifetime),
    ensuring Redis automatically reclaims memory once the token would have
    expired normally, while preventing attacker-controlled memory exhaustion.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        redis_url: str | None = None,
        prefix: str = "jwt_blacklist:",
        default_ttl: int = 604800,  # 7 days
        max_token_lifetime: int = 604800,  # 7 days
        issuer: JWTIssuer | None = None,
        config: Config | None = None,
    ) -> None:
        self._client = redis_client
        self._redis_url = redis_url
        self.prefix = prefix
        self.default_ttl = default_ttl
        self.max_token_lifetime = max_token_lifetime
        self._issuer = issuer
        self._config = config
        self._owns_client = redis_client is None
        self._lock = asyncio.Lock()

    async def get_client(self) -> aioredis.Redis:
        """Lazily initialize and return the async Redis client safely under lock."""
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    url = self._redis_url
                    if not url:
                        cfg = self._config
                        if cfg is None:
                            try:
                                cfg = Config.get_instance()
                            except (RuntimeError, ValueError):
                                cfg = Config()
                        url = build_redis_url(cfg, section="auth")

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
        raw_ttl = ttl if (ttl is not None and ttl > 0) else self.default_ttl
        expiration = min(raw_ttl, self.max_token_lifetime)

        await client.set(key, "revoked", ex=expiration)

    async def revoke_token(
        self,
        token: str,
        issuer: JWTIssuer | None = None,
    ) -> None:
        """
        Revoke an issued JWT by verifying its signature and calculating the remaining TTL.
        Cryptographically verifies token signature before revoking to prevent
        attacker-chosen TTL / unbounded memory DoS (P2-A3).
        """
        active_issuer = issuer or self._issuer
        if active_issuer is None:
            try:
                active_issuer = JWTIssuer.from_config(self._config)
            except Exception as exc:
                raise ValueError(
                    "JWTIssuer is required to cryptographically verify token signature before revocation."
                ) from exc

        # Cryptographically verify signature (exp expiration is checked manually for TTL calculation)
        payload = active_issuer.decode_token(token, verify_exp=False)

        jti = payload.get("jti")
        if not jti:
            raise ValueError("Token does not contain a 'jti' claim to revoke")

        exp = payload.get("exp")
        ttl: int
        if exp is not None and isinstance(exp, (int, float)):
            remaining = int(exp - time.time())
            if remaining <= 0:
                # Token has already expired; naturally rejected by exp check, skip Redis allocation
                return
            ttl = min(remaining, self.max_token_lifetime)
        else:
            ttl = min(self.default_ttl, self.max_token_lifetime)

        await self.revoke(jti, ttl=ttl)

    async def revoke_all_for_subject(
        self,
        subject: str | int,
        ttl: int | None = None,
    ) -> None:
        """
        Revoke all tokens issued for a given subject (user_id) at or before the current timestamp.
        Used for logout-all, password reset, or account compromise (P2-A3).
        """
        if not subject:
            raise ValueError("Subject cannot be empty")

        client = await self.get_client()
        sub_key = f"{self.prefix}sub:{subject}"
        raw_ttl = ttl if (ttl is not None and ttl > 0) else self.max_token_lifetime
        expiration = min(raw_ttl, self.max_token_lifetime)
        now = int(time.time())
        await client.set(sub_key, str(now), ex=expiration)

    async def is_revoked(
        self,
        jti: str,
        subject: str | int | None = None,
        issued_at: int | None = None,
    ) -> bool:
        """
        Determine whether a JWT ID (jti) or subject has been revoked.
        O(1) check against individual jti and per-subject not_before timestamp.
        """
        if not jti and not subject:
            return False

        client = await self.get_client()

        if jti:
            key = f"{self.prefix}{jti}"
            if await client.exists(key):
                return True

        if subject is not None:
            sub_key = f"{self.prefix}sub:{subject}"
            sub_cutoff = await client.get(sub_key)
            if sub_cutoff is not None:
                cutoff_ts = int(sub_cutoff)
                if issued_at is None or issued_at <= cutoff_ts:
                    return True

        return False

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
