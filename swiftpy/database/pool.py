from __future__ import annotations

import asyncpg

from swiftpy.core.config import Config


class DatabasePool:
    """Manages the application-wide asyncpg connection pool."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._pool: asyncpg.Pool | None = None

    async def init(self) -> asyncpg.Pool:
        """
        Initialize the asyncpg connection pool utilizing the injected Config repository.
        """
        if self._pool is None:
            default_conn = str(self.config.get("database.default", "postgresql"))
            conn_path = f"database.connections.{default_conn}"

            self._pool = await asyncpg.create_pool(
                host=str(self.config.get(f"{conn_path}.host", "127.0.0.1")),
                port=int(self.config.get(f"{conn_path}.port", 5432)),
                database=str(self.config.get(f"{conn_path}.database", "swiftpy")),
                user=str(self.config.get(f"{conn_path}.user", "postgres")),
                password=str(self.config.get(f"{conn_path}.password", "")),
                ssl=bool(self.config.get(f"{conn_path}.ssl", False)),
                min_size=int(self.config.get("database.pool.min_size", 5)),
                max_size=int(self.config.get("database.pool.max_size", 20)),
                max_queries=int(self.config.get("database.pool.max_queries", 50000)),
                max_inactive_connection_lifetime=float(
                    self.config.get(
                        "database.pool.max_inactive_connection_lifetime", 300.0
                    )
                ),
                command_timeout=float(
                    self.config.get("database.pool.command_timeout", 60.0)
                ),
                timeout=float(self.config.get("database.timeouts.connect", 10.0)),
            )
        return self._pool

    def get_pool(self) -> asyncpg.Pool:
        """Return the active connection pool instance."""
        if self._pool is None:
            raise RuntimeError(
                "Database pool is not initialized. Ensure it is initialized during application startup hooks."
            )
        return self._pool

    async def close(self) -> None:
        """Gracefully shutdown the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
