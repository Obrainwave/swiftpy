from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any, ClassVar

import pytest

from swiftpy import Auth, Callback, Hash, TestClient
from swiftpy.auth.auth_manager import _DUMMY_BCRYPT_HASH
from swiftpy.auth.events import Attempting, Authenticated, Failed, Login, Logout
from swiftpy.auth.user_context import format_remember_set_cookie
from swiftpy.core.application import Application
from swiftpy.core.container import bind_container
from swiftpy.core.context import Context
from swiftpy.database.orm.model import (
    Model,
    restore_model_registry,
    snapshot_model_registry,
)
from swiftpy.database.orm.relationships import HasOne
from swiftpy.database.pool import DatabasePool
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.http.request import Request
from swiftpy.http.response import json_response


class AttemptUser(Model):
    __table__ = "test_attempt_users"
    __primary_key__ = "id"
    __fillable__: ClassVar[list[str]] = [
        "name",
        "email",
        "password",
        "active",
        "banned",
        "login_level",
        "remember_token",
    ]
    __hidden__: ClassVar[list[str]] = ["password", "remember_token"]
    __guarded__: ClassVar[list[str]] = []

    id: int
    name: str
    email: str
    password: str
    active: bool
    banned: bool
    login_level: int = 1
    remember_token: str | None = None

    activeSubscription = HasOne("AttemptSubscription", foreign_key="user_id")

    def is_not_banned(self) -> bool:
        return not bool(self.banned)


class AttemptSubscription(Model):
    __table__ = "test_attempt_subscriptions"
    __primary_key__ = "id"
    __guarded__: ClassVar[list[str]] = []

    id: int
    user_id: int
    plan: str
    active: bool


@pytest.fixture(scope="module", autouse=True)
def _preserve_model_registry() -> Generator[None, None, None]:
    snap = snapshot_model_registry()
    yield
    restore_model_registry(snap)


@pytest.fixture(autouse=True)
def _reset_context_and_events() -> Generator[None, None, None]:
    Context.clear()
    EventDispatcher.set_instance(EventDispatcher())
    yield
    Context.clear()
    EventDispatcher.set_instance(None)


@pytest.fixture
async def attempt_app(ensure_db_pool: DatabasePool) -> Application:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS test_attempt_subscriptions, test_attempt_users CASCADE;
            CREATE TABLE test_attempt_users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                banned BOOLEAN NOT NULL DEFAULT FALSE,
                login_level INTEGER NOT NULL DEFAULT 1,
                remember_token VARCHAR(100) NULL
            );
            CREATE TABLE test_attempt_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES test_attempt_users(id) ON DELETE CASCADE,
                plan TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE
            );
            """
        )

    pw_hash = Hash.make("secret123", rounds=4)
    alice = await AttemptUser.create(
        {
            "name": "Alice",
            "email": "alice@example.com",
            "password": pw_hash,
            "active": True,
            "banned": False,
        }
    )
    await AttemptSubscription.create(
        {"user_id": alice.id, "plan": "pro", "active": True}
    )
    await AttemptUser.create(
        {
            "name": "InactiveBob",
            "email": "bob@example.com",
            "password": pw_hash,
            "active": False,
            "banned": False,
        }
    )
    banned_carol = await AttemptUser.create(
        {
            "name": "BannedCarol",
            "email": "carol@example.com",
            "password": pw_hash,
            "active": True,
            "banned": True,
        }
    )
    await AttemptSubscription.create(
        {"user_id": banned_carol.id, "plan": "pro", "active": True}
    )

    app = Application()
    app.config.set("app.key", "base64:dGVzdC1hcHAta2V5LTMyLWJ5dGVzLWZvci1zd2lmdHB5IQ==")
    app.config.set("auth.model", AttemptUser)
    app.config.set("auth.throttle.enabled", True)
    app.config.set("auth.throttle.max_attempts", 5)
    app.config.set("auth.guards.jwt.secret", "jwt-test-secret-with-32-bytes-minimum!!")
    app.container.instance(DatabasePool, ensure_db_pool)
    return app


def test_request_cookies_parsing() -> None:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (b"cookie", b'theme="dark"; swiftpy_remember=abc%3D123; token=x=y=z'),
                (b"cookie", b"lang=en"),
            ],
        },
        receive,
    )
    assert req.cookies == {
        "theme": "dark",
        "swiftpy_remember": "abc=123",
        "token": "x=y=z",
        "lang": "en",
    }
    assert req.cookie("token") == "x=y=z"
    assert req.cookie("missing", "fallback") == "fallback"


@pytest.mark.asyncio
async def test_attempt_basic_and_extra_bool_conditions(attempt_app: Application) -> None:
    with bind_container(attempt_app.container):
        # Wrong password & missing user both return False
        assert (
            await Auth.attempt({"email": "alice@example.com", "password": "wrong"})
            is False
        )
        assert Auth.guest() is True
        assert (
            await Auth.attempt({"email": "nobody@example.com", "password": "wrong"})
            is False
        )

        # Valid login + active=1 int->bool coercion
        ok = await Auth.attempt(
            {"email": "alice@example.com", "password": "secret123", "active": 1}
        )
        assert ok is True
        assert Auth.check() is True
        user = Auth.user()
        assert user is not None
        assert user.name == "Alice"
        assert "password" not in user.to_dict()
        assert "remember_token" not in user.to_dict()
        assert Auth.access_token() is not None
        assert Auth.refresh_token() is not None

        await Auth.logout()
        assert Auth.check() is False

        # Inactive user fails when active=1 is required
        assert (
            await Auth.attempt(
                {"email": "bob@example.com", "password": "secret123", "active": 1}
            )
            is False
        )


@pytest.mark.asyncio
async def test_attempt_with_callback_and_has(attempt_app: Application) -> None:
    with bind_container(attempt_app.container):
        # Closure passed via Callback key as requested
        assert (
            await Auth.attempt(
                {
                    "email": "alice@example.com",
                    "password": "secret123",
                    Callback(lambda q: q.has("activeSubscription")): True,
                }
            )
            is True
        )
        await Auth.logout()

        # User without activeSubscription fails closure condition
        assert (
            await Auth.attempt(
                {
                    "email": "bob@example.com",
                    "password": "secret123",
                    "sub": lambda q: q.has("activeSubscription"),
                }
            )
            is False
        )


@pytest.mark.asyncio
async def test_attempt_when_sync_and_async_closures(attempt_app: Application) -> None:
    with bind_container(attempt_app.container):
        # Allowed user passes sync Callback check
        assert (
            await Auth.attempt_when(
                {"email": "alice@example.com", "password": "secret123"},
                Callback(lambda user: user.is_not_banned()),
            )
            is True
        )
        await Auth.logout()

        # Banned user fails attemptWhen camelCase check
        assert (
            await Auth.attemptWhen(
                {"email": "carol@example.com", "password": "secret123"},
                Callback(lambda user: user.is_not_banned()),
            )
            is False
        )
        assert Auth.guest() is True

        # Async predicate also supported cleanly
        async def allow_alice(user: AttemptUser) -> bool:
            await asyncio.sleep(0)
            return user.name == "Alice"

        await Auth.clear_throttle("carol@example.com")
        assert (
            await Auth.attempt_when(
                {"email": "carol@example.com", "password": "secret123"},
                allow_alice,
            )
            is False
        )


@pytest.mark.asyncio
async def test_remember_me_http_cookie_lifecycle(attempt_app: Application) -> None:
    @attempt_app.post("/login")
    async def login_route(request: Request) -> Any:
        payload = await request.json()
        ok = await Auth.attempt(
            {"email": payload["email"], "password": payload["password"]},
            remember=bool(payload.get("remember", False)),
        )
        return json_response(
            {"ok": ok, "via_remember": Auth.via_remember(), "id": Auth.id()},
            status_code=200 if ok else 401,
        )

    @attempt_app.get("/me")
    async def me_route() -> Any:
        if Auth.guest():
            return json_response({"authenticated": False}, status_code=401)
        u = Auth.user()
        return json_response(
            {
                "authenticated": True,
                "id": Auth.id(),
                "email": u.email if u is not None else None,
                "via_remember": Auth.via_remember(),
            }
        )

    @attempt_app.post("/logout")
    async def logout_route() -> Any:
        await Auth.logout()
        return json_response({"logged_out": True})

    client = TestClient(attempt_app)

    res = await client.post(
        "/login",
        json={"email": "alice@example.com", "password": "secret123", "remember": True},
    )
    assert res.status_code == 200
    assert res.json()["via_remember"] is False

    set_cookies = res.headers.getlist("set-cookie")
    remember_hdr = next(
        val for val in set_cookies if "swiftpy_remember=" in val
    )
    assert "HttpOnly" in remember_hdr
    cookie_pair = remember_hdr.split(";", 1)[0]

    # DB has non-empty remember_token
    alice = await AttemptUser.where("email", "alice@example.com").first_or_fail()
    old_token = alice.remember_token
    assert isinstance(old_token, str) and len(old_token) > 20

    # Subsequent request authenticated solely by cookie has via_remember() == True
    me_res = await client.get("/me", headers={"Cookie": cookie_pair})
    assert me_res.status_code == 200
    assert me_res.json()["authenticated"] is True
    assert me_res.json()["email"] == "alice@example.com"
    assert me_res.json()["via_remember"] is True

    # Tampered cookie signature rejected
    tampered_res = await client.get(
        "/me",
        headers={"Cookie": f"{cookie_pair}tampered"},
    )
    assert tampered_res.status_code == 401

    # Logout clears remember_token in DB and expires cookie
    out_res = await client.post("/logout", headers={"Cookie": cookie_pair})
    assert out_res.status_code == 200
    out_cookies = out_res.headers.getlist("set-cookie")
    assert any("Max-Age=0" in c for c in out_cookies)

    reloaded = await AttemptUser.find(alice.id)
    assert reloaded is not None
    assert reloaded.remember_token is None

    # Old cookie no longer works after logout
    replay_res = await client.get("/me", headers={"Cookie": cookie_pair})
    assert replay_res.status_code == 401


@pytest.mark.asyncio
async def test_login_using_id_and_once_and_events(attempt_app: Application) -> None:
    events_seen: list[str] = []
    attempting_payloads: list[dict[str, Any]] = []

    def _on_attempting(ev: Attempting) -> None:
        events_seen.append("attempting")
        attempting_payloads.append(ev.credentials)

    dispatcher = EventDispatcher.get_instance()
    dispatcher.listen(Attempting, _on_attempting)
    dispatcher.listen(Authenticated, lambda _: events_seen.append("authenticated"))
    dispatcher.listen(Login, lambda _: events_seen.append("login"))
    dispatcher.listen(Failed, lambda _: events_seen.append("failed"))
    dispatcher.listen(Logout, lambda _: events_seen.append("logout"))

    with bind_container(attempt_app.container):
        alice = await AttemptUser.where("email", "alice@example.com").first_or_fail()

        # once() authenticates single request without Login event or tokens/cookies
        assert (
            await Auth.once({"email": "alice@example.com", "password": "secret123"})
            is True
        )
        assert Auth.check() is True
        assert Auth.tokens() is None
        assert "authenticated" in events_seen
        assert "login" not in events_seen

        events_seen.clear()
        Context.clear()

        # login_using_id & aliases (loginUsingId, login_sing_id)
        logged_user = await Auth.login_sing_id(alice.id)
        assert isinstance(logged_user, AttemptUser)
        assert logged_user.id == alice.id
        assert Auth.id() == alice.id
        assert "login" in events_seen

        assert await Auth.login_using_id(999999) is False
        assert await Auth.loginUsingId(alice.id) is not False

        await Auth.logout()
        assert "logout" in events_seen

        # Ensure Attempt event never records plaintext passwords
        await Auth.attempt({"email": "alice@example.com", "password": "wrong"})
        assert attempting_payloads
        assert "password" not in attempting_payloads[-1]


@pytest.mark.asyncio
async def test_concurrent_auth_context_isolation(attempt_app: Application) -> None:
    alice = await AttemptUser.where("email", "alice@example.com").first_or_fail()
    bob = await AttemptUser.where("email", "bob@example.com").first_or_fail()

    async def worker(email: str, expected_id: int) -> int:
        with bind_container(attempt_app.container):
            Context.clear()
            ok = await Auth.once({"email": email, "password": "secret123"})
            assert ok is True
            await asyncio.sleep(0.01)
            uid = Auth.id()
            assert uid == expected_id
            return int(uid)

    ids = await asyncio.gather(
        worker("alice@example.com", alice.id),
        worker("bob@example.com", bob.id),
    )
    assert list(ids) == [alice.id, bob.id]


@pytest.mark.asyncio
async def test_dummy_bcrypt_hash_executed_on_missing_user(
    attempt_app: Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    orig_check = Hash.check_async

    async def spy_check_async(
        _cls: type[Hash], plain: str, hashed: str
    ) -> bool:
        calls.append((plain, hashed))
        return await orig_check(plain, hashed)

    monkeypatch.setattr(Hash, "check_async", classmethod(spy_check_async))

    alice = await AttemptUser.where("email", "alice@example.com").first_or_fail()
    with bind_container(attempt_app.container):
        # Existing user + wrong password compares against user's stored hash
        assert (
            await Auth.attempt({"email": "alice@example.com", "password": "wrong-pw"})
            is False
        )
        assert len(calls) == 1
        assert calls[0] == ("wrong-pw", alice.password)

        # Missing user must still execute Hash.check_async against dummy bcrypt hash
        calls.clear()
        assert (
            await Auth.attempt({"email": "missing@example.com", "password": "wrong-pw"})
            is False
        )
        assert len(calls) == 1
        assert calls[0] == ("wrong-pw", _DUMMY_BCRYPT_HASH)

        # Missing password key also executes dummy check cleanly
        calls.clear()
        assert await Auth.attempt({"email": "alice@example.com"}) is False
        assert len(calls) == 1
        assert calls[0] == ("", _DUMMY_BCRYPT_HASH)


@pytest.mark.asyncio
async def test_boolean_coercion_and_non_bool_column_preservation(
    attempt_app: Application,
) -> None:
    with bind_container(attempt_app.container):
        # active: 0 rejects active user Alice and matches inactive user Bob
        assert (
            await Auth.attempt(
                {"email": "alice@example.com", "password": "secret123", "active": 0}
            )
            is False
        )
        assert (
            await Auth.attempt(
                {"email": "bob@example.com", "password": "secret123", "active": 0}
            )
            is True
        )
        await Auth.logout()

        # Explicit True/False also behave predictably
        assert (
            await Auth.attempt(
                {"email": "alice@example.com", "password": "secret123", "active": True}
            )
            is True
        )
        await Auth.logout()
        assert (
            await Auth.attempt(
                {"email": "alice@example.com", "password": "secret123", "active": False}
            )
            is False
        )
        await Auth.clear_throttle("alice@example.com")

        # Integer column keeps integer equality (1 stays 1, 2 fails)
        assert (
            await Auth.attempt(
                {"email": "alice@example.com", "password": "secret123", "login_level": 1}
            )
            is True
        )
        await Auth.logout()
        assert (
            await Auth.attempt(
                {"email": "alice@example.com", "password": "secret123", "login_level": 2}
            )
            is False
        )


@pytest.mark.asyncio
async def test_attempt_when_async_success_falsy_and_exception_propagation(
    attempt_app: Application,
) -> None:
    with bind_container(attempt_app.container):
        # Async predicate returning True succeeds and authenticates Alice
        async def allow_alice_async(user: AttemptUser) -> bool:
            await asyncio.sleep(0)
            return user.name == "Alice" and user.is_not_banned()

        assert (
            await Auth.attempt_when(
                {"email": "alice@example.com", "password": "secret123"},
                allow_alice_async,
            )
            is True
        )
        assert Auth.check() is True
        await Auth.logout()

        # Non-boolean falsy value rejects cleanly; truthy non-bool value accepts
        assert (
            await Auth.attempt_when(
                {"email": "alice@example.com", "password": "secret123"},
                lambda _u: "",
            )
            is False
        )
        assert (
            await Auth.attempt_when(
                {"email": "alice@example.com", "password": "secret123"},
                lambda _u: "ok",
            )
            is True
        )
        await Auth.logout()

        # Exceptions inside predicate propagate without leaving user authenticated
        def exploding_check(_user: AttemptUser) -> bool:
            raise RuntimeError("predicate exploded")

        with pytest.raises(RuntimeError, match="predicate exploded"):
            await Auth.attempt_when(
                {"email": "alice@example.com", "password": "secret123"},
                exploding_check,
            )
        assert Auth.guest() is True

        # Callback passed as dict value works just like dict key
        assert (
            await Auth.attempt(
                {
                    "email": "alice@example.com",
                    "password": "secret123",
                    "subscription_check": Callback(lambda q: q.has("activeSubscription")),
                }
            )
            is True
        )
        await Auth.logout()

        # Async closure in credentials mapping is also awaited cleanly
        async def async_sub_filter(q: Any) -> Any:
            await asyncio.sleep(0)
            return q.has("activeSubscription")

        assert (
            await Auth.attempt(
                {
                    "email": "alice@example.com",
                    "password": "secret123",
                    Callback(async_sub_filter): True,
                }
            )
            is True
        )


@pytest.mark.asyncio
async def test_auth_events_strip_all_sensitive_credentials(
    attempt_app: Application,
    ensure_db_pool: DatabasePool,
) -> None:
    class EventSanitizeUser(Model):
        __table__ = "test_attempt_event_users"
        __primary_key__ = "id"
        __fillable__: ClassVar[list[str]] = [
            "email",
            "password",
            "remember_token",
            "access_token",
            "refresh_token",
            "api_key",
            "client_secret",
        ]
        __hidden__: ClassVar[list[str]] = [
            "password",
            "remember_token",
            "access_token",
            "refresh_token",
            "api_key",
            "client_secret",
        ]
        __guarded__: ClassVar[list[str]] = []

        id: int
        email: str
        password: str
        remember_token: str | None = None
        access_token: str | None = None
        refresh_token: str | None = None
        api_key: str | None = None
        client_secret: str | None = None

    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS test_attempt_event_users CASCADE;
            CREATE TABLE test_attempt_event_users (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                remember_token TEXT NULL,
                access_token TEXT NULL,
                refresh_token TEXT NULL,
                api_key TEXT NULL,
                client_secret TEXT NULL
            );
            """
        )

    await EventSanitizeUser.create(
        {
            "email": "alice@example.com",
            "password": Hash.make("real-secret", rounds=4),
            "remember_token": "sec-rem-val",
            "access_token": "sec-acc-val",
            "refresh_token": "sec-ref-val",
            "api_key": "sec-api-val",
            "client_secret": "sec-cli-val",
        }
    )

    attempting_events: list[dict[str, Any]] = []
    failed_events: list[dict[str, Any]] = []
    aliased_attempting: list[dict[str, Any]] = []

    dispatcher = EventDispatcher.get_instance()
    dispatcher.listen(
        Attempting, lambda ev: attempting_events.append(dict(ev.credentials))
    )
    dispatcher.listen(Failed, lambda ev: failed_events.append(dict(ev.credentials)))
    dispatcher.listen(
        "auth.attempting",
        lambda ev: aliased_attempting.append(dict(ev.credentials)),
    )

    prev_model = attempt_app.config.get("auth.model")
    attempt_app.config.set("auth.model", EventSanitizeUser)
    try:
        with bind_container(attempt_app.container):
            assert (
                await Auth.attempt(
                    {
                        "email": "alice@example.com",
                        "password": "wrong-password-val",
                        "remember_token": "sec-rem-val",
                        "access_token": "sec-acc-val",
                        "refresh_token": "sec-ref-val",
                        "api_key": "sec-api-val",
                        "client_secret": "sec-cli-val",
                    }
                )
                is False
            )

        assert len(attempting_events) == 1
        assert len(failed_events) == 1
        assert len(aliased_attempting) == 1

        for payload in (*attempting_events, *failed_events, *aliased_attempting):
            assert payload["email"] == "alice@example.com"
            for hidden_key in (
                "password",
                "remember_token",
                "access_token",
                "refresh_token",
                "api_key",
                "client_secret",
            ):
                assert hidden_key not in payload
            for secret_val in (
                "wrong-password-val",
                "sec-rem-val",
                "sec-acc-val",
                "sec-ref-val",
                "sec-api-val",
                "sec-cli-val",
            ):
                assert secret_val not in str(payload)
    finally:
        attempt_app.config.set("auth.model", prev_model)
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS test_attempt_event_users CASCADE;")


@pytest.mark.asyncio
async def test_remember_me_flags_false_mode_deleted_user_rotation_and_logout_all(
    attempt_app: Application,
) -> None:
    @attempt_app.post("/login")
    async def login_route(request: Request) -> Any:
        payload = await request.json()
        ok = await Auth.attempt(
            {"email": payload["email"], "password": payload["password"]},
            remember=bool(payload.get("remember", False)),
        )
        return json_response({"ok": ok}, status_code=200 if ok else 401)

    @attempt_app.post("/login-by-id")
    async def login_by_id_route(request: Request) -> Any:
        payload = await request.json()
        user = await Auth.login_using_id(
            payload["id"], remember=bool(payload.get("remember", False))
        )
        return json_response({"ok": bool(user)}, status_code=200 if user else 401)

    @attempt_app.get("/me")
    async def me_route() -> Any:
        if Auth.guest():
            return json_response({"authenticated": False}, status_code=401)
        return json_response({"authenticated": True, "id": Auth.id(), "via_remember": Auth.via_remember()})

    @attempt_app.post("/logout-all")
    async def logout_all_route() -> Any:
        await Auth.logout_all()
        return json_response({"logged_out_all": True})

    client = TestClient(attempt_app)

    # 1. remember=False emits NO remember cookie
    no_rem_res = await client.post(
        "/login",
        json={"email": "alice@example.com", "password": "secret123", "remember": False},
    )
    assert no_rem_res.status_code == 200
    assert no_rem_res.headers.getlist("set-cookie") == []

    # 2. Cookie attributes & Secure in production / HTTPS
    attempt_app.config.set("app.env", "production")
    attempt_app.config.set("app.debug", False)
    _, prod_hdr = format_remember_set_cookie(1, "tok_abc", config=attempt_app.config)
    assert "HttpOnly" in prod_hdr
    assert "SameSite=Lax" in prod_hdr
    assert "Path=/" in prod_hdr
    assert "Max-Age=2592000" in prod_hdr
    assert "Secure" in prod_hdr

    attempt_app.config.set("app.env", "local")
    attempt_app.config.set("app.debug", True)
    _, dev_hdr = format_remember_set_cookie(1, "tok_abc", config=attempt_app.config)
    assert "Secure" not in dev_hdr
    attempt_app.config.set("app.url", "https://api.example.com")
    _, https_hdr = format_remember_set_cookie(1, "tok_abc", config=attempt_app.config)
    assert "Secure" in https_hdr
    attempt_app.config.set("app.url", "http://localhost")
    attempt_app.config.set("auth.remember.same_site", "Strict")
    attempt_app.config.set("auth.remember.path", "/api")
    _, custom_hdr = format_remember_set_cookie(1, "tok_abc", config=attempt_app.config)
    assert "SameSite=Strict" in custom_hdr
    assert "Path=/api" in custom_hdr
    attempt_app.config.set("auth.remember.same_site", "Lax")
    attempt_app.config.set("auth.remember.path", "/")

    # 3. login_using_id(remember=True) issues cookie & sets DB token; rotation invalidates older cookie
    alice = await AttemptUser.where("email", "alice@example.com").first_or_fail()
    rem_1 = await client.post("/login-by-id", json={"id": alice.id, "remember": True})
    assert rem_1.status_code == 200
    cookie_1 = rem_1.headers.getlist("set-cookie")[0].split(";", 1)[0]

    rem_2 = await client.post(
        "/login",
        json={"email": "alice@example.com", "password": "secret123", "remember": True},
    )
    assert rem_2.status_code == 200
    cookie_2 = rem_2.headers.getlist("set-cookie")[0].split(";", 1)[0]
    assert cookie_1 != cookie_2

    assert (await client.get("/me", headers={"Cookie": cookie_1})).status_code == 401
    assert (await client.get("/me", headers={"Cookie": cookie_2})).status_code == 200

    # 4. Malformed / empty remember cookies rejected safely
    for bad_cookie in (
        "swiftpy_remember=",
        "swiftpy_remember=only-one-part",
        "swiftpy_remember=!!!.@@@.deadbeef",
    ):
        assert (await client.get("/me", headers={"Cookie": bad_cookie})).status_code == 401

    # 5. logout_all() invalidates remember_token & expires cookie
    out_all = await client.post("/logout-all", headers={"Cookie": cookie_2})
    assert out_all.status_code == 200
    assert any("Max-Age=0" in c for c in out_all.headers.getlist("set-cookie"))
    reloaded = await AttemptUser.find(alice.id)
    assert reloaded is not None and reloaded.remember_token is None
    assert (await client.get("/me", headers={"Cookie": cookie_2})).status_code == 401

    # 6. Deleted user with otherwise-valid signed cookie fails cleanly
    temp = await AttemptUser.create(
        {
            "name": "TempUser",
            "email": "temp@example.com",
            "password": alice.password,
            "active": True,
            "banned": False,
        }
    )
    temp_login = await client.post(
        "/login",
        json={"email": "temp@example.com", "password": "secret123", "remember": True},
    )
    temp_cookie = temp_login.headers.getlist("set-cookie")[0].split(";", 1)[0]
    await temp.delete()
    assert (await client.get("/me", headers={"Cookie": temp_cookie})).status_code == 401
