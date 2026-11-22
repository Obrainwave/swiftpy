from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
import uuid
from datetime import timedelta
from typing import Any

import pytest

from swiftpy import Application, Request, Response, Router, TestClient, json_response
from swiftpy.auth import (
    AuthenticationError,
    CurrentUser,
    InvalidSignatureError,
    InvalidTokenError,
    JWTError,
    JWTGuard,
    JWTIssuer,
    TokenBlacklist,
    TokenExpiredError,
    TokenRevokedError,
    auth,
)
from swiftpy.auth.exceptions import AuthenticationServiceUnavailableError
from swiftpy.core.config import Config
from swiftpy.core.container import Container, bind_container
from swiftpy.core.context import Context


def _make_auth_app(
    *,
    blacklist_prefix: str | None = None,
    blacklist_enabled: bool = True,
) -> Application:
    secret = "phase6-test-jwt-secret-key-at-least-32-bytes!"
    app = Application(
        config={
            "app": {"name": "Phase6AuthApp", "env": "testing", "debug": True},
            "auth": {
                "blacklist_enabled": blacklist_enabled,
                "defaults": {"guard": "jwt"},
                "guards": {
                    "jwt": {
                        "secret": secret,
                        "algorithm": "HS256",
                        "access_token_ttl": 300,
                        "refresh_token_ttl": 3600,
                    }
                },
            },
            "redis": {
                "host": os.getenv("REDIS_HOST", "127.0.0.1"),
                "port": int(os.getenv("REDIS_PORT", "6379")),
                "password": os.getenv("REDIS_PASSWORD") or "secret",
            },
        },
        auto_discover_providers=False,
    )
    if blacklist_prefix is not None and blacklist_enabled:
        app.container.instance(
            TokenBlacklist,
            TokenBlacklist(
                prefix=blacklist_prefix,
                config=app.config,
                container=app.container,
            ),
        )
    return app


@pytest.mark.asyncio
async def test_refresh_token_pair_rotation_and_reuse_rejected_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    prefix = f"test_p6_refresh_{uuid.uuid4().hex[:8]}:"
    issuer = JWTIssuer(secret="phase6-refresh-secret-key-at-least-32-bytes!")
    blacklist = TokenBlacklist(prefix=prefix, issuer=issuer)
    await blacklist.clear()

    try:
        pair1 = issuer.issue_token_pair(
            subject=42,
            claims={"role": "member"},
            refresh_claims={"device": "ios"},
        )

        # Rotate once -> issues fresh pair and blacklists old refresh jti
        pair2 = await issuer.refresh_token_pair(
            pair1["refresh_token"],
            blacklist=blacklist,
            claims={"role": "admin", "email": "ada@example.com"},
        )
        assert pair2["access_token"] != pair1["access_token"]
        assert pair2["refresh_token"] != pair1["refresh_token"]
        assert pair2["token_type"] == "Bearer"
        assert pair2["expires_in"] == issuer.access_token_ttl

        access2_payload = issuer.decode_token(pair2["access_token"], expected_type="access")
        refresh2_payload = issuer.decode_token(pair2["refresh_token"], expected_type="refresh")
        assert access2_payload["sub"] == "42"
        assert access2_payload["role"] == "admin"
        assert access2_payload["email"] == "ada@example.com"
        assert refresh2_payload["device"] == "ios"

        # Old rotated refresh token must be rejected and logged without leaking token
        with (
            caplog.at_level(logging.WARNING, logger="swiftpy.auth.jwt"),
            pytest.raises(TokenRevokedError) as exc_info,
        ):
            await issuer.refresh_token_pair(
                pair1["refresh_token"],
                blacklist=blacklist,
            )
        assert isinstance(exc_info.value, JWTError)
        assert isinstance(exc_info.value, AuthenticationError)
        assert any(
            "revoked" in r.getMessage().lower() or "reused" in r.getMessage().lower()
            for r in caplog.records
        )
        assert all(pair1["refresh_token"] not in r.getMessage() for r in caplog.records)

        # Fresh rotated refresh token continues to work cleanly
        pair3 = await issuer.refresh_token_pair(
            pair2["refresh_token"],
            blacklist=blacklist,
        )
        assert pair3["refresh_token"] != pair2["refresh_token"]
    finally:
        await blacklist.clear()
        await blacklist.close()


@pytest.mark.asyncio
async def test_refresh_token_pair_rejection_matrix_and_rotate_false() -> None:
    prefix = f"test_p6_reject_{uuid.uuid4().hex[:8]}:"
    issuer = JWTIssuer(secret="phase6-reject-secret-key-at-least-32-bytes!")
    other_issuer = JWTIssuer(secret="phase6-wrong-secret-key-at-least-32-bytes!!")
    blacklist = TokenBlacklist(prefix=prefix, issuer=issuer)
    await blacklist.clear()

    try:
        pair = issuer.issue_token_pair(subject="u-1")

        # 1. Access token passed instead of refresh token
        with pytest.raises(InvalidTokenError, match="expected 'refresh'"):
            await issuer.refresh_token_pair(pair["access_token"], blacklist=blacklist)

        # 2. Expired refresh token
        expired_refresh = issuer.issue_refresh_token(
            subject="u-1",
            expires_in=timedelta(seconds=-10),
        )
        with pytest.raises(TokenExpiredError):
            await issuer.refresh_token_pair(expired_refresh, blacklist=blacklist)

        # 3. Wrong signature
        bad_sig = other_issuer.issue_refresh_token(subject="u-1")
        with pytest.raises(InvalidSignatureError):
            await issuer.refresh_token_pair(bad_sig, blacklist=blacklist)

        # 4. Explicitly blacklisted refresh token
        revoked_refresh = issuer.issue_refresh_token(subject="u-1")
        await blacklist.revoke_token(revoked_refresh)
        with pytest.raises(TokenRevokedError):
            await issuer.refresh_token_pair(revoked_refresh, blacklist=blacklist)

        # 5. Non-rotating refresh preserves the same refresh token validity
        unrotated = await issuer.refresh_token_pair(
            pair["refresh_token"],
            blacklist=blacklist,
            rotate=False,
        )
        assert unrotated["refresh_token"] != pair["refresh_token"]
        second = await issuer.refresh_token_pair(
            pair["refresh_token"],
            blacklist=blacklist,
            rotate=True,
        )
        assert second["access_token"]
        with pytest.raises(TokenRevokedError):
            await issuer.refresh_token_pair(pair["refresh_token"], blacklist=blacklist)
    finally:
        await blacklist.clear()
        await blacklist.close()


@pytest.mark.asyncio
async def test_refresh_token_pair_fails_closed_when_blacklist_redis_unavailable() -> None:
    issuer = JWTIssuer(secret="phase6-redis-fail-secret-key-at-least-32b!")
    refresh_token = issuer.issue_refresh_token(subject="user-503")

    class BrokenBlacklist:
        async def is_revoked(
            self,
            jti: str,
            subject: str | int | None = None,
            issued_at: int | None = None,
        ) -> bool:
            raise ConnectionError("Redis unavailable")

        async def revoke(self, jti: str, ttl: int | None = None) -> None:
            raise ConnectionError("Redis unavailable")

    with pytest.raises(AuthenticationServiceUnavailableError):
        await issuer.refresh_token_pair(
            refresh_token,
            blacklist=BrokenBlacklist(),
        )


@pytest.mark.asyncio
async def test_full_auth_cycle_login_refresh_logout_and_logout_all_e2e() -> None:
    prefix = f"test_p6_e2e_{uuid.uuid4().hex[:8]}:"
    app = _make_auth_app(blacklist_prefix=prefix)
    bl = app.container.resolve(TokenBlacklist)
    await bl.clear()

    @app.post("/auth/login")
    async def login(request: Request) -> Response:
        issuer = app.container.resolve(JWTIssuer)
        pair = issuer.issue_token_pair(
            subject=request.user_id,
            claims={"email": request.email},
        )
        return json_response(pair)

    @app.post("/auth/refresh")
    async def refresh(request: Request) -> Response:
        issuer = app.container.resolve(JWTIssuer)
        try:
            pair = await issuer.refresh_token_pair(
                request.refresh_token,
                claims={"email": "updated@example.com"},
            )
        except JWTError as exc:
            return json_response({"detail": str(exc)}, status_code=401)
        return json_response(pair)

    protected = Router(middleware=[JWTGuard])

    @protected.post("/auth/logout")
    async def logout_route(request: Request) -> Response:
        await auth().logout(refresh_token=request.get("refresh_token"))
        return json_response({"logged_out": True, "still_authenticated": auth().check()})

    @protected.post("/auth/logout-all")
    async def logout_all_route() -> Response:
        await auth().logout_all()
        return json_response({"logged_out_all": True, "still_authenticated": auth().check()})

    @protected.get("/me")
    async def me() -> Response:
        u = auth().user()
        assert u is not None
        return json_response({"id": u.id, "email": u.email})

    app.include_router(protected)

    try:
        async with TestClient(app) as client:
            # 1. Login session 1 & session 2 for user 101, plus session for user 202
            res_login1 = await client.post(
                "/auth/login",
                json={"user_id": "101", "email": "u101@example.com"},
            )
            res_login1.assert_ok()
            tokens1 = res_login1.json()

            res_login2 = await client.post(
                "/auth/login",
                json={"user_id": "101", "email": "u101@example.com"},
            )
            tokens2 = res_login2.json()

            res_other = await client.post(
                "/auth/login",
                json={"user_id": "202", "email": "u202@example.com"},
            )
            tokens_other = res_other.json()

            # 2. Access protected route
            me1 = await client.get(
                "/me",
                headers={"Authorization": f"Bearer {tokens1['access_token']}"},
            )
            me1.assert_ok()
            assert me1.json()["email"] == "u101@example.com"

            # 3. Refresh tokens1 -> old refresh token becomes invalid, new pair works
            ref1 = await client.post(
                "/auth/refresh",
                json={"refresh_token": tokens1["refresh_token"]},
            )
            ref1.assert_ok()
            refreshed1 = ref1.json()

            reused = await client.post(
                "/auth/refresh",
                json={"refresh_token": tokens1["refresh_token"]},
            )
            reused.assert_status(401)

            me_refreshed = await client.get(
                "/me",
                headers={"Authorization": f"Bearer {refreshed1['access_token']}"},
            )
            me_refreshed.assert_ok()
            assert me_refreshed.json()["email"] == "updated@example.com"

            # 4. Logout current session (revokes refreshed access AND refresh tokens)
            lo1 = await client.post(
                "/auth/logout",
                json={"refresh_token": refreshed1["refresh_token"]},
                headers={"Authorization": f"Bearer {refreshed1['access_token']}"},
            )
            lo1.assert_ok()
            assert lo1.json()["still_authenticated"] is False

            # Revoked access and refresh tokens from session 1 must now fail
            after_lo_me = await client.get(
                "/me",
                headers={"Authorization": f"Bearer {refreshed1['access_token']}"},
            )
            after_lo_me.assert_status(401)
            after_lo_ref = await client.post(
                "/auth/refresh",
                json={"refresh_token": refreshed1["refresh_token"]},
            )
            after_lo_ref.assert_status(401)

            # Session 2 for user 101 is still active until logout-all
            me2 = await client.get(
                "/me",
                headers={"Authorization": f"Bearer {tokens2['access_token']}"},
            )
            me2.assert_ok()

            # 5. Logout all sessions for user 101
            lo_all = await client.post(
                "/auth/logout-all",
                headers={"Authorization": f"Bearer {tokens2['access_token']}"},
            )
            lo_all.assert_ok()
            assert lo_all.json()["still_authenticated"] is False

            me2_after = await client.get(
                "/me",
                headers={"Authorization": f"Bearer {tokens2['access_token']}"},
            )
            me2_after.assert_status(401)
            ref2_after = await client.post(
                "/auth/refresh",
                json={"refresh_token": tokens2["refresh_token"]},
            )
            ref2_after.assert_status(401)

            # Different user 202 remains completely unaffected
            other_me = await client.get(
                "/me",
                headers={"Authorization": f"Bearer {tokens_other['access_token']}"},
            )
            other_me.assert_ok()
    finally:
        await bl.clear()


@pytest.mark.asyncio
async def test_auth_logout_all_explicit_subject_and_missing_subject_error() -> None:
    prefix = f"test_p6_sub_{uuid.uuid4().hex[:8]}:"
    issuer = JWTIssuer(secret="phase6-subject-secret-key-at-least-32b!")
    blacklist = TokenBlacklist(prefix=prefix, issuer=issuer)
    await blacklist.clear()
    Context.clear()

    try:
        with pytest.raises(AuthenticationError, match="subject"):
            await auth().logout_all(blacklist=blacklist)

        # Admin context remains intact when logging out a different target subject
        admin_payload: dict[str, Any] = {"sub": "900", "id": 900, "jti": "admin-jti"}
        Context.set("__swiftpy_current_user__", admin_payload)

        target_pair = issuer.issue_token_pair(subject=555)
        await auth().logout_all(555, blacklist=blacklist)

        assert auth().check() is True
        assert auth().id() == 900

        with pytest.raises(TokenRevokedError):
            await issuer.refresh_token_pair(
                target_pair["refresh_token"],
                blacklist=blacklist,
            )
    finally:
        Context.clear()
        await blacklist.clear()
        await blacklist.close()


@pytest.mark.asyncio
async def test_auth_logout_from_context_jti_without_bearer_header() -> None:
    prefix = f"test_p6_ctx_{uuid.uuid4().hex[:8]}:"
    issuer = JWTIssuer(secret="phase6-context-secret-key-at-least-32b!")
    blacklist = TokenBlacklist(prefix=prefix, issuer=issuer)
    await blacklist.clear()
    Context.clear()

    try:
        token = issuer.issue_access_token(subject=77)
        payload = issuer.decode_token(token)
        Context.set("__swiftpy_current_user__", payload)

        await auth().logout(blacklist=blacklist, issuer=issuer)
        assert auth().check() is False
        assert await blacklist.is_revoked(payload["jti"]) is True
    finally:
        Context.clear()
        await blacklist.clear()
        await blacklist.close()


@pytest.mark.asyncio
async def test_concurrent_refresh_token_rotation_allows_only_one_claim() -> None:
    prefix = f"test_p6_race_{uuid.uuid4().hex[:8]}:"
    issuer = JWTIssuer(secret="phase6-race-secret-key-at-least-32-bytes!!")
    blacklist = TokenBlacklist(prefix=prefix, issuer=issuer)
    await blacklist.clear()

    try:
        pair = issuer.issue_token_pair(subject=42)
        results = await asyncio.gather(
            *[
                issuer.refresh_token_pair(
                    pair["refresh_token"],
                    blacklist=blacklist,
                )
                for _ in range(8)
            ],
            return_exceptions=True,
        )
        successes = [r for r in results if isinstance(r, dict)]
        revoked_errors = [r for r in results if isinstance(r, TokenRevokedError)]
        assert len(successes) == 1
        assert len(revoked_errors) == 7
    finally:
        await blacklist.clear()
        await blacklist.close()


@pytest.mark.asyncio
async def test_integer_zero_id_and_blacklist_cache_key_with_issued_at() -> None:
    """Fixes 2 & 3: Integer id=0 is preserved (not treated as falsy) and TokenBlacklist._local_cache keys on issued_at."""
    prefix = f"test_fix23_{uuid.uuid4().hex[:8]}:"
    issuer = JWTIssuer(secret="fix23-secret-key-at-least-32-bytes-long!!")
    blacklist = TokenBlacklist(prefix=prefix, issuer=issuer)
    await blacklist.clear()
    Context.clear()

    try:
        # 1. Integer ID 0 is preserved in AuthUser, CurrentUser, auth().id(), and logout_all()
        Context.set("__swiftpy_current_user__", {"id": 0, "email": "root@example.com"})
        user_obj = auth().user()
        assert user_obj is not None
        assert user_obj.id == 0
        assert auth().id() == 0

        cu = CurrentUser()
        assert cu.id == 0

        # Revoke all for subject 0 via logout_all()
        await auth().logout_all(blacklist=blacklist)
        assert auth().check() is False

        # Subject 0 with issued_at <= cutoff must be detected as revoked (not skipped by `not subject`)
        now_ts = int(time.time())
        assert await blacklist.is_revoked("", subject=0, issued_at=now_ts - 5) is True

        # 2. TokenBlacklist._local_cache must include issued_at so a negative cache hit
        # for a newer token (issued_at > cutoff) never masks an older token (issued_at <= cutoff)
        await blacklist.revoke_all_for_subject("42")
        cutoff = int(time.time())

        # Newer token (issued after cutoff) -> NOT revoked (populates _local_cache)
        assert (
            await blacklist.is_revoked(
                "shared-or-empty-jti",
                subject="42",
                issued_at=cutoff + 100,
            )
            is False
        )
        # Older token with same (jti, subject) but issued_at <= cutoff checked immediately within 2s TTL -> MUST be revoked!
        assert (
            await blacklist.is_revoked(
                "shared-or-empty-jti",
                subject="42",
                issued_at=cutoff - 10,
            )
            is True
        )
    finally:
        Context.clear()
        await blacklist.clear()
        await blacklist.close()


@pytest.mark.asyncio
async def test_jwt_issuer_class_level_refresh_token_pair_e2e_service_pattern() -> None:
    """Calling await JWTIssuer.refresh_token_pair(...) directly on the class resolves the active app singleton."""
    prefix = f"test_cls_refresh_{uuid.uuid4().hex[:8]}:"
    app = _make_auth_app(blacklist_prefix=prefix)
    bl = app.container.resolve(TokenBlacklist)
    await bl.clear()

    assert inspect.iscoroutinefunction(JWTIssuer.refresh_token_pair)
    sig_params = list(inspect.signature(JWTIssuer.refresh_token_pair).parameters)
    assert sig_params[0] == "refresh_token"
    assert "self" not in sig_params

    class AuthService:
        async def refresh(self, refresh_token: str) -> dict[str, Any]:
            return await JWTIssuer.refresh_token_pair(
                refresh_token=refresh_token,
                rotate=True,
            )

    service = AuthService()

    @app.post("/auth/login")
    async def login(request: Request) -> Response:
        pair = JWTIssuer.issue_token_pair(
            subject=request.user_id,
            claims={"email": request.email},
        )
        return json_response(pair)

    @app.post("/auth/refresh")
    async def refresh(request: Request) -> Response:
        try:
            data = await service.refresh(request.refresh_token)
        except JWTError as exc:
            return json_response({"detail": str(exc)}, status_code=401)
        return json_response(data)

    protected = Router(middleware=[JWTGuard])

    @protected.get("/me")
    async def me() -> Response:
        u = auth().user()
        assert u is not None
        return json_response({"id": u.id, "email": u.email})

    app.include_router(protected)

    try:
        async with TestClient(app) as client:
            login_res = await client.post(
                "/auth/login",
                json={"user_id": 77, "email": "service@example.com"},
            )
            login_res.assert_ok()
            tokens1 = login_res.json()

            refresh_res = await client.post(
                "/auth/refresh",
                json={"refresh_token": tokens1["refresh_token"]},
            )
            refresh_res.assert_ok()
            tokens2 = refresh_res.json()
            assert tokens2["access_token"] != tokens1["access_token"]
            assert tokens2["refresh_token"] != tokens1["refresh_token"]

            # Reusing rotated refresh token via class call must be rejected by TokenBlacklist
            replay_res = await client.post(
                "/auth/refresh",
                json={"refresh_token": tokens1["refresh_token"]},
            )
            replay_res.assert_status(401)

            me_res = await client.get(
                "/me",
                headers={"Authorization": f"Bearer {tokens2['access_token']}"},
            )
            me_res.assert_ok()
            assert me_res.json()["id"] == 77
    finally:
        Context.clear()
        await bl.clear()
        await bl.close()


@pytest.mark.asyncio
async def test_jwt_issuer_class_level_methods_container_isolation_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Class-level JWTIssuer methods stay isolated per active Container and fall back without a container."""
    c1 = Container()
    c1.instance(
        Config,
        Config(
            {
                "app": {"env": "testing", "debug": True},
                "auth": {
                    "blacklist_enabled": False,
                    "guards": {
                        "jwt": {"secret": "container-one-secret-at-least-32-bytes!"}
                    },
                },
            }
        ),
    )
    c2 = Container()
    c2.instance(
        Config,
        Config(
            {
                "app": {"env": "testing", "debug": True},
                "auth": {
                    "blacklist_enabled": False,
                    "guards": {
                        "jwt": {"secret": "container-two-secret-at-least-32-bytes!"}
                    },
                },
            }
        ),
    )

    try:
        with bind_container(c1):
            pair1 = JWTIssuer.issue_token_pair(subject="u-1", claims={"tenant": "one"})
            access1 = JWTIssuer.issue_access_token(subject="u-1")
            refresh1 = JWTIssuer.issue_refresh_token(subject="u-1")
            custom1 = JWTIssuer.issue_token(subject="u-1", token_type="verify")
            assert JWTIssuer.decode_token(pair1["access_token"])["tenant"] == "one"
            assert JWTIssuer.decode_token(access1, expected_type="access")["sub"] == "u-1"
            assert JWTIssuer.decode_token(refresh1, expected_type="refresh")["sub"] == "u-1"
            assert JWTIssuer.decode_token(custom1, expected_type="verify")["sub"] == "u-1"
            rotated1 = await JWTIssuer.refresh_token_pair(
                refresh_token=pair1["refresh_token"],
                rotate=True,
            )
            assert JWTIssuer.decode_token(rotated1["access_token"])["sub"] == "u-1"
            Context.clear()

        with bind_container(c2):
            pair2 = JWTIssuer.issue_token_pair(subject="u-2", claims={"tenant": "two"})
            assert JWTIssuer.decode_token(pair2["access_token"])["tenant"] == "two"
            with pytest.raises(InvalidSignatureError):
                JWTIssuer.decode_token(pair1["access_token"])
            with pytest.raises(InvalidSignatureError):
                await JWTIssuer.refresh_token_pair(
                    refresh_token=pair1["refresh_token"],
                    rotate=True,
                )
            Context.clear()

        # Explicit instance invocation still uses its own secret regardless of active container
        explicit = JWTIssuer(secret="explicit-instance-secret-key-32-bytes!!")
        explicit_pair = explicit.issue_token_pair(subject="explicit-u")
        with bind_container(c1):
            assert explicit.decode_token(explicit_pair["access_token"])["sub"] == "explicit-u"
            refreshed_explicit = await explicit.refresh_token_pair(
                explicit_pair["refresh_token"]
            )
            assert explicit.decode_token(refreshed_explicit["access_token"])["sub"] == "explicit-u"
            Context.clear()

        # Standalone fallback when no Container is bootstrapped reads Config.get_instance() cleanly
        standalone_cfg = Config(
            {
                "app": {"env": "testing", "debug": True},
                "auth": {
                    "blacklist_enabled": False,
                    "guards": {
                        "jwt": {"secret": "standalone-fallback-secret-key-32-bytes!"}
                    },
                },
            }
        )

        def _standalone_cfg_instance(cls: type[Config]) -> Config:
            return standalone_cfg

        def _raise_no_container() -> Container:
            raise RuntimeError("Application container not bootstrapped")

        monkeypatch.setattr(
            Config,
            "get_instance",
            classmethod(_standalone_cfg_instance),
        )
        monkeypatch.setattr(
            "swiftpy.auth.jwt_issuer.get_container",
            _raise_no_container,
        )
        standalone_pair = JWTIssuer.issue_token_pair(subject="standalone-user")
        assert (
            JWTIssuer.decode_token(standalone_pair["access_token"])["sub"]
            == "standalone-user"
        )
        standalone_rotated = await JWTIssuer.refresh_token_pair(
            refresh_token=standalone_pair["refresh_token"],
            rotate=False,
        )
        assert (
            JWTIssuer.decode_token(standalone_rotated["access_token"])["sub"]
            == "standalone-user"
        )
    finally:
        Context.clear()

