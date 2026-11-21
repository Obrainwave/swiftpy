from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest

from swiftpy import Application, Request, Response, Router, TestClient, json_response
from swiftpy.auth import (
    CurrentUser,
    JWTGuard,
    JWTIssuer,
    TokenBlacklist,
    auth,
    current_user_model,
    set_user_model,
    user_context,
)
from swiftpy.core.context import Context
from swiftpy.database.orm import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import QueryError

SECRET = "context-on-issue-secret-key-at-least-32-bytes!"


class Account(Model):
    __table__ = "ctx_accounts"
    __primary_key__ = "id"
    __fillable__: ClassVar[list[str]] = ["name", "email", "password"]
    __hidden__: ClassVar[list[str]] = ["password"]

    id: int
    name: str
    email: str
    password: str


class Post(Model):
    __table__ = "ctx_posts"
    __primary_key__ = "id"
    __fillable__: ClassVar[list[str]] = ["title"]

    id: int
    title: str


class HydratedAccount(Model):
    """User model whose `find()` is counted and never touches a database."""

    __table__ = "ctx_hydrated_accounts"
    __primary_key__ = "id"
    __fillable__: ClassVar[list[str]] = ["name", "email"]

    id: int
    name: str
    email: str

    find_calls: ClassVar[list[Any]] = []
    fail_with: ClassVar[Exception | None] = None

    @classmethod
    async def find(cls, id: Any) -> HydratedAccount | None:
        cls.find_calls.append(id)
        if cls.fail_with is not None:
            raise cls.fail_with
        return cls(id=int(id), name="Hydrated Ada", email="ada@db.test")


class FakePool(DatabasePool):
    """Pool stand-in that reports itself initialized without connecting."""

    def __init__(self) -> None:
        pass

    @property
    def is_initialized(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _clean_context() -> Iterator[None]:
    Context.clear()
    HydratedAccount.find_calls.clear()
    HydratedAccount.fail_with = None
    yield
    Context.clear()


def _app(*, auth_options: dict[str, Any] | None = None, blacklist: bool = False) -> Application:
    return Application(
        config={
            "app": {"name": "CtxOnIssue", "env": "testing", "debug": True},
            "auth": {
                "blacklist_enabled": blacklist,
                "defaults": {"guard": "jwt"},
                "guards": {"jwt": {"secret": SECRET, "algorithm": "HS256"}},
                **(auth_options or {}),
            },
            "redis": {"host": "127.0.0.1", "port": 26379, "password": "secret"},
        },
        auto_discover_providers=False,
    )


def _issuer() -> JWTIssuer:
    return JWTIssuer(secret=SECRET)


def test_issue_with_model_mapping_and_user_kwarg_binds_context() -> None:
    issuer = _issuer()

    account = Account(id=3, name="Ada", email="ada@example.com", password="hash")
    issuer.issue_access_token(account)
    user = auth().user()
    assert user is not None
    assert user.id == 3
    assert user.model is account
    assert "password" not in user

    Context.clear()
    issuer.issue_access_token({"id": 4, "name": "Bob", "password": "x", "secret": "y"})
    mapped = auth().user()
    assert mapped is not None
    assert mapped.name == "Bob"
    assert "password" not in mapped
    assert "secret" not in mapped

    Context.clear()
    issuer.issue_token_pair(3, user=account)
    assert auth().id() == 3
    assert current_user_model() is account


def test_issue_rejects_user_that_does_not_match_subject() -> None:
    account = Account(id=3, name="Ada", email="ada@example.com", password="hash")
    with pytest.raises(ValueError, match="does not match"):
        _issuer().issue_access_token(9, user=account)
    with pytest.raises(ValueError, match="does not match"):
        _issuer().issue_access_token(9, user={"id": 3})
    assert auth().check() is False


def test_issue_for_unsaved_model_without_primary_key_raises() -> None:
    with pytest.raises(ValueError, match="primary key"):
        _issuer().issue_access_token(Account(name="No Id"))


def test_refresh_token_never_binds_context() -> None:
    _issuer().issue_refresh_token(5)
    assert auth().check() is False


def test_issue_does_not_bind_a_model_of_another_class_with_same_pk() -> None:
    post = Post(id=5, title="not a user")
    _issuer().issue_access_token(5)

    user = auth().user()
    assert user is not None
    assert user.model is None
    assert "title" not in user
    assert current_user_model() is not post


def test_issue_for_other_subject_keeps_authenticated_user() -> None:
    Context.set("__swiftpy_current_user__", {"sub": "900", "id": 900})
    issuer = _issuer()

    issuer.issue_access_token(555)
    issuer.issue_token_pair(556)

    assert auth().id() == 900


@pytest.mark.asyncio
async def test_refresh_token_pair_binds_when_unauthenticated_and_preserves_admin() -> None:
    issuer = _issuer()
    pair = issuer.issue_token_pair(42, claims={"email": "grace@example.com"})
    Context.clear()

    refreshed = await issuer.refresh_token_pair(
        pair["refresh_token"], claims={"email": "grace@example.com"}
    )
    assert refreshed["access_token"]
    refreshed_user = auth().user()
    assert refreshed_user is not None
    assert refreshed_user.email == "grace@example.com"

    Context.clear()
    Context.set("__swiftpy_current_user__", {"sub": "900", "id": 900})
    pair2 = issuer.issue_token_pair(43)
    await issuer.refresh_token_pair(pair2["refresh_token"])
    assert auth().id() == 900


@pytest.mark.asyncio
async def test_passive_resolution_is_off_by_default() -> None:
    app = _app()

    @app.get("/open")
    async def open_route(request: Request) -> Response:
        return json_response({"check": auth().check()})

    token = app.container.resolve(JWTIssuer).issue_access_token(1)
    Context.clear()
    async with TestClient(app) as client:
        res = await client.get("/open", headers={"Authorization": f"Bearer {token}"})
    res.assert_ok()
    assert res.json() == {"check": False}


@pytest.mark.asyncio
async def test_passive_resolution_rejects_revoked_token() -> None:
    app = _app(auth_options={"passive_resolution": True}, blacklist=True)
    blacklist = TokenBlacklist(
        prefix=f"test_ctx_issue_{uuid.uuid4().hex[:8]}:",
        config=app.config,
        container=app.container,
    )
    app.container.instance(TokenBlacklist, blacklist)
    await blacklist.clear()

    @app.get("/open")
    async def open_route(request: Request, current: CurrentUser) -> Response:
        return json_response(
            {"check": auth().check(), "id": auth().id(), "current": current.is_authenticated}
        )

    jwt = app.container.resolve(JWTIssuer)
    valid = jwt.issue_access_token(7)
    revoked = jwt.issue_access_token(8)
    await blacklist.revoke_token(revoked, issuer=jwt)
    Context.clear()

    try:
        async with TestClient(app) as client:
            ok = await client.get("/open", headers={"Authorization": f"Bearer {valid}"})
            bad = await client.get("/open", headers={"Authorization": f"Bearer {revoked}"})
    finally:
        await blacklist.clear()
        await blacklist.close()

    assert ok.json()["check"] is True
    assert ok.json()["current"] is True
    assert bad.status_code == 200
    assert bad.json() == {"check": False, "id": None, "current": False}


@pytest.mark.asyncio
async def test_hydrate_user_is_opt_in_and_costs_one_query_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_context, "resolve_user_model_class", lambda: HydratedAccount)

    async def run(options: dict[str, Any]) -> dict[str, Any]:
        HydratedAccount.find_calls.clear()
        app = _app(auth_options=options)
        app.container.instance(DatabasePool, FakePool())
        guarded = Router(middleware=[JWTGuard])

        @guarded.get("/me")
        async def me(current: CurrentUser) -> Response:
            return json_response(
                {"email": current.get("email"), "model": type(current_user_model()).__name__}
            )

        app.include_router(guarded)
        token = app.container.resolve(JWTIssuer).issue_access_token("11")
        Context.clear()
        async with TestClient(app) as client:
            res = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
        res.assert_ok()
        return {**res.json(), "queries": len(HydratedAccount.find_calls)}

    off = await run({})
    assert off == {"email": None, "model": "NoneType", "queries": 0}

    on = await run({"hydrate_user": True})
    assert on == {"email": "ada@db.test", "model": "HydratedAccount", "queries": 1}


@pytest.mark.asyncio
async def test_hydrate_user_degrades_to_claims_when_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_context, "resolve_user_model_class", lambda: HydratedAccount)
    HydratedAccount.fail_with = QueryError("boom")
    app = _app(auth_options={"hydrate_user": True})
    app.container.instance(DatabasePool, FakePool())
    guarded = Router(middleware=[JWTGuard])

    @guarded.get("/me")
    async def me(current: CurrentUser) -> Response:
        return json_response({"id": current.id, "authenticated": current.is_authenticated})

    app.include_router(guarded)
    token = app.container.resolve(JWTIssuer).issue_access_token("12")
    Context.clear()
    async with TestClient(app) as client:
        res = await client.get("/me", headers={"Authorization": f"Bearer {token}"})

    res.assert_ok()
    assert res.json() == {"id": "12", "authenticated": True}
    assert HydratedAccount.find_calls == [12]


def test_set_user_model_still_wins_for_matching_subject() -> None:
    account = Account(id=21, name="Pre", email="pre@example.com", password="h")
    set_user_model(account, guard="jwt")
    _issuer().issue_access_token(21)
    assert current_user_model() is account
