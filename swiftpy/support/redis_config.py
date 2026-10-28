"""
Centralized Redis connection configuration resolver.

Guarantees single-source-of-truth configuration resolution through Config,
with proper URL-encoding of user credentials and consistent defaults.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from swiftpy.core.config import Config


def _first_not_none(*values: Any) -> Any:
    """Return the first value that is not None."""
    for val in values:
        if val is not None:
            return val
    return None


def _resolve_section_dict(cfg: Config, section: str) -> dict[str, Any]:
    """Retrieve dictionary for the given section or known subsystem fallback."""
    # Subsystem-specific structures
    if section in ("cache", "cache.redis", "cache.stores.redis"):
        stores = cfg.get("cache.stores")
        if isinstance(stores, dict):
            redis_store = stores.get("redis")
            if isinstance(redis_store, dict):
                return {str(k): v for k, v in redis_store.items()}
        cache_redis = cfg.get("cache.redis")
        if isinstance(cache_redis, dict):
            return {str(k): v for k, v in cache_redis.items()}

    if section in ("queue", "queue.redis", "queue.connections.redis"):
        connections = cfg.get("queue.connections")
        if isinstance(connections, dict):
            redis_conn = connections.get("redis")
            if isinstance(redis_conn, dict):
                return {str(k): v for k, v in redis_conn.items()}
        queue_redis = cfg.get("queue.redis")
        if isinstance(queue_redis, dict):
            return {str(k): v for k, v in queue_redis.items()}

    if section in ("events", "events.redis"):
        events_redis = cfg.get("events.redis")
        if isinstance(events_redis, dict):
            return {str(k): v for k, v in events_redis.items()}

    val = cfg.get(section)
    if isinstance(val, dict):
        val_redis = val.get("redis")
        if isinstance(val_redis, dict):
            return {str(k): v for k, v in val_redis.items()}
        return {str(k): v for k, v in val.items()}

    return {}


def resolve_redis_params(
    config: Config | None = None,
    section: str = "redis",
    *,
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    database: int | None = None,
    username: str | None = None,
    ssl: bool | None = None,
) -> dict[str, Any]:
    """
    Resolve Redis connection parameters from Config and keyword overrides.

    Reads only through Config (never raw os.getenv in runtime callers),
    ensuring deterministic precedence and environment variable integration.
    """
    if config is None:
        try:
            cfg = Config.get_instance()
        except (RuntimeError, ValueError):
            cfg = Config()
    else:
        cfg = config

    # Check for direct URL in section or sub-path
    section_dict = _resolve_section_dict(cfg, section)
    explicit_url = _first_not_none(
        section_dict.get("url"),
        cfg.get(f"{section}.url"),
        cfg.get(f"{section}.redis_url"),
        cfg.get("redis.url"),
        cfg.get("redis_url"),
    )

    # Resolve individual components
    cfg_host = _first_not_none(
        section_dict.get("host"),
        cfg.get(f"{section}.host"),
        cfg.get("redis.host"),
        cfg.get("redis_host"),
    )
    cfg_port = _first_not_none(
        section_dict.get("port"),
        cfg.get(f"{section}.port"),
        cfg.get("redis.port"),
        cfg.get("redis_port"),
    )
    cfg_password = (
        section_dict.get("password")
        if "password" in section_dict
        else _first_not_none(
            cfg.get(f"{section}.password"),
            cfg.get("redis.password"),
            cfg.get("redis_password"),
        )
    )

    # Per-subsystem database fallback
    subsystem_db = None
    if section in ("queue", "queue.redis", "queue.connections.redis"):
        subsystem_db = _first_not_none(cfg.get("redis_queue_db"), cfg.get("redis.queue_db"))
    elif section in ("cache", "cache.redis", "cache.stores.redis"):
        subsystem_db = _first_not_none(cfg.get("redis_cache_db"), cfg.get("redis.cache_db"))
    elif section in ("events", "events.redis"):
        subsystem_db = _first_not_none(cfg.get("redis_events_db"), cfg.get("redis.events_db"))

    cfg_database = (
        section_dict.get("database")
        if "database" in section_dict
        else _first_not_none(
            subsystem_db,
            cfg.get(f"{section}.database"),
            cfg.get("redis.database"),
            cfg.get("redis_db"),
        )
    )

    cfg_username = _first_not_none(
        section_dict.get("username"),
        cfg.get(f"{section}.username"),
        cfg.get("redis.username"),
    )
    cfg_ssl = (
        section_dict.get("ssl")
        if "ssl" in section_dict
        else _first_not_none(cfg.get(f"{section}.ssl"), cfg.get("redis.ssl"))
    )

    # Apply overrides over config
    final_host = host if host is not None else (str(cfg_host) if cfg_host is not None else "127.0.0.1")
    final_port = port if port is not None else (int(cfg_port) if cfg_port is not None else 6379)
    final_password = password if password is not None else (str(cfg_password) if cfg_password is not None else None)
    final_database = database if database is not None else (int(cfg_database) if cfg_database is not None else 0)
    final_username = username if username is not None else (str(cfg_username) if cfg_username is not None else None)
    final_ssl = ssl if ssl is not None else (bool(cfg_ssl) if cfg_ssl is not None else False)

    # If an explicit URL was configured and no individual component override was provided, use it
    has_overrides = any(
        arg is not None
        for arg in (host, port, password, database, username, ssl)
    )
    if explicit_url and not has_overrides:
        return {
            "host": final_host,
            "port": final_port,
            "password": final_password,
            "database": final_database,
            "username": final_username,
            "ssl": final_ssl,
            "url": str(explicit_url),
        }

    # Construct the percent-encoded URL
    scheme = "rediss" if final_ssl else "redis"
    userinfo = ""
    if final_username is not None and final_password is not None:
        userinfo = f"{quote(final_username, safe='')}:{quote(final_password, safe='')}@"
    elif final_password is not None:
        userinfo = f":{quote(final_password, safe='')}@"
    elif final_username is not None:
        userinfo = f"{quote(final_username, safe='')}@"

    url = f"{scheme}://{userinfo}{final_host}:{final_port}/{final_database}"

    return {
        "host": final_host,
        "port": final_port,
        "password": final_password,
        "database": final_database,
        "username": final_username,
        "ssl": final_ssl,
        "url": url,
    }


def build_redis_url(
    config: Config | None = None,
    section: str = "redis",
    *,
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    database: int | None = None,
    username: str | None = None,
    ssl: bool | None = None,
) -> str:
    """Build a sanitized and credential-encoded Redis connection URL."""
    params = resolve_redis_params(
        config=config,
        section=section,
        host=host,
        port=port,
        password=password,
        database=database,
        username=username,
        ssl=ssl,
    )
    return str(params["url"])


__all__ = ["build_redis_url", "resolve_redis_params"]
