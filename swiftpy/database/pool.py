from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

import asyncpg

from swiftpy.core.config import Config, ConfigurationError

logger = logging.getLogger("swiftpy.database.pool")


def _build_ssl_context(
    ssl_opt: Any,
    ssl_ca_file: str | None = None,
) -> ssl.SSLContext | bool:
    """
    Build an ssl.SSLContext or boolean flag for asyncpg from configuration.
    Supported modes: disable, require, verify-ca, verify-full.
    Boolean True (or string 'true') maps to 'require' with a warning for backward compatibility.
    """
    if ssl_opt is False or ssl_opt is None:
        return False

    cafile = ssl_ca_file if ssl_ca_file else None

    if ssl_opt is True:
        logger.warning(
            "Database SSL configured with boolean True; mapping to 'require' "
            "(encrypted connection without certificate verification). "
            "Prefer explicit 'verify-full' or 'verify-ca' in production."
        )
        ctx = ssl.create_default_context(cafile=cafile)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    if isinstance(ssl_opt, ssl.SSLContext):
        return ssl_opt

    if isinstance(ssl_opt, str):
        mode = ssl_opt.strip().lower()
        if mode in ("", "disable", "false", "0", "off", "no"):
            return False
        if mode in ("true", "1", "yes", "on"):
            logger.warning(
                "Database SSL configured with boolean string %r; mapping to 'require' "
                "(encrypted connection without certificate verification). "
                "Prefer explicit 'verify-full' or 'verify-ca' in production.",
                ssl_opt,
            )
            ctx = ssl.create_default_context(cafile=cafile)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if mode == "require":
            ctx = ssl.create_default_context(cafile=cafile)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if mode == "verify-ca":
            ctx = ssl.create_default_context(cafile=cafile)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_REQUIRED
            return ctx
        if mode == "verify-full":
            ctx = ssl.create_default_context(cafile=cafile)
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            return ctx

    raise ConfigurationError(
        f"Unsupported database SSL mode: {ssl_opt!r}. "
        "Expected one of: disable, require, verify-ca, verify-full."
    )


class DatabasePool:
    """Manages the application-wide asyncpg connection pool."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._pool: asyncpg.Pool | None = None
        self._init_lock = asyncio.Lock()

    async def init(self) -> asyncpg.Pool:
        """
        Initialize the asyncpg connection pool utilizing the injected Config repository.
        Idempotent and safe under concurrent calls.
        """
        if self._pool is not None:
            return self._pool

        async with self._init_lock:
            if self._pool is not None:
                return self._pool

            default_conn = str(self.config.get("database.default", "postgresql"))
            conn_path = f"database.connections.{default_conn}"

            user = str(self.config.get(f"{conn_path}.user", "postgres"))
            raw_password = self.config.get(f"{conn_path}.password", "")
            password = "" if raw_password is None else str(raw_password)

            app_env = str(
                self.config.get("app.env") or self.config.get("env") or "development"
            ).lower()
            debug_val = (
                self.config.get("app.debug")
                if self.config.get("app.debug") is not None
                else self.config.get("debug")
            )
            is_production = app_env in ("production", "prod")
            is_debug = bool(debug_val) if debug_val is not None else not is_production

            if not password:
                if is_production or not is_debug:
                    raise ConfigurationError(
                        f"Database connection '{default_conn}' has an empty password "
                        "while running in non-debug/production mode. Set DB_PASSWORD explicitly."
                    )
                logger.warning(
                    "Database connection '%s' (user=%r) is using an empty password in debug mode.",
                    default_conn,
                    user,
                )

            ca_file_raw = self.config.get(
                f"{conn_path}.ssl_ca_file"
            ) or self.config.get("database.ssl_ca_file")
            ssl_ca_file = str(ca_file_raw).strip() if ca_file_raw else None
            ssl_arg = _build_ssl_context(
                self.config.get(f"{conn_path}.ssl", "disable"),
                ssl_ca_file=ssl_ca_file,
            )

            statement_timeout = float(
                self.config.get("database.timeouts.statement", 30.0)
            )
            server_settings: dict[str, str] | None = None
            if statement_timeout > 0:
                server_settings = {
                    "statement_timeout": str(int(statement_timeout * 1000))
                }

            self._pool = await asyncpg.create_pool(
                host=str(self.config.get(f"{conn_path}.host", "127.0.0.1")),
                port=int(self.config.get(f"{conn_path}.port", 5432)),
                database=str(self.config.get(f"{conn_path}.database", "swiftpy")),
                user=user,
                password=password,
                ssl=ssl_arg,
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
                server_settings=server_settings,
            )
            return self._pool

    def get_pool(self) -> asyncpg.Pool:
        """Return the active connection pool instance."""
        if self._pool is None:
            raise RuntimeError(
                "Database pool is not initialized. Ensure it is initialized during application startup hooks."
            )
        return self._pool

    async def close(self, timeout: float = 5.0) -> None:
        """Gracefully shutdown the connection pool with a timeout fallback to terminate()."""
        if self._pool is not None:
            pool = self._pool
            self._pool = None
            try:
                await asyncio.wait_for(pool.close(), timeout=timeout)
            except TimeoutError:
                logger.warning(
                    "Database pool close timed out after %.2fs; terminating active connections.",
                    timeout,
                )
                pool.terminate()


__all__ = ["DatabasePool", "_build_ssl_context"]
