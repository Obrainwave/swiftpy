from __future__ import annotations

from collections.abc import Generator
from typing import Any, ClassVar

import pytest

from swiftpy import Auth, FormRequest, Hash
from swiftpy.auth.events import Attempting, Failed, Login
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.core.container import Container, bind_container
from swiftpy.core.context import Context
from swiftpy.database.orm.model import (
    Model,
    restore_model_registry,
    snapshot_model_registry,
)
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.http.response import json_response
from swiftpy.schemas import BaseSchema, Field
from swiftpy.testing.http_client import TestClient


class _SchemaAuthAccount(Model):
    __table__ = "schema_auth_users"
    __primary_key__ = "id"
    __fillable__: ClassVar[list[str]] = ["name", "email", "password", "tenant_id"]
    __hidden__: ClassVar[list[str]] = ["password", "remember_token"]

    id: int
    name: str
    email: str
    password: str
    tenant_id: str | None = None


class _FakeAuthQuery:
    def __init__(self, users: list[_SchemaAuthAccount]) -> None:
        self.users = users
        self.filters: list[tuple[str, Any]] = []

    def where(self, col: str, val: Any) -> _FakeAuthQuery:
        self.filters.append((col, val))
        return self

    async def first(self) -> _SchemaAuthAccount | None:
        for user in self.users:
            if all(getattr(user, col, None) == val for col, val in self.filters):
                return user
        return None


class _LoginCredentialsSchema(BaseSchema):
    email: str = Field(..., to_lower=True, strip_whitespace=True)
    password: str = Field(..., min_length=6)
    tenant_id: str | None = None


class _ExcludedPasswordLoginSchema(BaseSchema):
    email: str = Field(..., to_lower=True, strip_whitespace=True)
    password: str = Field(..., min_length=6, exclude=True)
    tenant_id: str | None = None


class _LoginFormRequest(FormRequest[_ExcludedPasswordLoginSchema]):
    def rules(self) -> dict[str, Any]:
        return {
            "email": "required|email",
            "password": "required|str|min:6",
        }


@pytest.fixture(autouse=True)
def _isolated_auth_state() -> Generator[None, None, None]:
    snap = snapshot_model_registry()
    Context.clear()
    EventDispatcher.set_instance(EventDispatcher())
    container = Container()
    cfg = Config(
        {
            "auth": {
                "model": _SchemaAuthAccount,
                "guards": {"jwt": {"secret": "test-schema-auth-secret-32-bytes-long!"}},
                "throttle": {"enabled": False},
            }
        }
    )
    container.instance(Config, cfg)
    with bind_container(container):
        try:
            yield
        finally:
            Context.clear()
            EventDispatcher.set_instance(None)
            restore_model_registry(snap)


async def test_auth_attempt_once_and_attempt_when_accept_base_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashed = await Hash.make_async("super-secret-123")
    user = _SchemaAuthAccount(
        id=42,
        name="Ada Lovelace",
        email="ada@example.com",
        password=hashed,
        tenant_id="org-1",
    )
    recorded_queries: list[_FakeAuthQuery] = []

    def _query_factory(cls: type[_SchemaAuthAccount]) -> _FakeAuthQuery:
        _ = cls
        q = _FakeAuthQuery([user])
        recorded_queries.append(q)
        return q

    monkeypatch.setattr(_SchemaAuthAccount, "query", classmethod(_query_factory))

    events: list[Any] = []
    dispatcher = EventDispatcher.get_instance()
    assert dispatcher is not None
    dispatcher.listen(Attempting, events.append)
    dispatcher.listen(Login, events.append)
    dispatcher.listen(Failed, events.append)

    # 1. Auth.attempt(schema) succeeds and omits unset optional fields from SQL filters
    valid_creds = _LoginCredentialsSchema.validate(
        {"email": "  ADA@Example.com  ", "password": "super-secret-123"}
    )
    assert await Auth.attempt(valid_creds) is True
    assert Auth.check() is True
    assert Auth.id() == 42
    assert recorded_queries[-1].filters == [("email", "ada@example.com")]
    attempting_ev = next(e for e in events if isinstance(e, Attempting))
    assert attempting_ev.credentials == {"email": "ada@example.com"}
    assert "password" not in attempting_ev.credentials

    # 2. Invalid password records Failed without leaking password
    bad_creds = _LoginCredentialsSchema.validate(
        {"email": "ada@example.com", "password": "wrong-password", "tenant_id": "org-1"}
    )
    Context.clear()
    assert await Auth.attempt(bad_creds) is False
    assert recorded_queries[-1].filters == [
        ("email", "ada@example.com"),
        ("tenant_id", "org-1"),
    ]
    failed_ev = next(e for e in events if isinstance(e, Failed))
    assert "password" not in failed_ev.credentials

    # 3. Auth.attempt_when and Auth.once accept BaseSchema
    Context.clear()
    assert (
        await Auth.attempt_when(
            valid_creds, lambda u: getattr(u, "tenant_id", None) == "org-1"
        )
        is True
    )
    Context.clear()
    assert await Auth.once(valid_creds) is True
    assert Auth.check() is True
    assert Auth.tokens() is None

    # 4. Route auto-injection feeds BaseSchema straight into Auth.attempt
    Context.clear()
    app = Application(debug=False)
    app.container.instance(
        Config,
        Config(
            {
                "auth": {
                    "model": _SchemaAuthAccount,
                    "guards": {
                        "jwt": {"secret": "test-schema-auth-secret-32-bytes-long!"}
                    },
                    "throttle": {"enabled": False},
                }
            }
        ),
    )

    @app.post("/login")
    async def login_handler(payload: _LoginCredentialsSchema) -> Any:
        ok = await Auth.attempt(payload)
        if not ok:
            return json_response({"detail": "Invalid credentials"}, status_code=401)
        return {"user_id": Auth.id(), "access_token": Auth.access_token()}

    async with TestClient(app) as client:
        res = await client.post(
            "/login",
            json={"email": "ada@example.com", "password": "super-secret-123"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["user_id"] == 42
        assert isinstance(body["access_token"], str) and body["access_token"]

    # 5. BaseSchema with password Field(..., exclude=True) and FormRequest instances work with Auth.attempt / once
    excluded_creds = _ExcludedPasswordLoginSchema.validate(
        {"email": "ada@example.com", "password": "super-secret-123"}
    )
    assert "password" not in excluded_creds.to_dict()
    Context.clear()
    assert await Auth.attempt(excluded_creds) is True
    assert Auth.id() == 42

    form_creds = _LoginFormRequest(
        data={"email": "ada@example.com", "password": "super-secret-123"}
    )
    Context.clear()
    assert await Auth.attempt(form_creds) is True
    assert Auth.id() == 42
    Context.clear()
    assert await Auth.once(form_creds) is True
    assert Auth.id() == 42
