from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import timedelta
from typing import Any

import pytest

from swiftpy import Application, Request, Response, Router, TestClient, json_response
from swiftpy.admin.auth import AdminSessionStore, AdminUser
from swiftpy.auth import (
    AuthenticationError,
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
                "port": int(os.getenv("REDIS_PORT", "26379")),
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


def test_admin_session_store_flush_revokes_all_prior_sessions_immediately() -> None:
    store = AdminSessionStore(secret_key="phase6-admin-flush-secret-32-bytes!!")
    user1 = AdminUser(username="alice", session_id="sid-1")
    user2 = AdminUser(username="bob", session_id="sid-2")

    tok1 = store.create_token(user1, max_age=3600)
    tok2 = store.create_token(user2, max_age=3600)
    assert store.verify_token(tok1) is not None
    assert store.verify_token(tok2) is not None

    # Manually crafted legacy token without epoch field still works prior to flush
    store.flush()

    assert store.verify_token(tok1) is None
    assert store.verify_token(tok2) is None

    # New session created immediately after flush (within the same second) is valid
    user3 = AdminUser(username="alice", session_id="sid-3")
    tok3 = store.create_token(user3, max_age=3600)
    verified3 = store.verify_token(tok3)
    assert verified3 is not None
    assert verified3.session_id == "sid-3"


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
