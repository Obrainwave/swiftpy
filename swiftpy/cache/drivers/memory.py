from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from swiftpy.cache.driver import _SENTINEL, CacheDriver


class MemoryDriver(CacheDriver):
    """
    In-memory cache driver suitable for unit tests and local execution
    without requiring an external Redis instance.
    Includes LRU eviction when max_items is set and lazy expiry sweeping.
    """

    def __init__(
        self,
        prefix: str = "swiftpy_cache:",
        default_ttl: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_items: int | None = 1000,
    ) -> None:
        super().__init__()
        self.prefix = prefix
        self.default_ttl = default_ttl
        self._clock = clock
        self.max_items = max_items
        self._storage: OrderedDict[str, tuple[Any, float | None]] = OrderedDict()
        self._lock = asyncio.Lock()

    def _sweep_expired(self, max_keys: int = 20) -> None:
        """Lazily sweep up to max_keys expired entries."""
        now = self._clock()
        to_del: list[str] = []
        for checked, (k, (_, exp)) in enumerate(self._storage.items()):
            if exp is not None and now >= exp:
                to_del.append(k)
            if checked + 1 >= max_keys:
                break
        for k in to_del:
            self._storage.pop(k, None)

    def _prefixed_key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    async def get(self, key: str, default: Any = None) -> Any:
        full_key = self._prefixed_key(key)
        raw_val: Any = _SENTINEL
        async with self._lock:
            if full_key in self._storage:
                value, expires_at = self._storage[full_key]
                if expires_at is not None and self._clock() >= expires_at:
                    del self._storage[full_key]
                else:
                    self._storage.move_to_end(full_key)
                    raw_val = value

        if raw_val is _SENTINEL:
            return default
        return copy.deepcopy(raw_val)

    async def set(
        self,
        key: str,
        value: Any,
        ttl: float | None = None,
    ) -> bool:
        full_key = self._prefixed_key(key)
        effective_ttl = ttl if ttl is not None else self.default_ttl
        val_to_store = copy.deepcopy(value)

        async with self._lock:
            self._sweep_expired()
            if effective_ttl is not None and effective_ttl <= 0:
                self._storage.pop(full_key, None)
                return True

            expires_at = (
                self._clock() + effective_ttl if effective_ttl is not None else None
            )
            self._storage[full_key] = (val_to_store, expires_at)
            self._storage.move_to_end(full_key)
            if self.max_items is not None and len(self._storage) > self.max_items:
                self._storage.popitem(last=False)
            return True

    async def forget(self, key: str) -> bool:
        full_key = self._prefixed_key(key)
        async with self._lock:
            if full_key in self._storage:
                del self._storage[full_key]
                return True
            return False

    async def has(self, key: str) -> bool:
        return await self.get(key, _SENTINEL) is not _SENTINEL

    async def flush(self) -> bool:
        async with self._lock:
            if not self.prefix:
                self._storage.clear()
            else:
                keys_to_delete = [k for k in self._storage if k.startswith(self.prefix)]
                for k in keys_to_delete:
                    del self._storage[k]
            return True

    async def add(
        self,
        key: str,
        value: Any,
        ttl: float | None = None,
    ) -> bool:
        full_key = self._prefixed_key(key)
        effective_ttl = ttl if ttl is not None else self.default_ttl
        val_to_store = copy.deepcopy(value)

        async with self._lock:
            self._sweep_expired()
            if full_key in self._storage:
                _, expires_at = self._storage[full_key]
                if expires_at is None or self._clock() < expires_at:
                    return False
                # Expired key can be replaced
                del self._storage[full_key]

            if effective_ttl is not None and effective_ttl <= 0:
                return True

            expires_at = (
                self._clock() + effective_ttl if effective_ttl is not None else None
            )
            self._storage[full_key] = (val_to_store, expires_at)
            self._storage.move_to_end(full_key)
            if self.max_items is not None and len(self._storage) > self.max_items:
                self._storage.popitem(last=False)
            return True

    async def increment(
        self, key: str, value: int = 1, ttl: float | None = None
    ) -> int:
        full_key = self._prefixed_key(key)
        effective_ttl = ttl if ttl is not None else self.default_ttl
        async with self._lock:
            self._sweep_expired()
            current: Any = 0
            expires_at: float | None = None

            if full_key in self._storage:
                stored_val, stored_exp = self._storage[full_key]
                if stored_exp is None or self._clock() < stored_exp:
                    current = stored_val
                    expires_at = stored_exp
                else:
                    del self._storage[full_key]
                    if effective_ttl is not None and effective_ttl > 0:
                        expires_at = self._clock() + effective_ttl
            else:
                if effective_ttl is not None and effective_ttl > 0:
                    expires_at = self._clock() + effective_ttl

            try:
                new_value = int(current) + value
            except (ValueError, TypeError):
                raise TypeError(
                    f"Cannot increment non-numeric cached value of type {type(current).__name__}"
                )

            self._storage[full_key] = (new_value, expires_at)
            self._storage.move_to_end(full_key)
            if self.max_items is not None and len(self._storage) > self.max_items:
                self._storage.popitem(last=False)
            return new_value

    async def close(self) -> None:
        async with self._lock:
            self._storage.clear()
