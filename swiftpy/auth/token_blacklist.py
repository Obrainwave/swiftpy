from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, cast

import redis.asyncio as aioredis

from swiftpy.auth.exceptions import BaseTokenBlacklist, is_token_blacklist_enabled
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.core.config import Config
from swiftpy.core.container import Container, get_container
from swiftpy.support.redis_config import build_redis_url


class _BlacklistPipeline(Protocol):
    def exists(self, *names: str) -> Any: ...
    def get(self, name: str, /) -> Any: ...
    def execute(self) -> Awaitable[list[Any]]: ...


class _BlacklistRedisClient(Protocol):
    def pipeline(self) -> _BlacklistPipeline: ...
    def set(
        self, name: str, value: str, /, ex: int | None = None
    ) -> Awaitable[bool | str | bytes | None]: ...
    def scan(
        self,
        cursor: int = 0,
        match: str | None = None,
        count: int | None = None,
    ) -> Awaitable[tuple[int, Sequence[bytes | str]]]: ...
    def delete(self, *names: str) -> Awaitable[int]: ...
    def aclose(self) -> Awaitable[None]: ...


def _validate_positive_ttl(ttl: int | None) -> int | None:
    """Validate explicit blacklist TTL overrides so <= 0 never silently extends to 7 days."""
    if ttl is None:
        return None
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise ValueError("ttl must be a positive integer")
    return ttl


class TokenBlacklist(BaseTokenBlacklist):
    """
    Redis-backed token revocation list for JWT logout and invalidation (Decision D-1, P2-A3).

    Keys are stored with a TTL capped at min(exp - now, max_token_lifetime),
    ensuring Redis automatically reclaims memory once the token would have
    expired normally, while preventing attacker-controlled memory exhaustion.
    """

    def __init__(
        self,
        redis_client: _BlacklistRedisClient | None = None,
        redis_url: str | None = None,
        prefix: str = "jwt_blacklist:",
        default_ttl: int = 604800,  # 7 days
        max_token_lifetime: int = 604800,  # 7 days
        issuer: JWTIssuer | None = None,
        config: Config | None = None,
        container: Container | None = None,
    ) -> None:
        self._client: _BlacklistRedisClient | None = redis_client
        self._redis_url = redis_url
        self.prefix = prefix
        self.default_ttl = default_ttl
        self.max_token_lifetime = max_token_lifetime
        self._issuer = issuer
        self._config = config
        self._container = container
        self._owns_client = redis_client is None
        self._lock = asyncio.Lock()
        self._local_cache: dict[tuple[str, str, str | None, int | None], float] = {}
        self._local_cache_ttl: float = 2.0

    async def get_client(self) -> _BlacklistRedisClient:
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

                    created: _BlacklistRedisClient = aioredis.from_url(
                        url, decode_responses=True
                    )
                    self._client = created
                    self._owns_client = True

        assert self._client is not None
        return self._client

    async def revoke(self, jti: str, ttl: int | None = None) -> None:
        """
        Revoke a token by its unique JWT ID (jti) with an optional TTL in seconds.
        """
        if not jti:
            raise ValueError("JWT ID (jti) cannot be empty")

        validated_ttl = _validate_positive_ttl(ttl)
        client = await self.get_client()
        key = f"{self.prefix}{jti}"
        raw_ttl = validated_ttl if validated_ttl is not None else self.default_ttl
        expiration = min(raw_ttl, self.max_token_lifetime)

        self._local_cache.clear()
        await client.set(key, "revoked", ex=expiration)
        self._local_cache.clear()

    async def revoke_if_not_revoked(
        self,
        jti: str,
        ttl: int | None = None,
    ) -> bool:
        """
        Atomically revoke `jti` using Redis `SET ... NX`.
        Returns True if newly revoked (first claim), or False if already revoked.
        """
        if not jti:
            raise ValueError("JWT ID (jti) cannot be empty")

        validated_ttl = _validate_positive_ttl(ttl)
        client = await self.get_client()
        key = f"{self.prefix}{jti}"
        raw_ttl = validated_ttl if validated_ttl is not None else self.default_ttl
        expiration = min(raw_ttl, self.max_token_lifetime)

        self._local_cache.clear()
        set_fn = cast(Callable[..., Awaitable[Any]], client.set)
        try:
            res = await set_fn(key, "revoked", ex=expiration, nx=True)
        except TypeError:
            if await self.is_revoked(jti):
                return False
            await client.set(key, "revoked", ex=expiration)
            self._local_cache.clear()
            return True
        self._local_cache.clear()
        return bool(res)

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
        if (
            active_issuer is None
            and self._container is not None
            and self._container.has(JWTIssuer)
        ):
            active_issuer = self._container.resolve(JWTIssuer)
        if active_issuer is None:
            try:
                container = get_container()
                if container.has(JWTIssuer):
                    active_issuer = container.resolve(JWTIssuer)
            except RuntimeError:
                pass

        if active_issuer is None:
            raise ValueError(
                "JWTIssuer is required to cryptographically verify token signature before revocation."
            )

        # Cryptographically verify signature; exp and future nbf are handled below so future-active tokens can be pre-emptively revoked
        payload = active_issuer.decode_token(token, verify_exp=False, verify_nbf=False)

        jti = payload.get("jti")
        if not jti:
            raise ValueError("Token does not contain a 'jti' claim to revoke")

        exp = payload.get("exp")
        ttl: int
        if (
            exp is not None
            and not isinstance(exp, bool)
            and isinstance(exp, (int, float))
            and math.isfinite(exp)
        ):
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
        if subject is None or str(subject) == "":
            raise ValueError("Subject cannot be empty")

        validated_ttl = _validate_positive_ttl(ttl)
        client = await self.get_client()
        sub_key = f"{self.prefix}sub:{subject}"
        raw_ttl = (
            validated_ttl if validated_ttl is not None else self.max_token_lifetime
        )
        expiration = min(raw_ttl, self.max_token_lifetime)
        now = int(time.time())
        self._local_cache.clear()
        await client.set(sub_key, str(now), ex=expiration)
        self._local_cache.clear()

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
        norm_sub = (
            str(subject)
            if subject is not None and str(subject) != ""
            else None
        )
        if not jti and norm_sub is None:
            return False

        norm_iat = (
            int(issued_at)
            if issued_at is not None and not isinstance(issued_at, bool)
            else None
        )
        cache_key = (self.prefix, jti or "", norm_sub, norm_iat)
        now = time.monotonic()
        cached_ts = self._local_cache.get(cache_key)
        if cached_ts is not None and (now - cached_ts) < self._local_cache_ttl:
            return False

        client = await self.get_client()

        pipe = client.pipeline()
        has_jti = bool(jti)
        has_sub = norm_sub is not None

        if has_jti:
            pipe.exists(f"{self.prefix}{jti}")
        if has_sub:
            pipe.get(f"{self.prefix}sub:{norm_sub}")

        results = await pipe.execute()

        res_idx = 0
        if has_jti:
            if results[res_idx]:
                return True
            res_idx += 1

        if has_sub:
            sub_cutoff = results[res_idx]
            if sub_cutoff is not None:
                cutoff_ts = int(sub_cutoff)
                if norm_iat is None or norm_iat <= cutoff_ts:
                    return True

        if len(self._local_cache) > 10000:
            self._local_cache.clear()
        self._local_cache[cache_key] = now
        return False

    async def clear(self) -> None:
        """
        Remove all blacklisted tokens matching this prefix (primarily for tests/maintenance).
        """
        self._local_cache.clear()
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
        self._local_cache.clear()

    async def close(self) -> None:
        """Close the underlying Redis client connection pool."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


__all__ = ["TokenBlacklist", "is_token_blacklist_enabled"]
