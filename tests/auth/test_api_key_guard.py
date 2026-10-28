from typing import Any

import pytest

from swiftpy.auth.api_key_guard import (
    generate_api_key,
    get_server_pepper,
    hash_api_key,
    require_api_key,
    verify_api_key_hash,
)
from swiftpy.auth.exceptions import InvalidApiKeyError
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.database.pool import DatabasePool
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
    # In production without pepper, get_server_pepper must raise ConfigurationError
    with pytest.raises(ConfigurationError, match=r"(?i)pepper"):
        get_server_pepper(cfg)



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

        # Inactive key
        inactive_full, inactive_prefix, inactive_secret = generate_api_key(prefix="sk_inact")
        inactive_hash = hash_api_key(inactive_secret, pepper=pepper)

        # Revoked key
        revoked_full, revoked_prefix, revoked_secret = generate_api_key(prefix="sk_rev")
        revoked_hash = hash_api_key(revoked_secret, pepper=pepper)

        # Expired key
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

    # 1. Valid key succeeds and returns user row
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

