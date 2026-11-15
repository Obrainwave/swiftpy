from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

from swiftpy.auth.api_key_guard import (
    get_server_pepper,
    hash_api_key,
    verify_api_key_hash,
)
from swiftpy.auth.auth_manager import AuthManager
from swiftpy.auth.jwt_guard import require_jwt
from swiftpy.auth.jwt_issuer import InvalidTokenError, JWTIssuer
from swiftpy.auth.token_blacklist import TokenBlacklist
from swiftpy.cache.cache import Cache
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.core.container import Container, bind_container
from swiftpy.events.redis_bus import RedisEventBus
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.testing.http_client import TestClient


@pytest.mark.asyncio
async def test_fresh_application_binds_auth_and_infra_singletons_and_closes_clients() -> None:
    """AUTH-1: Fresh Application without autodiscovery binds TokenBlacklist/Queue/EventBus/Cache/AuthManager and shuts down cleanly."""
    app = Application(
        config={
            "app": {"name": "AuthRemTest", "env": "testing", "debug": True},
            "auth": {
                "guards": {
                    "jwt": {
                        "secret": "remediation-jwt-secret-key-32-bytes!",
                    }
                }
            },
            "redis": {
                "host": os.getenv("REDIS_HOST", "127.0.0.1"),
                "port": int(os.getenv("REDIS_PORT", "6379")),
                "password": os.getenv("REDIS_PASSWORD") or None,
            },
        },
        auto_discover_providers=False,
    )

    assert app.container.has(JWTIssuer)
    assert app.container.has(TokenBlacklist)
    assert app.container.has(AuthManager)
    assert app.container.has(QueueDispatcher)
    assert app.container.has(RedisEventBus)
    assert app.container.has(Cache)

    blacklist = app.container.resolve(TokenBlacklist)
    prefix = f"test_auth1_{uuid.uuid4().hex[:8]}:"
    blacklist.prefix = prefix
    await blacklist.clear()

    issuer = app.container.resolve(JWTIssuer)
    token = issuer.issue_access_token(subject="user-1")

    @app.get("/protected")
    async def protected(request: Request) -> Response:
        user = await require_jwt(request)
        return json_response({"sub": user["sub"]})

    async with TestClient(app) as client:
        res_ok = await client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        res_ok.assert_ok()
        assert res_ok.json() == {"sub": "user-1"}

        await blacklist.revoke_token(token)
        res_revoked = await client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        res_revoked.assert_status(401)

        queue_disp = app.container.resolve(QueueDispatcher)
        await queue_disp.get_client()
        bus = app.container.resolve(RedisEventBus)
        await bus.get_client()
        cache_mgr = app.container.resolve(Cache)
        await cache_mgr.driver("memory").set("k", "v")
        await blacklist.clear()

    # Shutdown must close Redis/driver resources created during lifespan
    assert blacklist._client is None
    assert queue_disp._client is None
    assert bus._client is None
    assert cache_mgr._drivers == {}


def test_get_instance_binds_to_active_container_instead_of_global_state() -> None:
    """AUTH-1: QueueDispatcher.get_instance() and RedisEventBus.get_instance() bind into active container."""
    c1 = Container()
    c2 = Container()

    with bind_container(c1):
        q1 = QueueDispatcher.get_instance()
        b1 = RedisEventBus.get_instance()
        assert QueueDispatcher.get_instance() is q1
        assert RedisEventBus.get_instance() is b1
        assert c1.get_singleton(QueueDispatcher) is q1
        assert c1.get_singleton(RedisEventBus) is b1

    with bind_container(c2):
        q2 = QueueDispatcher.get_instance()
        b2 = RedisEventBus.get_instance()
        assert q2 is not q1
        assert b2 is not b1
        assert c2.get_singleton(QueueDispatcher) is q2
        assert c2.get_singleton(RedisEventBus) is b2


def test_jwt_issuer_custom_token_type_uses_access_token_ttl() -> None:
    """AUTH-2: Custom token_type values default to access_token_ttl rather than 7-day refresh_token_ttl."""
    issuer = JWTIssuer(
        secret="custom-token-type-secret-32-bytes!!",
        access_token_ttl=900,
        refresh_token_ttl=604800,
    )

    for custom_type in ("password_reset", "email_verify", "mfa"):
        tok = issuer.issue_token(subject=42, token_type=custom_type)
        payload = issuer.decode_token(tok, expected_type=custom_type)
        assert payload["exp"] - payload["iat"] == 900

    refresh_tok = issuer.issue_token(subject=42, token_type="refresh")
    refresh_payload = issuer.decode_token(refresh_tok, expected_type="refresh")
    assert refresh_payload["exp"] - refresh_payload["iat"] == 604800


@pytest.mark.asyncio
async def test_token_blacklist_revoke_future_nbf_and_reject_non_positive_ttl() -> None:
    """AUTH-3: Future-nbf tokens can be pre-emptively revoked and non-positive explicit TTL raises ValueError."""
    issuer = JWTIssuer(secret="future-nbf-revocation-secret-32-bytes!")
    prefix = f"test_auth3_{uuid.uuid4().hex[:8]}:"
    blacklist = TokenBlacklist(prefix=prefix, issuer=issuer)
    await blacklist.clear()

    try:
        future_tok = issuer.issue_token(
            subject="user-9",
            not_before=timedelta(minutes=10),
        )
        # Normal decode enforces nbf
        with pytest.raises(InvalidTokenError, match="nbf"):
            issuer.decode_token(future_tok)

        # Opting out of nbf check succeeds
        decoded = issuer.decode_token(future_tok, verify_nbf=False)

        # Pre-emptive revocation must succeed
        await blacklist.revoke_token(future_tok)
        assert await blacklist.is_revoked(decoded["jti"]) is True

        # Explicit ttl <= 0 or bool must fail loudly instead of falling back to 7 days
        for invalid_ttl in (0, -1, -60, True, False):
            with pytest.raises(ValueError, match="positive"):
                await blacklist.revoke("jti-invalid-ttl", ttl=invalid_ttl)
            with pytest.raises(ValueError, match="positive"):
                await blacklist.revoke_all_for_subject("user-9", ttl=invalid_ttl)
    finally:
        await blacklist.clear()
        await blacklist.close()


def test_hash_api_key_defaults_to_peppered_hmac_and_isolates_ephemeral_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-4: hash_api_key defaults to peppered HMAC (>72B safe) and ephemeral peppers are isolated per Config."""
    monkeypatch.delenv("AUTH_KEY_PEPPER", raising=False)
    monkeypatch.delenv("API_KEY_PEPPER", raising=False)

    cfg1 = Config({"app": {"debug": True, "env": "development"}})
    cfg2 = Config({"app": {"debug": True, "env": "development"}})

    pepper1_a = get_server_pepper(cfg1)
    pepper1_b = get_server_pepper(cfg1)
    pepper2 = get_server_pepper(cfg2)
    assert pepper1_a == pepper1_b
    assert len(pepper1_a) >= 32
    assert pepper2 != pepper1_a

    # Default hash_api_key binds to active container's Config pepper and handles >72-byte keys cleanly
    c1 = Container()
    c1.instance(Config, cfg1)
    long_secret = "x" * 100
    with bind_container(c1):
        hashed_hmac = hash_api_key(long_secret)
    assert not hashed_hmac.startswith("$2")
    assert len(hashed_hmac) == 64
    assert verify_api_key_hash(long_secret, hashed_hmac, pepper=pepper1_a) is True
    assert verify_api_key_hash(long_secret, hashed_hmac, pepper=pepper2) is False

    # Explicit legacy bcrypt rounds also handles >72-byte secrets safely via pre-hashing
    hashed_bcrypt = hash_api_key(long_secret, rounds=4)
    assert hashed_bcrypt.startswith("$2")
    assert verify_api_key_hash(long_secret, hashed_bcrypt) is True
    assert verify_api_key_hash("y" * 100, hashed_bcrypt) is False


@pytest.mark.asyncio
async def test_blacklist_enabled_false_allows_jwt_without_redis() -> None:
    """AUTH-1: Disabling blacklist via config allows JWT verification even when Redis is unavailable."""
    secret = "disabled-blacklist-jwt-secret-key-32bytes"
    for auth_cfg in (
        {"blacklist_enabled": False, "guards": {"jwt": {"secret": secret}}},
        {"guards": {"jwt": {"secret": secret, "blacklist_enabled": False}}},
    ):
        app = Application(
            config={
                "app": {"name": "NoRedisAuth", "env": "testing", "debug": True},
                "auth": auth_cfg,
                "redis": {"host": "127.0.0.1", "port": 1},
            },
            auto_discover_providers=False,
        )
        assert not app.container.has(TokenBlacklist)
        issuer = app.container.resolve(JWTIssuer)
        token = issuer.issue_access_token(subject="user-no-redis")

        @app.get("/me")
        async def me(request: Request) -> Response:
            claims = await require_jwt(request)
            return json_response({"sub": claims["sub"]})

        async with TestClient(app) as client:
            res = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
            res.assert_ok()
            assert res.json() == {"sub": "user-no-redis"}
