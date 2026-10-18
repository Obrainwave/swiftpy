from __future__ import annotations

import asyncio
import time
from typing import Self

import asyncpg

# Deterministic 64-bit signed integer key: 'SWIFTPY' in hex -> 0x53574946545059
DEFAULT_MIGRATION_LOCK_KEY: int = 0x53574946545059


class MigrationLockError(RuntimeError):
    """Raised when an advisory lock cannot be acquired or timed out."""


class MigrationLock:
    """
    PostgreSQL advisory lock manager.

    Prevents concurrent migration runs across distributed workers, processes,
    or containers using native pg_try_advisory_lock / pg_advisory_unlock.
    """

    def __init__(
        self,
        connection: asyncpg.Connection,
        key: int = DEFAULT_MIGRATION_LOCK_KEY,
        timeout: float = 30.0,
        poll_interval: float = 0.2,
    ) -> None:
        self.connection: asyncpg.Connection = connection
        self.key: int = key
        self.timeout: float = timeout
        self.poll_interval: float = poll_interval
        self.is_locked: bool = False

    async def acquire(self) -> bool:
        """
        Attempt to acquire the advisory lock, polling up to the specified timeout.
        Raises MigrationLockError if the lock cannot be acquired within the timeout window.
        """
        start = time.monotonic()

        while True:
            # pg_try_advisory_lock returns boolean immediately without blocking PostgreSQL backend
            locked: bool = await self.connection.fetchval(
                "SELECT pg_try_advisory_lock($1);", self.key
            )

            if locked:
                self.is_locked = True
                return True

            elapsed = time.monotonic() - start
            if elapsed >= self.timeout:
                raise MigrationLockError(
                    f"Failed to acquire migration advisory lock (key={hex(self.key)}) "
                    f"after {self.timeout:.1f}s. Another migration process is currently active."
                )

            await asyncio.sleep(self.poll_interval)

    async def release(self) -> bool:
        """Release the PostgreSQL advisory lock."""
        if not self.is_locked:
            return False

        unlocked: bool = await self.connection.fetchval(
            "SELECT pg_advisory_unlock($1);", self.key
        )
        self.is_locked = False
        return unlocked

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.release()
