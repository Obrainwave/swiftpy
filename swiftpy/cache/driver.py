from __future__ import annotations

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("swiftpy.cache")

_SENTINEL = object()


class _LockWithRef:
    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


class CacheDriver(ABC):
    """
    Abstract base class defining the contract for all SwiftPY cache drivers.
    """

    def __init__(self) -> None:
        self._remember_locks: dict[str, _LockWithRef] = {}
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

    async def increment(
        self, key: str, value: int = 1, ttl: float | None = None
    ) -> int:
        """
        Increment the value of an item in the cache.

        If the key does not exist, it is initialized to the given value with optional TTL.
        """
        current = await self.get(key, 0)
        try:
            new_value = int(current) + value
        except (ValueError, TypeError):
            raise TypeError(
                f"Cannot increment non-numeric cached value of type {type(current).__name__}"
            )
        await self.set(key, new_value, ttl)
        return new_value

    async def decrement(
        self, key: str, value: int = 1, ttl: float | None = None
    ) -> int:
        """
        Decrement the value of an item in the cache.
        """
        return await self.increment(key, -value, ttl)

    async def remember(
        self,
        key: str,
        ttl: float | None,
        callback: Callable[[], Any] | Callable[[], Awaitable[Any]],
        lock_timeout: float = 10.0,
    ) -> Any:
        """
        Get an item from the cache, or execute the given callback and store the result.

        Includes stampede protection via per-key locking so that concurrent
        requests for a missing key execute the callback exactly once.
        If lock acquisition times out, computes the value without caching and logs a warning.
        """
        # Fast path check
        cached = await self.get(key, _SENTINEL)
        if cached is not _SENTINEL:
            return cached

        # Stampede protection: acquire key-specific lock with reference counting
        async with self._meta_lock:
            if key not in self._remember_locks:
                self._remember_locks[key] = _LockWithRef()
            entry = self._remember_locks[key]
            entry.refs += 1

        acquired = False
        try:
            try:
                await asyncio.wait_for(entry.lock.acquire(), timeout=lock_timeout)
                acquired = True
            except TimeoutError:
                logger.warning(
                    "Cache lock timed out waiting for key '%s'; computing value directly without caching",
                    key,
                )
                if inspect.iscoroutinefunction(callback):
                    return await callback()
                res = callback()
                return await res if inspect.isawaitable(res) else res

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
        finally:
            if acquired:
                entry.lock.release()
            async with self._meta_lock:
                entry.refs -= 1
                if entry.refs <= 0:
                    self._remember_locks.pop(key, None)

    async def close(self) -> None:
        """
        Close any underlying resources, connections, or pools held by the driver.
        """
