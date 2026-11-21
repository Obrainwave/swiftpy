from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from swiftpy import TestClient
from swiftpy.auth import (
    AuthProxy,
    AuthUser,
    CurrentUser,
    JWTIssuer,
    auth,
    current_user_model,
    set_user_model,
)
from swiftpy.auth_manager import auth as top_level_auth
from swiftpy.core.application import Application
from swiftpy.core.context import Context
from swiftpy.database.orm import Model
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response


class DummyUser(Model):
    __table__ = "dummy_users"
    __primary_key__ = "id"
    __fillable__: ClassVar[list[str]] = ["name", "email", "password", "is_admin"]
    __hidden__: ClassVar[list[str]] = ["password"]

    id: int
    name: str
    email: str
    password: str
    is_admin: bool


@pytest.fixture(autouse=True)
def _clean_context() -> Any:
    Context.clear()
    yield
    Context.clear()


def _make_dummy_request(headers: dict[str, str] | None = None) -> Request:
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


def test_auth_imports_succeed() -> None:
    # Both canonical top-level import and swiftpy.auth import must be identical
    assert auth is top_level_auth
    proxy = auth()
    assert isinstance(proxy, AuthProxy)


def test_auth_unauthenticated() -> None:
    proxy = auth()
    assert proxy.check() is False
    assert proxy.guest() is True
    assert proxy.user() is None
    assert proxy.id() is None


def test_auth_user_with_model_instance() -> None:
    user = DummyUser(
        id=1,
        name="Alice",
        email="alice@example.com",
        password="supersecretpassword",
        is_admin=True,
    )
    set_user_model(user)

    assert current_user_model() is user

    # auth().check() and auth().guest()
    assert auth().check() is True
    assert auth().guest() is False
    assert auth().id() == 1

    # auth().user()
    current = auth().user()
    assert current is not None
    assert isinstance(current, AuthUser)

    # Attribute-style access
    assert current.id == 1
    assert current.name == "Alice"
    assert current.email == "alice@example.com"
    assert current.is_admin is True

    # Dict-style access
    assert current["id"] == 1
    assert current["name"] == "Alice"
    assert current["email"] == "alice@example.com"
    assert current["is_admin"] is True

    # Hidden fields must strictly NOT be present
    assert "password" not in current
    assert current.get("password") is None
    with pytest.raises(KeyError):
        _ = current["password"]
    with pytest.raises(AttributeError):
        _ = current.password

    # Serialization must NOT expose password
    data = current.to_dict()
    assert "password" not in data
    assert data["id"] == 1
    assert data["name"] == "Alice"
    assert data["email"] == "alice@example.com"
    assert data["is_admin"] is True

    serialized = json.dumps(current)
    assert "supersecretpassword" not in serialized
    assert "password" not in serialized

    # Underlying model instance must be accessible via .model
    assert current.model is user


def test_auth_user_hydrates_declared_fields_from_dict_and_excludes_hidden() -> None:
    # Raw payload in Context (e.g. from JWT or sanitised DB row)
    payload = {
        "sub": "42",
        "name": "Bob",
        "email": "bob@example.com",
        "password": "should_be_omitted",
        "is_admin": False,
        "extra_claim": "member",
    }
    Context.set("__swiftpy_current_user__", payload)

    current = auth().user()
    assert current is not None
    assert auth().check() is True
    assert auth().id() == 42  # Coerced to int matching DummyUser.id: int

    # All declared fields are present
    assert current.name == "Bob"
    assert current.email == "bob@example.com"
    assert current.is_admin is False

    # Extra non-sensitive claim is retained
    assert current["extra_claim"] == "member"

    # Hidden field is strictly excluded
    assert "password" not in current
    assert current.get("password") is None
    with pytest.raises(AttributeError):
        _ = current.password


def test_auth_guard_isolation() -> None:
    user = DummyUser(
        id=7,
        name="Charlie",
        email="charlie@example.com",
        password="secret",
        is_admin=False,
    )
    set_user_model(user, guard="api_key")

    # Asking for "api_key" guard matches
    assert auth("api_key").check() is True
    api_user = auth("api_key").user()
    assert api_user is not None
    assert api_user.id == 7

    # Asking for "jwt" guard must fail because active guard is "api_key"
    assert auth("jwt").check() is False
    assert auth("jwt").user() is None

    # Default proxy (no guard specified) matches any active authenticated user
    assert auth().check() is True
    assert auth().user() is not None


def test_current_user_di_binding_supports_attribute_access() -> None:
    payload = {
        "id": 10,
        "name": "David",
        "email": "david@example.com",
        "role": "manager",
    }
    Context.set("__swiftpy_current_user__", payload)

    current = CurrentUser()
    assert current.is_authenticated is True
    assert current.id == 10
    assert current.email == "david@example.com"
    assert current.name == "David"
    assert current.role == "manager"
    assert current["name"] == "David"


@pytest.mark.asyncio
async def test_auth_proxy_authenticate_delegates_to_guard() -> None:
    app = Application(config={}, auto_discover_providers=False)

    class CustomGuard:
        async def authenticate(self, request: Request | None = None) -> dict[str, Any]:
            user_data = {"id": 99, "name": "Eve", "email": "eve@example.com"}
            Context.set("__swiftpy_current_user__", user_data)
            Context.set("__swiftpy_auth_guard__", "custom")
            return user_data

    from swiftpy.auth import AuthManager

    auth_mgr = app.container.resolve(AuthManager) if app.container.has(AuthManager) else AuthManager(app.container)
    auth_mgr.extend("custom", lambda _: CustomGuard())
    app.container.instance(AuthManager, auth_mgr)

    req = _make_dummy_request()
    res = await auth("custom").authenticate(req)
    assert res == {"id": 99, "name": "Eve", "email": "eve@example.com"}

    # After authenticate, auth() and auth("custom") reflect the user
    assert auth().check() is True
    assert auth("custom").check() is True
    custom_user = auth("custom").user()
    assert custom_user is not None
    assert custom_user.name == "Eve"
    assert auth("jwt").check() is False


def test_auth_user_attribute_mutations_and_repr() -> None:
    u = AuthUser({"id": 1, "name": "Frank"})
    assert repr(u) == "AuthUser({'id': 1, 'name': 'Frank'})"
    u.name = "Franklin"
    assert u.name == "Franklin"
    assert u["name"] == "Franklin"
    assert u.to_dict()["name"] == "Franklin"


def test_auth_user_fallback_without_model() -> None:
    payload = {
        "id": "user-xyz",
        "email": "xyz@example.com",
        "password": "hashed_pw",
        "secret": "top_secret",
        "key_hash": "deadbeef",
        "role": "staff",
    }
    Context.set("__swiftpy_current_user__", payload)
    current = auth().user()
    assert current is not None
    assert current.id == "user-xyz"
    assert current.email == "xyz@example.com"
    assert current.role == "staff"
    assert "password" not in current
    assert "secret" not in current
    assert "key_hash" not in current


@pytest.mark.asyncio
async def test_current_user_and_auth_immediately_available_when_access_token_is_issued() -> None:
    """
    Issuing an access_token (via issue_access_token, issue_token_pair, or refresh_token_pair)
    must immediately populate CurrentUser (including already-injected instances) and auth().
    """
    secret = "test-jwt-secret-key-at-least-32-bytes-long!!"
    app = Application(
        config={
            "app": {"name": "AuthIssueTest", "env": "testing", "debug": True},
            "auth": {
                "blacklist_enabled": False,
                "passive_resolution": True,
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
        },
        auto_discover_providers=False,
    )

    @app.post("/auth/login-with-id")
    async def login_with_id(
        request: Request,
        jwt: JWTIssuer,
        current: CurrentUser,
    ) -> Response:
        # Before token issuance, CurrentUser and auth() are unauthenticated
        assert current.is_authenticated is False
        assert auth().check() is False

        # Instantiate DummyUser model in this request task, then issue token with user.id
        user = DummyUser(
            id=77,
            name="Grace Hopper",
            email="grace@example.com",
            password="hashed_secret_password",
            is_admin=True,
        )
        pair = jwt.issue_token_pair(user.id, user=user)

        # Immediately after access_token is issued, both CurrentUser and auth() are available!
        assert current.is_authenticated is True
        assert current.id == 77
        assert current.name == "Grace Hopper"
        assert current.email == "grace@example.com"
        assert "password" not in current

        assert auth().check() is True
        assert auth().guest() is False
        assert auth().id() == 77
        assert auth("jwt").check() is True
        auth_user = auth().user()
        assert auth_user is not None
        assert auth_user.id == 77
        assert auth_user.name == "Grace Hopper"
        assert auth_user.email == "grace@example.com"
        assert auth_user.is_admin is True
        assert auth_user.model is user
        assert "password" not in auth_user

        return json_response(
            {
                "access_token": pair["access_token"],
                "refresh_token": pair["refresh_token"],
                "current_user_id": current.id,
                "auth_id": auth().id(),
                "auth_name": auth_user.name,
            }
        )

    @app.post("/auth/login-with-model")
    async def login_with_model(
        jwt: JWTIssuer,
        current: CurrentUser,
    ) -> Response:
        user = DummyUser(
            id=88,
            name="Alan Turing",
            email="alan@example.com",
            password="secret_password",
            is_admin=False,
        )
        token = jwt.issue_access_token(user)
        assert current.is_authenticated is True
        assert current.id == 88
        assert current.name == "Alan Turing"
        assert auth().check() is True
        assert auth().id() == 88
        u = auth().user()
        assert u is not None
        assert u.email == "alan@example.com"
        return json_response({"access_token": token, "id": current.id, "email": u.email})

    @app.get("/profile-without-guard")
    async def profile_without_guard(current: CurrentUser) -> Response:
        # With auth.passive_resolution enabled, a valid Bearer access_token populates CurrentUser & auth()
        # even without middleware=[JWTGuard]. CurrentUser.id stays the raw `sub` string (as with JWTGuard).
        u = auth().user()
        return json_response(
            {
                "is_authenticated": current.is_authenticated,
                "current_id": current.id,
                "auth_check": auth().check(),
                "auth_id": auth().id(),
                "email": u.email if u is not None else None,
            }
        )

    async with TestClient(app) as client:
        res1 = await client.post("/auth/login-with-id", json={})
        res1.assert_ok()
        body1 = res1.json()
        assert body1["current_user_id"] == 77
        assert body1["auth_id"] == 77
        assert body1["auth_name"] == "Grace Hopper"

        res2 = await client.post("/auth/login-with-model", json={})
        res2.assert_ok()
        body2 = res2.json()
        assert body2["id"] == 88
        assert body2["email"] == "alan@example.com"

        # Issue token with claims and hit route without explicit JWTGuard
        jwt_issuer = app.container.resolve(JWTIssuer)
        token_with_claims = jwt_issuer.issue_access_token(
            99,
            claims={"email": "katherine@example.com", "name": "Katherine Johnson"},
        )
        Context.clear()

        res3 = await client.get(
            "/profile-without-guard",
            headers={"Authorization": f"Bearer {token_with_claims}"},
        )
        res3.assert_ok()
        body3 = res3.json()
        assert body3["is_authenticated"] is True
        assert body3["current_id"] == "99"
        assert body3["auth_check"] is True
        assert body3["auth_id"] == 99
        assert body3["email"] == "katherine@example.com"
