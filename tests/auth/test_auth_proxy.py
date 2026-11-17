from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from swiftpy.auth import (
    AuthProxy,
    AuthUser,
    CurrentUser,
    auth,
    current_user_model,
    set_user_model,
)
from swiftpy.auth_manager import auth as top_level_auth
from swiftpy.core.application import Application
from swiftpy.core.context import Context
from swiftpy.database.orm import Model
from swiftpy.http.request import Request


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
    assert auth("api_key").user() is not None
    assert auth("api_key").user().id == 7

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
    assert auth("custom").user().name == "Eve"
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
