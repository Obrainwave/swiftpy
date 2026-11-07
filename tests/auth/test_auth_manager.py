from __future__ import annotations

from typing import Any

import pytest

from swiftpy.auth.auth_manager import AuthManager, CurrentUser
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.core.application import Application
from swiftpy.core.context import Context
from swiftpy.http.request import Request


def _make_request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": raw_headers,
        "query_string": b"",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_auth_manager_honors_custom_guard_registered_as_jwt() -> None:
    app = Application(config={}, auto_discover_providers=False)
    auth = AuthManager(app.container)
    auth.register_container_bindings(app.container)

    called_with: list[Request | None] = []

    class CustomJWTGuard:
        async def authenticate(self, request: Request | None = None) -> dict[str, Any]:
            called_with.append(request)
            user_data = {"id": "custom-jwt-user", "role": "admin"}
            Context.set("__swiftpy_current_user__", user_data)
            return user_data

    auth.extend("jwt", lambda _: CustomJWTGuard())

    req = _make_request()
    Context.set("request", req)
    try:
        result = await auth.authenticate(req)
        assert result == {"id": "custom-jwt-user", "role": "admin"}
        assert called_with == [req]

        # Explicit guard_name="jwt" also uses the custom guard instance
        result2 = await auth.authenticate(req, guard_name="jwt")
        assert result2 == {"id": "custom-jwt-user", "role": "admin"}
        assert len(called_with) == 2
    finally:
        Context.clear()


@pytest.mark.asyncio
async def test_auth_manager_default_builtin_jwt_guard_authenticate() -> None:
    secret = "s" * 40
    app = Application(
        config={
            "auth": {
                "defaults": {"guard": "jwt"},
                "jwt": {"secret": secret, "algorithm": "HS256"},
            }
        },
        auto_discover_providers=False,
    )
    issuer = JWTIssuer(secret=secret)
    app.container.instance(JWTIssuer, issuer)

    auth = AuthManager(app.container)
    token = issuer.issue_access_token("user-99", claims={"email": "u99@example.com"})

    req = _make_request({"Authorization": f"Bearer {token}"})
    Context.set("request", req)
    try:
        payload = await auth.authenticate(req)
        assert payload["sub"] == "user-99"
        assert payload["email"] == "u99@example.com"
    finally:
        Context.clear()


@pytest.mark.asyncio
async def test_current_user_resolved_before_auth_does_not_stay_empty() -> None:
    app = Application(config={}, auto_discover_providers=False)
    Context.clear()

    try:
        # 1. Resolve CurrentUser BEFORE authentication runs
        early_user = app.container.resolve(CurrentUser)
        assert early_user.is_authenticated is False
        assert early_user.id is None
        assert dict(early_user) == {}

        # 2. Simulate authentication middleware setting the user in Context
        Context.set(
            "__swiftpy_current_user__",
            {"id": 42, "email": "alice@example.com", "role": "member"},
        )

        # 3. Resolving CurrentUser AFTER auth must NOT return a cached empty user
        post_auth_user = app.container.resolve(CurrentUser)
        assert post_auth_user.is_authenticated is True
        assert post_auth_user.id == 42
        assert post_auth_user.email == "alice@example.com"
        assert post_auth_user["role"] == "member"

        # 4. Accessing the earlier reference also lazily reflects the authenticated user
        assert early_user.is_authenticated is True
        assert early_user.id == 42
        assert early_user.email == "alice@example.com"
        assert early_user.get("role") == "member"
    finally:
        Context.clear()

