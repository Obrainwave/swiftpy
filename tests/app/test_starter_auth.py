from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from app.http.middleware.admin_middleware import AdminMiddleware
from app.http.middleware.auth_middleware import AuthMiddleware
from app.providers.auth_service_provider import AuthServiceProvider
from swiftpy.auth import AuthManager, AuthUser, JWTIssuer, auth, set_user_model
from swiftpy.core.application import Application
from swiftpy.core.context import Context
from swiftpy.core.exceptions import HttpException
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import RouteGroup
from swiftpy.testing.http_client import TestClient
from tests.benchmarks import benchmark_token, register_benchmark_routes


def _build_test_app(*, secret: str = "x" * 40, env: str = "local", debug: bool = True, enable_benchmarks: bool = False) -> Application:
    app = Application(
        auto_discover_providers=False,
        debug=debug,
        config={
            "app": {
                "env": env,
                "debug": debug,
                "enable_benchmarks": enable_benchmarks,
            },
            "auth": {
                "defaults": {"guard": "jwt"},
                "guards": {
                    "jwt": {
                        "driver": "jwt",
                        "algorithm": "HS256",
                        "secret": secret,
                        "access_token_ttl": 900,
                        "refresh_token_ttl": 3600,
                    }
                },
            },
            "redis": {
                "host": os.getenv("REDIS_HOST", "127.0.0.1"),
                "port": int(os.getenv("REDIS_PORT", "6379")),
                "password": os.getenv("REDIS_PASSWORD") or None,
            },
        },
        providers=[AuthServiceProvider],
    )
    return app


@pytest.mark.asyncio
async def test_starter_auth_middleware_rejects_forged_and_missing_tokens() -> None:
    app = _build_test_app()

    @app.group("/protected", middleware=[AuthMiddleware])
    def _protected_group(group: RouteGroup) -> None:
        @group.get("/")
        async def protected(request: Request) -> Response:
            user: Any = Context.get("auth.user")
            return json_response({"user": user, "state_user": request.state.get("user")})

    client = TestClient(app)

    # 1. Missing Authorization header -> 401
    res_missing = await client.get("/protected")
    assert res_missing.status_code == 401

    # 2. Arbitrary Bearer token string MUST be rejected -> 401
    res_forged = await client.get(
        "/protected",
        headers={"Authorization": "Bearer not-a-real-jwt-token"},
    )
    assert res_forged.status_code == 401

    # 3. JWT signed with wrong key MUST be rejected -> 401
    wrong_issuer = JWTIssuer(secret="y" * 40)
    wrong_token = wrong_issuer.issue_token(subject="42")
    res_wrong_sig = await client.get(
        "/protected",
        headers={"Authorization": f"Bearer {wrong_token}"},
    )
    assert res_wrong_sig.status_code == 401

    # 4. Valid signed JWT succeeds and populates auth.user + request.state["user"]
    real_issuer = app.container.resolve(JWTIssuer)
    valid_token = real_issuer.issue_token(subject="42", claims={"is_admin": False})
    res_ok = await client.get(
        "/protected",
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    res_ok.assert_ok()
    payload = res_ok.json()
    assert payload["user"]["sub"] == "42"
    assert payload["state_user"]["sub"] == "42"


@pytest.mark.asyncio
async def test_starter_admin_middleware_enforces_is_admin_strictly_on_dict_and_object() -> None:
    app = _build_test_app()

    @app.group("/admin-only", middleware=[AuthMiddleware, AdminMiddleware])
    def _admin_group(group: RouteGroup) -> None:
        @group.get("/")
        async def admin_only(request: Request) -> Response:
            return json_response({"ok": True})

    client = TestClient(app)
    issuer = app.container.resolve(JWTIssuer)

    # Non-admin claim -> 403
    user_token = issuer.issue_token(subject="7", claims={"is_admin": False})
    res_forbidden = await client.get(
        "/admin-only",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res_forbidden.status_code == 403

    # String "false" must NOT be treated as truthy admin -> 403
    str_token = issuer.issue_token(subject="8", claims={"is_admin": "false"})
    res_str = await client.get(
        "/admin-only",
        headers={"Authorization": f"Bearer {str_token}"},
    )
    assert res_str.status_code == 403

    # Admin boolean True -> 200
    admin_token = issuer.issue_token(subject="1", claims={"is_admin": True})
    res_admin = await client.get(
        "/admin-only",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    res_admin.assert_ok()
    assert res_admin.json() == {"ok": True}

    # Also verify AdminMiddleware handles user model/object attributes strictly (`is True`)
    Context.clear()
    mw = AdminMiddleware()

    async def _next_ok(request: Request) -> Response:
        return json_response({"ok": True})

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/admin-obj",
        "headers": [],
        "query_string": b"",
    }

    async def _recv() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    req_non_admin = Request(scope, _recv)
    req_non_admin.state["user"] = SimpleNamespace(is_admin=False)
    with pytest.raises(HttpException) as exc_non_admin:
        await mw.handle(req_non_admin, _next_ok)
    assert exc_non_admin.value.status_code == 403

    req_str_admin = Request(scope, _recv)
    req_str_admin.state["user"] = SimpleNamespace(is_admin="true")
    with pytest.raises(HttpException) as exc_str_admin:
        await mw.handle(req_str_admin, _next_ok)
    assert exc_str_admin.value.status_code == 403

    req_true_admin = Request(scope, _recv)
    req_true_admin.state["user"] = SimpleNamespace(is_admin=True)
    ok_resp = await mw.handle(req_true_admin, _next_ok)
    assert ok_resp.status_code == 200


@pytest.mark.asyncio
async def test_benchmark_token_endpoint_disabled_unless_explicitly_enabled_in_debug() -> None:
    # Even if directly mounted or registered, calling /benchmarks/token without enable_benchmarks=True fails closed
    app_prod = _build_test_app(env="production", debug=False, enable_benchmarks=True)
    register_benchmark_routes(app_prod)
    client_prod = TestClient(app_prod)
    resp_prod = await client_prod.get("/benchmarks/token")
    assert resp_prod.status_code in (403, 404)

    app_unmounted = _build_test_app(env="local", debug=True, enable_benchmarks=False)
    register_benchmark_routes(app_unmounted)
    client_unmounted = TestClient(app_unmounted)
    resp_unmounted = await client_unmounted.get("/benchmarks/token")
    assert resp_unmounted.status_code == 404

    app_default = _build_test_app(env="local", debug=True, enable_benchmarks=False)
    app_default.get("/benchmarks/token")(benchmark_token)
    client_default = TestClient(app_default)
    resp_default = await client_default.get("/benchmarks/token")
    assert resp_default.status_code == 403

    app_enabled = _build_test_app(env="local", debug=True, enable_benchmarks=True)
    register_benchmark_routes(app_enabled)
    client_enabled = TestClient(app_enabled)
    resp_enabled = await client_enabled.get("/benchmarks/token")
    resp_enabled.assert_ok()
    assert "token" in resp_enabled.json()


@pytest.mark.asyncio
async def test_auth_logout_clears_auth_user_context_and_admin_middleware_supports_models() -> None:
    """Fix 1: Logout clears Context['auth.user'], AdminMiddleware respects logout and model-backed admin flags, and AuthMiddleware avoids truthiness traps."""
    mw = AdminMiddleware()
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/admin-check",
        "headers": [],
        "query_string": b"",
    }

    async def _recv() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _next_ok(req: Request) -> Response:
        return json_response({"ok": True})

    Context.clear()
    try:
        # 1. Logout clears Context["auth.user"] so AdminMiddleware rejects with 401
        req1 = Request(scope, _recv)
        Context.set("request", req1)
        Context.set("auth.user", {"id": 1, "is_admin": True})
        Context.set("__swiftpy_current_user__", {"id": 1, "is_admin": True})
        req1.state["user"] = {"id": 1, "is_admin": True}

        await auth().logout(req1)
        assert Context.get("auth.user") is None
        assert Context.get("__swiftpy_current_user__") is None
        with pytest.raises(HttpException) as exc_after_logout:
            await mw.handle(req1, _next_ok)
        assert exc_after_logout.value.status_code == 401

        # 2. AdminMiddleware resolves model-backed admin flag on AuthUser(dict_without_is_admin, model=...)
        Context.clear()
        req2 = Request(scope, _recv)
        Context.set("request", req2)
        admin_model: Any = SimpleNamespace(id=7, is_admin=True)
        wrapped_user = AuthUser({"id": 7, "is_admin": None}, model=admin_model)
        Context.set("auth.user", wrapped_user)
        resp_model_admin = await mw.handle(req2, _next_ok)
        assert resp_model_admin.status_code == 200

        # 3. set_user_model synchronizes Context["auth.user"] and model-backed admin lookup
        Context.clear()
        req3 = Request(scope, _recv)
        Context.set("request", req3)
        set_user_model(SimpleNamespace(id=8, is_admin=True))
        resp_set_model = await mw.handle(req3, _next_ok)
        assert resp_set_model.status_code == 200

        # 4. AuthMiddleware uses `user is None` rather than truthiness (falsy non-None user object passes)
        class _FalsyCustomUser:
            def __bool__(self) -> bool:
                return False

        app = _build_test_app()
        auth_mgr = app.container.resolve(AuthManager)
        auth_mgr.extend("falsy_custom", lambda _: lambda _req: _FalsyCustomUser())
        app.config.set("auth.defaults.guard", "falsy_custom")

        @app.get("/falsy-auth", middleware=[AuthMiddleware])
        async def _falsy_endpoint(request: Request) -> Response:
            return json_response(
                {"is_custom": isinstance(request.state.get("user"), _FalsyCustomUser)}
            )

        client = TestClient(app)
        res_falsy = await client.get("/falsy-auth")
        res_falsy.assert_ok()
        assert res_falsy.json() == {"is_custom": True}
    finally:
        Context.clear()

