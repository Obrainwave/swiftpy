from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from swiftpy.auth.api_key_guard import (
    ApiKeyGuard,
    generate_api_key,
    get_server_pepper,
    hash_api_key,
    require_api_key,
    verify_api_key_hash,
)
from swiftpy.auth.exceptions import InvalidApiKeyError
from swiftpy.core.application import Application
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container, set_container
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import db
from swiftpy.http.request import Request


def test_generate_api_key() -> None:
    full_key, prefix, secret = generate_api_key(prefix="sk_live_")
    assert "." in full_key
    assert full_key.startswith("sk_live_")
    p, s = full_key.split(".", 1)
    assert p == prefix
    assert s == secret
    assert len(secret) >= 32


def test_hash_api_key_with_pepper() -> None:
    pepper = "server-pepper-secret-32-chars-long!"
    secret = "random_secret_token_value_12345"
    h1 = hash_api_key(secret, pepper=pepper)
    h2 = hash_api_key(secret, pepper=pepper)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex length
    assert verify_api_key_hash(secret, h1, pepper=pepper)
    assert not verify_api_key_hash("wrong_secret", h1, pepper=pepper)


async def _dummy_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _make_request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        scope={
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
        },
        receive=_dummy_receive,
    )


def test_missing_pepper_in_production_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config({
        "app": {"debug": False, "env": "production"},
        "auth": {"guards": {"api_key": {"pepper": None}}},
    })
    monkeypatch.delenv("AUTH_KEY_PEPPER", raising=False)
    monkeypatch.delenv("API_KEY_PEPPER", raising=False)
    with pytest.raises(ConfigurationError, match=r"(?i)pepper"):
        get_server_pepper(cfg)


def test_short_pepper_rejected_everywhere() -> None:
    cfg = Config({
        "app": {"debug": True},
        "auth": {"guards": {"api_key": {"pepper": "too-short-pepper"}}},
    })
    with pytest.raises(ConfigurationError, match=r"(?i)at least 32 bytes"):
        get_server_pepper(cfg)

    with pytest.raises(ConfigurationError, match=r"(?i)at least 32 bytes"):
        hash_api_key("secret", pepper="short")

    with pytest.raises(ConfigurationError, match=r"(?i)at least 32 bytes"):
        verify_api_key_hash("secret", "0" * 64, pepper=b"short")

    with pytest.raises(ConfigurationError, match=r"(?i)at least 32 bytes"):
        ApiKeyGuard(pepper="short-pepper")


def test_application_boot_missing_or_short_pepper_non_debug_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_KEY_PEPPER", raising=False)
    monkeypatch.delenv("API_KEY_PEPPER", raising=False)
    app_missing = Application(
        debug=False,
        config={
            "app": {"debug": False, "env": "production"},
            "auth": {
                "defaults": {"guard": "api_key"},
                "guards": {"api_key": {"pepper": None}},
            },
        },
    )
    with pytest.raises(ConfigurationError, match=r"(?i)AUTH_KEY_PEPPER"):
        app_missing.boot()

    app_short = Application(
        debug=False,
        config={
            "app": {"debug": False, "env": "production"},
            "auth": {
                "defaults": {"guard": "api_key"},
                "guards": {"api_key": {"pepper": "short-pepper"}},
            },
        },
    )
    with pytest.raises(ConfigurationError, match=r"(?i)at least 32 bytes"):
        app_short.boot()


def test_api_key_guard_from_config() -> None:
    cfg = Config({
        "auth": {
            "guards": {
                "api_key": {
                    "table": "custom_keys",
                    "prefix_column": "prefix_id",
                    "hash_column": "secret_digest",
                    "header": "x-custom-key",
                    "pepper": "custom-pepper-32-bytes-minimum!!",
                }
            }
        }
    })
    guard = ApiKeyGuard.from_config(cfg)
    assert guard.table == "custom_keys"
    assert guard.prefix_column == "prefix_id"
    assert guard.hash_column == "secret_digest"
    assert guard.header_name == "x-custom-key"
    assert guard.pepper == "custom-pepper-32-bytes-minimum!!"


@pytest.mark.asyncio
async def test_legacy_unsegmented_key_rejected_with_clear_message() -> None:
    for legacy_key in (b"sk_live_legacy_unsegmented_key", b"prefix_only.", b".secret_only"):
        req = _make_request([(b"x-api-key", legacy_key)])
        with pytest.raises(
            InvalidApiKeyError,
            match=r"Invalid API key format\. Expected '<prefix>\.<secret>'\.",
        ):
            await require_api_key(req, pepper="test-pepper-32-bytes-minimum----!")


@pytest.mark.asyncio
async def test_api_key_indexed_prefix_lookup(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    table_name = "test_auth_api_keys_indexed"
    pepper = "test-server-pepper-32-bytes-long!"

    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            DROP TABLE IF EXISTS "{table_name}" CASCADE;
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                key_prefix VARCHAR(64) NOT NULL UNIQUE,
                key_hash VARCHAR(128) NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                revoked BOOLEAN DEFAULT FALSE,
                expires_at TIMESTAMPTZ DEFAULT NULL
            );
            CREATE INDEX "idx_{table_name}_prefix" ON "{table_name}" (key_prefix);
            """
        )

        full_key, prefix, secret = generate_api_key(prefix="sk_test")
        key_hash = hash_api_key(secret, pepper=pepper)

        inactive_full, inactive_prefix, inactive_secret = generate_api_key(prefix="sk_inact")
        inactive_hash = hash_api_key(inactive_secret, pepper=pepper)

        revoked_full, revoked_prefix, revoked_secret = generate_api_key(prefix="sk_rev")
        revoked_hash = hash_api_key(revoked_secret, pepper=pepper)

        expired_full, expired_prefix, expired_secret = generate_api_key(prefix="sk_exp")
        expired_hash = hash_api_key(expired_secret, pepper=pepper)

        await conn.execute(
            f"""
            INSERT INTO "{table_name}" (name, key_prefix, key_hash, is_active, revoked, expires_at) VALUES
            ('Valid Client', $1, $2, TRUE, FALSE, NULL),
            ('Inactive Client', $3, $4, FALSE, FALSE, NULL),
            ('Revoked Client', $5, $6, TRUE, TRUE, NULL),
            ('Expired Client', $7, $8, TRUE, FALSE, NOW() - INTERVAL '1 hour');
            """,
            prefix, key_hash,
            inactive_prefix, inactive_hash,
            revoked_prefix, revoked_hash,
            expired_prefix, expired_hash,
        )

    # 1. Valid key succeeds and returns user row with key_hash stripped
    req_valid = _make_request([(b"x-api-key", full_key.encode("latin1"))])
    user = await require_api_key(
        req_valid,
        table=table_name,
        pepper=pepper,
        pool=ensure_db_pool,
    )
    assert user["name"] == "Valid Client"
    assert user["key_prefix"] == prefix
    assert "key_hash" not in user
    assert "key_hash" not in req_valid.state["user"]

    # 2. Invalid prefix returns 401
    req_bad_prefix = _make_request([(b"x-api-key", b"sk_nonexistent_prefix.randomsecret1234567890")])
    with pytest.raises(InvalidApiKeyError, match="Invalid API key"):
        await require_api_key(
            req_bad_prefix,
            table=table_name,
            pepper=pepper,
            pool=ensure_db_pool,
        )

    # 3. Valid prefix but wrong secret returns 401
    req_wrong_secret = _make_request([(b"x-api-key", f"{prefix}.wrong_secret_value_123456789".encode("latin1"))])
    with pytest.raises(InvalidApiKeyError, match="Invalid API key"):
        await require_api_key(
            req_wrong_secret,
            table=table_name,
            pepper=pepper,
            pool=ensure_db_pool,
        )

    # 4. Inactive key returns 401
    req_inactive = _make_request([(b"x-api-key", inactive_full.encode("latin1"))])
    with pytest.raises(InvalidApiKeyError, match="API key is inactive"):
        await require_api_key(
            req_inactive,
            table=table_name,
            pepper=pepper,
            pool=ensure_db_pool,
        )

    # 5. Revoked key returns 401
    req_revoked = _make_request([(b"x-api-key", revoked_full.encode("latin1"))])
    with pytest.raises(InvalidApiKeyError, match="API key has been revoked"):
        await require_api_key(
            req_revoked,
            table=table_name,
            pepper=pepper,
            pool=ensure_db_pool,
        )

    # 6. Expired key returns 401
    req_expired = _make_request([(b"x-api-key", expired_full.encode("latin1"))])
    with pytest.raises(InvalidApiKeyError, match="API key has expired"):
        await require_api_key(
            req_expired,
            table=table_name,
            pepper=pepper,
            pool=ensure_db_pool,
        )


@pytest.mark.asyncio
async def test_api_key_one_query_regardless_of_table_size_and_non_blocking_loop(
    ensure_db_pool: DatabasePool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    P2-A2: Asserts that:
    1. Exactly one DB query is executed regardless of how many rows exist in the table.
    2. A table of N rows does not block the asyncio event loop (concurrent ticker task max gap < threshold).
    3. Active transaction connection (get_active_connection) is reused inside db.transaction().
    """
    container = Container()
    container.instance(DatabasePool, ensure_db_pool)
    set_container(container)

    pool = ensure_db_pool.get_pool()
    table_name = "test_auth_api_keys_scale"
    pepper = "scale-server-pepper-32-bytes-long!"

    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            DROP TABLE IF EXISTS "{table_name}" CASCADE;
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                key_prefix VARCHAR(64) NOT NULL UNIQUE,
                key_hash VARCHAR(128) NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                revoked BOOLEAN DEFAULT FALSE,
                expires_at TIMESTAMPTZ DEFAULT NULL
            );
            CREATE INDEX "idx_{table_name}_prefix" ON "{table_name}" (key_prefix);
            """
        )

        rows_to_insert: list[tuple[str, str, str]] = []
        for i in range(39):
            _fk, pfx, sec = generate_api_key(prefix=f"sk_row{i}_")
            kh = hash_api_key(sec, pepper=pepper)
            rows_to_insert.append((f"Client {i}", pfx, kh))

        await conn.executemany(
            f'INSERT INTO "{table_name}" (name, key_prefix, key_hash) VALUES ($1, $2, $3)',
            rows_to_insert,
        )

    target_full_key, target_pfx, target_sec = generate_api_key(prefix="sk_row39_")
    target_hash = hash_api_key(target_sec, pepper=pepper)

    # Spy on query count via active transaction connection
    query_count = 0
    max_gap = 0.0
    stop_ticker = asyncio.Event()

    async def ticker() -> None:
        nonlocal max_gap
        last = time.perf_counter()
        while not stop_ticker.is_set():
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            gap = now - last
            max_gap = max(max_gap, gap)
            last = now

    class _CountingConnProxy:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> Any:
            nonlocal query_count
            query_count += 1
            return await self._inner.fetchrow(query, *args, **kwargs)

        async def fetch(self, query: str, *args: Any, **kwargs: Any) -> Any:
            nonlocal query_count
            query_count += 1
            return await self._inner.fetch(query, *args, **kwargs)

    ticker_task = asyncio.create_task(ticker())
    try:
        async with db.transaction() as tx_conn:
            # Insert row 39 uncommitted inside the active transaction so it is only visible via tx_conn
            await tx_conn.execute(
                f'INSERT INTO "{table_name}" (name, key_prefix, key_hash) VALUES ($1, $2, $3)',
                "Client 39",
                target_pfx,
                target_hash,
            )
            monkeypatch.setattr(
                "swiftpy.auth.api_key_guard.get_active_connection",
                lambda: _CountingConnProxy(tx_conn),
            )
            req = _make_request([(b"x-api-key", target_full_key.encode("latin1"))])
            user = await require_api_key(req, table=table_name, pepper=pepper)
            assert user["name"] == "Client 39"
            assert query_count == 1
    finally:
        stop_ticker.set()
        await ticker_task

    assert max_gap < 0.25


@pytest.mark.asyncio
async def test_api_key_expires_at_type_validation_p2a5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-A5: Non-datetime expires_at in row raises InvalidApiKeyError (401), never 500 AttributeError."""
    pepper = "type-check-pepper-at-least-32-bytes!"
    full_key, _prefix, secret = generate_api_key(prefix="sk_type")
    key_hash = hash_api_key(secret, pepper=pepper)

    class FakeConn:
        def __init__(self, expires_val: Any) -> None:
            self.expires_val = expires_val

        async def fetchrow(self, query: str, pfx: str) -> dict[str, Any]:
            return {
                "id": 1,
                "name": "Typed Key",
                "key_prefix": pfx,
                "key_hash": key_hash,
                "is_active": True,
                "revoked": False,
                "expires_at": self.expires_val,
            }

    # 1. Invalid integer expires_at -> InvalidApiKeyError (not AttributeError)
    monkeypatch.setattr(
        "swiftpy.auth.api_key_guard.get_active_connection",
        lambda: FakeConn(12345),
    )
    req1 = _make_request([(b"x-api-key", full_key.encode("latin1"))])
    with pytest.raises(InvalidApiKeyError, match="Invalid API key"):
        await require_api_key(req1, pepper=pepper)

    # 2. Invalid string expires_at -> InvalidApiKeyError
    monkeypatch.setattr(
        "swiftpy.auth.api_key_guard.get_active_connection",
        lambda: FakeConn("not-a-timestamp"),
    )
    req2 = _make_request([(b"x-api-key", full_key.encode("latin1"))])
    with pytest.raises(InvalidApiKeyError, match="Invalid API key"):
        await require_api_key(req2, pepper=pepper)

    # 3. Valid future ISO-8601 string expires_at -> succeeds
    future_iso = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    monkeypatch.setattr(
        "swiftpy.auth.api_key_guard.get_active_connection",
        lambda: FakeConn(future_iso),
    )
    req3 = _make_request([(b"x-api-key", full_key.encode("latin1"))])
    res = await require_api_key(req3, pepper=pepper)
    assert res["name"] == "Typed Key"


