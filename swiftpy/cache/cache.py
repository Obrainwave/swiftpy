from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis

from swiftpy.cache.driver import CacheDriver
from swiftpy.cache.drivers.memory import MemoryDriver
from swiftpy.cache.drivers.redis import RedisDriver
from swiftpy.core.config import Config
from swiftpy.support.redis_config import resolve_redis_params


class Cache:
    """
    Cache manager for SwiftPY applications.

    Provides a unified interface across caching backends (Redis, in-memory)
    with driver management, configuration integration, and stampede-safe caching.
    """

    def __init__(
        self,
        config: Config | None = None,
        default_driver: str | None = None,
    ) -> None:
        if config is None:
            try:
                self.config = Config.get_instance()
            except RuntimeError:
                self.config = Config()
        else:
            self.config = config

        self._default_driver_name = default_driver
        self._drivers: dict[str, CacheDriver] = {}
        self._custom_creators: dict[str, Callable[..., CacheDriver]] = {}
        self._shared_pools: dict[tuple[Any, ...], aioredis.ConnectionPool] = {}

    @property
    def default_driver_name(self) -> str:
        if self._default_driver_name:
            return self._default_driver_name
        return str(self.config.get("cache.default", "redis"))

    def extend(
        self,
        driver_name: str,
        factory: Callable[..., CacheDriver],
    ) -> None:
        """Register a custom cache driver creator."""
        self._custom_creators[driver_name] = factory

    def driver(self, name: str | None = None) -> CacheDriver:
        """
        Resolve a cache driver instance by name, or return the default driver.
        """
        driver_name = name or self.default_driver_name

        if driver_name in self._drivers:
            return self._drivers[driver_name]

        driver_instance = self._resolve_driver(driver_name)
        self._drivers[driver_name] = driver_instance
        return driver_instance

    def store(self, name: str | None = None) -> CacheDriver:
        """Alias for driver()."""
        return self.driver(name)

    def _build_store_prefix(self, store_config: dict[str, Any]) -> str:
        """Resolve cache store prefix consistently including optional app.name namespace."""
        app_name = str(self.config.get("app.name", "")).strip()
        raw_val = (
            store_config["prefix"]
            if "prefix" in store_config
            else self.config.get("cache.prefix", "swiftpy_cache:")
        )
        raw_prefix = str(raw_val) if raw_val is not None else "swiftpy_cache:"
        if (
            app_name
            and not raw_prefix.startswith(f"{app_name}:")
            and not raw_prefix.startswith(f"{app_name}_")
        ):
            return f"{app_name}:{raw_prefix}"
        return raw_prefix

    def _resolve_store_ttl(self, store_config: dict[str, Any]) -> float | None:
        """Resolve default TTL while distinguishing explicit None or 0.0 values."""
        default_ttl = (
            store_config["ttl"]
            if "ttl" in store_config
            else self.config.get("cache.default_ttl", None)
        )
        return float(default_ttl) if default_ttl is not None else None

    def _resolve_driver(self, name: str) -> CacheDriver:
        if name in self._custom_creators:
            creator = self._custom_creators[name]
            return creator()

        stores = self.config.get("cache.stores", {})
        store_config: dict[str, Any] = {}
        if isinstance(stores, dict) and name in stores:
            raw = stores[name]
            if isinstance(raw, dict):
                store_config = raw

        driver_type = store_config.get("driver", name)

        if driver_type == "memory":
            prefix = self._build_store_prefix(store_config)
            ttl_val = self._resolve_store_ttl(store_config)
            raw_max_items = (
                store_config["max_items"]
                if "max_items" in store_config
                else self.config.get("cache.max_items", 1000)
            )
            max_items = int(raw_max_items) if raw_max_items is not None else None
            return MemoryDriver(prefix=prefix, default_ttl=ttl_val, max_items=max_items)

        if driver_type == "redis":
            prefix = self._build_store_prefix(store_config)
            ttl_val = self._resolve_store_ttl(store_config)
            host = store_config.get("host")
            port = store_config.get("port")
            password = store_config.get("password")
            database = store_config.get("database", 0)
            redis_url = store_config.get(
                "url", self.config.get("cache.redis_url") or self.config.get("cache.url")
            )
            max_connections = int(
                store_config.get(
                    "max_connections", self.config.get("cache.max_connections", 50)
                )
            )
            timeout = float(
                store_config.get("timeout", self.config.get("cache.timeout", 30.0))
            )

            pool_key = (
                redis_url,
                str(host) if host is not None else None,
                int(port) if port is not None else None,
                str(password) if password is not None else None,
                int(database) if database is not None else 0,
                max_connections,
                timeout,
            )

            if pool_key not in self._shared_pools:
                if redis_url:
                    pool = aioredis.ConnectionPool.from_url(
                        redis_url,
                        decode_responses=True,
                        max_connections=max_connections,
                        timeout=timeout,
                    )
                else:
                    params = resolve_redis_params(
                        section="cache",
                        host=str(host) if host is not None else None,
                        port=int(port) if port is not None else None,
                        password=str(password) if password is not None else None,
                        database=int(database) if database is not None else 0,
                    )
                    pool = aioredis.BlockingConnectionPool(
                        host=params["host"],
                        port=params["port"],
                        db=params["database"],
                        password=params["password"] or None,
                        decode_responses=True,
                        max_connections=max_connections,
                        timeout=timeout,
                    )
                self._shared_pools[pool_key] = pool

            return RedisDriver(
                connection_pool=self._shared_pools[pool_key],
                prefix=prefix,
                default_ttl=ttl_val,
                max_connections=max_connections,
                timeout=timeout,
            )

        raise ValueError(f"Unsupported cache driver: '{name}'")

    async def get(self, key: str, default: Any = None) -> Any:
        return await self.driver().get(key, default)

    async def set(
        self,
        key: str,
        value: Any,
        ttl: float | None = None,
    ) -> bool:
        return await self.driver().set(key, value, ttl)

    async def remember(
        self,
        key: str,
        ttl: float | None,
        callback: Callable[[], Any] | Callable[[], Awaitable[Any]],
        lock_timeout: float = 10.0,
    ) -> Any:
        return await self.driver().remember(
            key, ttl, callback, lock_timeout=lock_timeout
        )

    async def forget(self, key: str) -> bool:
        return await self.driver().forget(key)

    async def has(self, key: str) -> bool:
        return await self.driver().has(key)

    async def flush(self) -> bool:
        return await self.driver().flush()

    async def pull(self, key: str, default: Any = None) -> Any:
        return await self.driver().pull(key, default)

    async def add(
        self,
        key: str,
        value: Any,
        ttl: float | None = None,
    ) -> bool:
        return await self.driver().add(key, value, ttl)

    async def increment(
        self, key: str, value: int = 1, ttl: float | None = None
    ) -> int:
        return await self.driver().increment(key, value, ttl=ttl)

    async def decrement(
        self, key: str, value: int = 1, ttl: float | None = None
    ) -> int:
        return await self.driver().decrement(key, value, ttl=ttl)

    async def close(self) -> None:
        for drv in self._drivers.values():
            await drv.close()
        self._drivers.clear()
        for pool in self._shared_pools.values():
            await pool.disconnect()
        self._shared_pools.clear()


# CacheManager is an alias for Cache for consistency across naming styles
CacheManager = Cache
