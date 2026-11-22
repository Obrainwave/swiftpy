from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Any
from weakref import WeakKeyDictionary

from redis.exceptions import RedisError

from swiftpy.cache.cache import Cache
from swiftpy.cache.driver import CacheDriver
from swiftpy.cache.drivers.memory import MemoryDriver
from swiftpy.core.config import Config
from swiftpy.core.container import Container, get_container

logger = logging.getLogger("swiftpy.auth.throttle")

_CONTAINER_FALLBACK_DRIVERS: WeakKeyDictionary[Container, MemoryDriver] = (
    WeakKeyDictionary()
)


def _normalize_identifier(identifier: str) -> str:
    return identifier.strip().lower()


def _normalize_ip(ip: str | None) -> str:
    if not ip:
        return "unknown"
    cleaned = ip.strip().lower()
    return cleaned or "unknown"


class LoginThrottle:
    """
    Cache-backed login rate limiter enforcing failed-attempt thresholds and
    temporary lockouts per normalized identifier + client IP pair.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        max_attempts: int | None = None,
        decay_seconds: int | None = None,
        lockout_seconds: int | None = None,
        prefix: str | None = None,
        store: str | None = None,
        driver: CacheDriver | None = None,
    ) -> None:
        self._explicit_enabled = enabled
        self._explicit_max_attempts = max_attempts
        self._explicit_decay_seconds = decay_seconds
        self._explicit_lockout_seconds = lockout_seconds
        self._explicit_prefix = prefix
        self._store = store
        self._driver = driver
        self._instance_fallback = MemoryDriver(prefix="swiftpy_throttle:")

    @staticmethod
    def _get_config() -> Config | None:
        try:
            container = get_container()
            if container.has(Config):
                return container.resolve(Config)
        except RuntimeError:
            pass
        try:
            return Config.get_instance()
        except RuntimeError:
            return None

    @property
    def enabled(self) -> bool:
        if self._explicit_enabled is not None:
            return self._explicit_enabled
        cfg = self._get_config()
        if cfg is not None:
            raw = cfg.get("auth.throttle.enabled", True)
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.strip().lower() in {"1", "true", "yes", "on"}
            return bool(raw)
        return True

    @property
    def max_attempts(self) -> int:
        if self._explicit_max_attempts is not None:
            return max(1, int(self._explicit_max_attempts))
        cfg = self._get_config()
        if cfg is not None:
            val = cfg.get("auth.throttle.max_attempts", 5)
            return max(1, int(val))
        return 5

    @property
    def decay_seconds(self) -> int:
        if self._explicit_decay_seconds is not None:
            return max(1, int(self._explicit_decay_seconds))
        cfg = self._get_config()
        if cfg is not None:
            val = cfg.get("auth.throttle.decay_seconds", 60)
            return max(1, int(val))
        return 60

    @property
    def lockout_seconds(self) -> int:
        if self._explicit_lockout_seconds is not None:
            return max(1, int(self._explicit_lockout_seconds))
        cfg = self._get_config()
        if cfg is not None:
            val = cfg.get("auth.throttle.lockout_seconds", 60)
            return max(1, int(val))
        return 60

    @property
    def prefix(self) -> str:
        if self._explicit_prefix is not None:
            return self._explicit_prefix
        cfg = self._get_config()
        if cfg is not None:
            raw = cfg.get("auth.throttle.prefix", "auth:throttle")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        return "auth:throttle"

    def make_key(self, identifier: str, ip: str | None = None) -> str:
        """Build a deterministic SHA-256 cache key for an identifier + client IP pair."""
        norm_id = _normalize_identifier(identifier)
        norm_ip = _normalize_ip(ip)
        digest = hashlib.sha256(f"{norm_id}|{norm_ip}".encode()).hexdigest()
        return f"{self.prefix}:{digest}"

    def _lockout_key(self, base_key: str) -> str:
        return f"{base_key}:lockout"

    def _generation_key(self, base_key: str) -> str:
        return f"{base_key}:gen"

    def _attempt_key(self, base_key: str, generation: int) -> str:
        return f"{base_key}:g{max(0, generation)}"

    def _fallback_driver(self) -> MemoryDriver:
        try:
            container = get_container()
        except RuntimeError:
            return self._instance_fallback
        driver = _CONTAINER_FALLBACK_DRIVERS.get(container)
        if driver is None:
            driver = MemoryDriver(prefix="swiftpy_throttle:")
            _CONTAINER_FALLBACK_DRIVERS[container] = driver
        return driver

    def _resolve_driver(self) -> CacheDriver:
        if self._driver is not None:
            return self._driver

        try:
            container = get_container()
        except RuntimeError:
            return self._instance_fallback

        if container.has(Cache):
            try:
                cache = container.resolve(Cache)
                store_name = self._store
                if store_name is None:
                    cfg = self._get_config()
                    if cfg is not None:
                        configured_store = cfg.get("auth.throttle.store", None)
                        if isinstance(configured_store, str) and configured_store.strip():
                            store_name = configured_store.strip()
                return cache.driver(store_name)
            except (ConnectionError, TimeoutError, OSError, RedisError) as exc:
                logger.warning(
                    "Falling back to in-memory throttle driver because Cache resolution failed: %s",
                    exc,
                )
        return self._fallback_driver()

    async def _driver_get(self, key: str) -> Any:
        driver = self._resolve_driver()
        try:
            return await driver.get(key)
        except (ConnectionError, TimeoutError, OSError, RedisError) as exc:
            logger.warning(
                "Cache backend unavailable during throttle read; falling back to memory: %s",
                exc,
            )
            return await self._fallback_driver().get(key)

    async def _driver_add(self, key: str, value: Any, ttl: float) -> bool:
        driver = self._resolve_driver()
        try:
            return await driver.add(key, value, ttl=ttl)
        except (ConnectionError, TimeoutError, OSError, RedisError) as exc:
            logger.warning(
                "Cache backend unavailable during throttle add; falling back to memory: %s",
                exc,
            )
            return await self._fallback_driver().add(key, value, ttl=ttl)

    async def _driver_increment(self, key: str, ttl: float) -> int:
        driver = self._resolve_driver()
        try:
            return await driver.increment(key, 1, ttl=ttl)
        except (ConnectionError, TimeoutError, OSError, RedisError) as exc:
            logger.warning(
                "Cache backend unavailable during throttle increment; falling back to memory: %s",
                exc,
            )
            return await self._fallback_driver().increment(key, 1, ttl=ttl)

    async def _driver_increment_persistent(self, key: str) -> int:
        driver = self._resolve_driver()
        try:
            return await driver.increment(key, 1, ttl=None)
        except (ConnectionError, TimeoutError, OSError, RedisError) as exc:
            logger.warning(
                "Cache backend unavailable during generation increment; falling back to memory: %s",
                exc,
            )
            return await self._fallback_driver().increment(key, 1, ttl=None)

    async def _driver_forget(self, key: str) -> bool:
        driver = self._resolve_driver()
        try:
            return await driver.forget(key)
        except (ConnectionError, TimeoutError, OSError, RedisError) as exc:
            logger.warning(
                "Cache backend unavailable during throttle clear; falling back to memory: %s",
                exc,
            )
            return await self._fallback_driver().forget(key)

    async def _remaining_lockout_seconds(self, base_key: str) -> int:
        raw_until = await self._driver_get(self._lockout_key(base_key))
        if raw_until is None:
            return 0
        try:
            until_ts = float(raw_until)
        except (TypeError, ValueError):
            return 0
        remaining = until_ts - time.time()
        if remaining <= 0:
            return 0
        return max(1, math.ceil(remaining))

    async def too_many_attempts(self, identifier: str, ip: str | None = None) -> bool:
        """Return True if the identifier + IP pair is currently locked out."""
        if not self.enabled:
            return False
        base_key = self.make_key(identifier, ip)
        return (await self._remaining_lockout_seconds(base_key)) > 0

    async def available_in(self, identifier: str, ip: str | None = None) -> int:
        """Return remaining lockout duration in seconds (0 when not locked out)."""
        if not self.enabled:
            return 0
        base_key = self.make_key(identifier, ip)
        return await self._remaining_lockout_seconds(base_key)

    async def _current_generation(self, base_key: str) -> int:
        raw_gen = await self._driver_get(self._generation_key(base_key))
        try:
            return max(0, int(raw_gen or 0))
        except (TypeError, ValueError):
            return 0

    async def attempts(self, identifier: str, ip: str | None = None) -> int:
        """Return total recorded failed attempts in the active window (or locked window)."""
        if not self.enabled:
            return 0
        base_key = self.make_key(identifier, ip)
        gen = await self._current_generation(base_key)
        locked = (await self._remaining_lockout_seconds(base_key)) > 0
        target_gen = max(0, gen - 1) if locked and gen > 0 else gen
        raw_count = await self._driver_get(self._attempt_key(base_key, target_gen))
        try:
            used = max(0, int(raw_count or 0))
        except (TypeError, ValueError):
            used = 0
        if locked and used < self.max_attempts:
            return self.max_attempts
        return used

    async def retries_left(self, identifier: str, ip: str | None = None) -> int:
        """Return how many failed login attempts remain before lockout."""
        if not self.enabled:
            return self.max_attempts
        base_key = self.make_key(identifier, ip)
        if (await self._remaining_lockout_seconds(base_key)) > 0:
            return 0
        gen = await self._current_generation(base_key)
        raw_count = await self._driver_get(self._attempt_key(base_key, gen))
        used = int(raw_count or 0)
        return max(0, self.max_attempts - used)

    async def hit(self, identifier: str, ip: str | None = None) -> int:
        """
        Increment the failed attempt counter and trigger lockout if ``max_attempts`` is reached.
        Returns the updated attempt count within the active window.
        """
        if not self.enabled:
            return 0
        base_key = self.make_key(identifier, ip)
        if (await self._remaining_lockout_seconds(base_key)) > 0:
            return await self.attempts(identifier, ip)

        gen = await self._current_generation(base_key)
        count = await self._driver_increment(
            self._attempt_key(base_key, gen),
            ttl=float(self.decay_seconds),
        )
        if count >= self.max_attempts:
            lock_until = time.time() + float(self.lockout_seconds)
            acquired_lock = await self._driver_add(
                self._lockout_key(base_key),
                lock_until,
                ttl=float(self.lockout_seconds),
            )
            if acquired_lock:
                await self._driver_increment_persistent(self._generation_key(base_key))
        return count

    async def clear(self, identifier: str, ip: str | None = None) -> None:
        """Reset recorded failed attempts and any active lockout for an identifier + IP."""
        base_key = self.make_key(identifier, ip)
        gen = await self._current_generation(base_key)
        await self._driver_forget(self._attempt_key(base_key, gen))
        if gen > 0:
            await self._driver_forget(self._attempt_key(base_key, gen - 1))
        await self._driver_forget(self._generation_key(base_key))
        await self._driver_forget(self._lockout_key(base_key))


__all__ = ["LoginThrottle"]
