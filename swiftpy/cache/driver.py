from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

_SENTINEL = object()


class CacheDriver(ABC):
    """
    Abstract base class defining the contract for all SwiftPY cache drivers.
    """

    def __init__(self) -> None:
        self._remember_locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    @abstractmethod
    async def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieve an item from the cache by key.

        If the item does not exist or has expired, return the default value.
        """
        ...

    @abstractmethod
    async def set(
        self,
        key: str,
        value: Any,
        ttl: float | None = None,
    ) -> bool:
        """
        Store an item in the cache with an optional TTL in seconds.
        """
        ...

    @abstractmethod
    async def forget(self, key: str) -> bool:
        """
        Remove an item from the cache.

        Returns True if the item was removed, False if it did not exist.
        """
        ...

    @abstractmethod
    async def has(self, key: str) -> bool:
        """
        Determine if an item exists in the cache and has not expired.
        """
        ...

    @abstractmethod
    async def flush(self) -> bool:
        """
        Remove all items from the cache driver.
        """
        ...

    async def pull(self, key: str, default: Any = None) -> Any:
        """
        Retrieve an item from the cache and delete it.
        """
        value = await self.get(key, _SENTINEL)
        if value is _SENTINEL:
            return default
        await self.forget(key)
        return value

    async def add(
        self,
        key: str,
        value: Any,
        ttl: float | None = None,
    ) -> bool:
        """
        Store an item in the cache if the key does not already exist.

        Returns True if the item was added, False otherwise.
        """
        if await self.has(key):
            return False
        return await self.set(key, value, ttl)

    async def increment(self, key: str, value: int = 1) -> int:
        """
        Increment the value of an item in the cache.

        If the key does not exist, it is initialized to the given value.
        """
        current = await self.get(key, 0)
        try:
            new_value = int(current) + value
        except (ValueError, TypeError):
            raise TypeError(
                f"Cannot increment non-numeric cached value of type {type(current).__name__}"
            )
        await self.set(key, new_value)
        return new_value

    async def decrement(self, key: str, value: int = 1) -> int:
        """
        Decrement the value of an item in the cache.
        """
        return await self.increment(key, -value)

    async def remember(
        self,
        key: str,
        ttl: float | None,
        callback: Callable[[], Any] | Callable[[], Awaitable[Any]],
    ) -> Any:
        """
        Get an item from the cache, or execute the given callback and store the result.

        Includes stampede protection via per-key locking so that concurrent
        requests for a missing key execute the callback exactly once.
        """
        # Fast path check
        cached = await self.get(key, _SENTINEL)
        if cached is not _SENTINEL:
            return cached

        # Stampede protection: acquire key-specific lock
        async with self._meta_lock:
            if key not in self._remember_locks:
                self._remember_locks[key] = asyncio.Lock()
            lock = self._remember_locks[key]

        async with lock:
            # Double-checked locking
            cached = await self.get(key, _SENTINEL)
            if cached is not _SENTINEL:
                return cached

            # Execute callback (supporting both synchronous and asynchronous callables)
            if inspect.iscoroutinefunction(callback):
                result = await callback()
            else:
                res = callback()
                if inspect.isawaitable(res):
                    result = await res
                else:
                    result = res

            await self.set(key, result, ttl)
            return result

    async def close(self) -> None:
        """
        Close any underlying resources, connections, or pools held by the driver.
        """
