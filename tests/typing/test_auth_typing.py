from __future__ import annotations

from typing import Any, assert_type

from swiftpy import Auth, AuthProxy, AuthUser
from swiftpy.auth import auth
from swiftpy.auth_manager import Auth as LegacyAuth
from swiftpy.auth_manager import auth as legacy_auth


def _verify_auth_typing_contracts() -> None:
    """Verify static types across auth(), Auth, AuthProxy, AuthUser, and legacy facades."""
    proxy = assert_type(auth(), AuthProxy)
    legacy_proxy = assert_type(legacy_auth(), AuthProxy)

    user = assert_type(proxy.user(), AuthUser)
    legacy_user = assert_type(legacy_proxy.user(), AuthUser)
    facade_user = assert_type(Auth.user(), AuthUser)
    legacy_facade_user = assert_type(LegacyAuth.user(), AuthUser)

    # Declared AuthUser field properties & dict export types
    _uid: Any = auth().user().id
    _uemail: Any = auth().user().email
    _uname: Any = auth().user().name
    _facade_id: Any = Auth.user().id
    _facade_email: Any = Auth.user().email
    _legacy_id: Any = legacy_auth().user().id
    _udict = assert_type(user.to_dict(), dict[str, Any])

    # Explicit nullable access and explicit strict access
    _nullable_user = assert_type(auth().user(nullable=True), AuthUser | None)
    _nullable_facade = assert_type(Auth.user(nullable=True), AuthUser | None)
    _strict_user = assert_type(auth().user_or_fail(), AuthUser)
    _strict_facade = assert_type(Auth.user_or_fail(), AuthUser)

    # Guarded proxies preserve identical static return contracts
    _guarded_user = assert_type(auth("api_key").user(), AuthUser)
    _guarded_nullable = assert_type(auth("api_key").user(nullable=True), AuthUser | None)
    _guarded_strict = assert_type(auth("api_key").user_or_fail(), AuthUser)

    _ = (
        user,
        legacy_user,
        facade_user,
        legacy_facade_user,
        _uid,
        _uemail,
        _uname,
        _facade_id,
        _facade_email,
        _legacy_id,
        _udict,
        _nullable_user,
        _nullable_facade,
        _strict_user,
        _strict_facade,
        _guarded_user,
        _guarded_nullable,
        _guarded_strict,
    )


def _return_auth_user_helper() -> AuthUser:
    return auth().user()


def _return_facade_auth_user_helper() -> AuthUser:
    return Auth.user()


def test_auth_typing_contracts_runtime_smoke() -> None:
    """Confirm the auth typing contract test module imports cleanly under pytest."""
    assert callable(_verify_auth_typing_contracts)
    assert callable(_return_auth_user_helper)
    assert callable(_return_facade_auth_user_helper)
