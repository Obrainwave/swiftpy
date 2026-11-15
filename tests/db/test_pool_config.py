from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest

from swiftpy.core.config import Config, ConfigurationError
from swiftpy.database.pool import DatabasePool, _build_ssl_context


def test_ssl_option_mapping(caplog: pytest.LogCaptureFixture) -> None:
    assert _build_ssl_context(False) is False
    assert _build_ssl_context(None) is False
    assert _build_ssl_context("disable") is False
    assert _build_ssl_context("false") is False

    with caplog.at_level(logging.WARNING, logger="swiftpy.database.pool"):
        ctx_true = _build_ssl_context(True)
    assert isinstance(ctx_true, ssl.SSLContext)
    assert ctx_true.verify_mode == ssl.CERT_NONE
    assert ctx_true.check_hostname is False
    assert "mapping to 'require'" in caplog.text

    ctx_req = _build_ssl_context("require")
    assert isinstance(ctx_req, ssl.SSLContext)
    assert ctx_req.verify_mode == ssl.CERT_NONE
    assert ctx_req.check_hostname is False

    ctx_ca = _build_ssl_context("verify-ca")
    assert isinstance(ctx_ca, ssl.SSLContext)
    assert ctx_ca.verify_mode == ssl.CERT_REQUIRED
    assert ctx_ca.check_hostname is False

    ctx_full = _build_ssl_context("verify-full")
    assert isinstance(ctx_full, ssl.SSLContext)
    assert ctx_full.verify_mode == ssl.CERT_REQUIRED
    assert ctx_full.check_hostname is True

    with pytest.raises(ConfigurationError, match="Unsupported database SSL mode"):
        _build_ssl_context("invalid-ssl-mode")


async def test_concurrent_init_creates_single_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0
    fake_pool = AsyncMock()

    async def slow_create_pool(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return fake_pool

    monkeypatch.setattr("swiftpy.database.pool.asyncpg.create_pool", slow_create_pool)

    cfg = Config(
        {
            "database": {
                "default": "postgresql",
                "connections": {
                    "postgresql": {
                        "host": "127.0.0.1",
                        "port": 5432,
                        "database": "swiftpy",
                        "user": "postgres",
                        "password": "secret",
                        "ssl": "disable",
                    }
                },
            }
        }
    )
    pool = DatabasePool(cfg)
    results = await asyncio.gather(pool.init(), pool.init(), pool.init())
    assert call_count == 1
    assert results[0] is fake_pool
    assert results[1] is fake_pool
    assert results[2] is fake_pool


async def test_statement_timeout_and_default_max_size_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict[str, Any] = {}
    fake_pool = AsyncMock()

    async def capture_create_pool(**kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return fake_pool

    monkeypatch.setattr(
        "swiftpy.database.pool.asyncpg.create_pool", capture_create_pool
    )

    cfg = Config(
        {
            "database": {
                "default": "postgresql",
                "connections": {
                    "postgresql": {
                        "host": "127.0.0.1",
                        "port": 5432,
                        "database": "swiftpy",
                        "user": "postgres",
                        "password": "secret",
                    }
                },
                "timeouts": {"statement": 15.0},
            }
        }
    )
    pool = DatabasePool(cfg)
    await pool.init()

    assert captured_kwargs["max_size"] == 20
    assert captured_kwargs["command_timeout"] == 60.0
    assert captured_kwargs["server_settings"] == {"statement_timeout": "15000"}


async def test_empty_password_in_production_fails_and_warns_in_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_pool = AsyncMock()

    async def noop_create_pool(**kwargs: Any) -> Any:
        return fake_pool

    monkeypatch.setattr("swiftpy.database.pool.asyncpg.create_pool", noop_create_pool)

    prod_cfg = Config(
        {
            "app": {"env": "production", "debug": False},
            "database": {
                "default": "postgresql",
                "connections": {
                    "postgresql": {
                        "host": "127.0.0.1",
                        "user": "postgres",
                        "password": "",
                    }
                },
            },
        }
    )
    prod_pool = DatabasePool(prod_cfg)
    with pytest.raises(ConfigurationError, match="empty password"):
        await prod_pool.init()

    debug_cfg = Config(
        {
            "app": {"env": "development", "debug": True},
            "database": {
                "default": "postgresql",
                "connections": {
                    "postgresql": {
                        "host": "127.0.0.1",
                        "user": "postgres",
                        "password": "",
                    }
                },
            },
        }
    )
    debug_pool = DatabasePool(debug_cfg)
    with caplog.at_level(logging.WARNING, logger="swiftpy.database.pool"):
        await debug_pool.init()
    assert "empty password" in caplog.text


async def test_close_with_inflight_connection_does_not_hang() -> None:
    cfg = Config.load()
    cfg.set("database.pool.min_size", 1)
    cfg.set("database.pool.max_size", 2)
    pool = DatabasePool(cfg)
    raw_pool = await pool.init()

    conn = await raw_pool.acquire()
    # With conn acquired and not released, raw_pool.close() would wait indefinitely.
    # pool.close(timeout=0.1) must time out cleanly and terminate the pool.
    await asyncio.wait_for(pool.close(timeout=0.1), timeout=1.0)
    assert pool._pool is None
    with pytest.raises(asyncpg.InterfaceError):
        await conn.fetchval("SELECT 1")
