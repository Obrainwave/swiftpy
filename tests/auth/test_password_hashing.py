from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from typing import Any

import bcrypt
import pytest

import swiftpy
import swiftpy.auth as auth_pkg
import swiftpy.helpers as helpers_pkg
import swiftpy.helpers.helper as helper_mod
import swiftpy.support as support_pkg
from app.providers.auth_service_provider import AuthServiceProvider
from swiftpy import (
    Application,
    Hash,
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)
from swiftpy.core.config import Config
from swiftpy.core.container import Container, bind_container
from swiftpy.support.hashing import DEFAULT_BCRYPT_ROUNDS, prepare_bcrypt_secret


def test_hash_make_and_check_basics() -> None:
    hashed = Hash.make("Secret123!", rounds=4)
    assert isinstance(hashed, str)
    assert hashed.startswith("$2b$04$")
    assert len(hashed) == 60
    assert Hash.make("Secret123!", rounds=4) != hashed  # unique random salt

    assert Hash.check("Secret123!", hashed) is True
    assert Hash.check("WrongPassword", hashed) is False


def test_helper_functions_and_export_surfaces() -> None:
    assert auth_pkg.Hash is Hash
    assert support_pkg.Hash is Hash
    assert auth_pkg.hash_password is hash_password
    assert support_pkg.hash_password is hash_password
    assert helpers_pkg.hash_password is hash_password
    assert helper_mod.hash_password is hash_password
    assert auth_pkg.verify_password is verify_password
    assert helpers_pkg.verify_password is verify_password
    assert auth_pkg.hash_password_async is hash_password_async
    assert helpers_pkg.hash_password_async is hash_password_async
    assert auth_pkg.verify_password_async is verify_password_async
    assert helpers_pkg.verify_password_async is verify_password_async

    for symbol in (
        "Hash",
        "hash_password",
        "hash_password_async",
        "verify_password",
        "verify_password_async",
    ):
        assert symbol in swiftpy.__all__

    hashed = hash_password("SwiftPY#Pass1", rounds=4)
    assert verify_password("SwiftPY#Pass1", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_long_and_multibyte_passwords_do_not_truncate_at_72_bytes() -> None:
    prefix = "a" * 72
    pw_one = prefix + "-alpha"
    pw_two = prefix + "-beta"

    hashed = Hash.make(pw_one, rounds=4)
    assert hashed.startswith("$2b$04$")
    assert Hash.check(pw_one, hashed) is True
    assert Hash.check(pw_two, hashed) is False

    # 30 4-byte emojis = 120 UTF-8 bytes (<72 code points, >72 bytes)
    emoji_pw = "\U0001f680" * 30
    emoji_hash = Hash.make(emoji_pw + "A", rounds=4)
    assert Hash.check(emoji_pw + "A", emoji_hash) is True
    assert Hash.check(emoji_pw + "B", emoji_hash) is False


def test_interop_with_raw_bcrypt_hashes_up_to_72_bytes() -> None:
    raw_hash = bcrypt.hashpw(b"LegacyUser123", bcrypt.gensalt(rounds=4)).decode("utf-8")
    assert Hash.check("LegacyUser123", raw_hash) is True
    assert Hash.check("wrong", raw_hash) is False

    # Also verify $2y$ prefix compatibility
    variant_2y = "$2y$" + raw_hash[4:]
    assert Hash.check("LegacyUser123", variant_2y) is True
    assert Hash.needs_rehash(variant_2y, rounds=4) is False


def test_needs_rehash_checks_cost_and_format() -> None:
    hashed = Hash.make("rehash-test", rounds=4)
    assert Hash.needs_rehash(hashed, rounds=4) is False
    assert Hash.needs_rehash(hashed, rounds=5) is True
    assert Hash.needs_rehash(hashed, rounds=DEFAULT_BCRYPT_ROUNDS) is True
    assert Hash.needs_rehash("not-a-valid-hash", rounds=4) is True
    assert Hash.needs_rehash("", rounds=4) is True


def test_config_driven_default_rounds() -> None:
    cfg = Config({"auth": {"hash": {"rounds": 5}}})
    container = Container()
    container.instance(Config, cfg)

    with bind_container(container):
        hashed = Hash.make("configured-rounds")
        assert hashed.startswith("$2b$05$")
        assert Hash.needs_rehash(hashed) is False
        assert Hash.needs_rehash(hashed, rounds=6) is True


@pytest.mark.parametrize(
    ("bad_rounds", "exc_type"),
    [
        (True, TypeError),
        (False, TypeError),
        ("10", TypeError),
        (10.5, TypeError),
        (3, ValueError),
        (32, ValueError),
        (-1, ValueError),
    ],
)
def test_invalid_rounds_rejected(bad_rounds: Any, exc_type: type[Exception]) -> None:
    with pytest.raises(exc_type):
        Hash.make("secret", rounds=bad_rounds)


def test_invalid_config_rounds_rejected() -> None:
    cases: list[tuple[Any, type[Exception]]] = [
        (True, TypeError),
        ([12], TypeError),
        ("not-an-int", ValueError),
        (2, ValueError),
        (35, ValueError),
    ]
    for bad_cfg_val, exc_type in cases:
        cfg = Config({"auth": {"hash": {"rounds": bad_cfg_val}}})
        container = Container()
        container.instance(Config, cfg)
        with bind_container(container), pytest.raises(exc_type):
            Hash.make("secret")


def test_type_and_malformed_hash_safety() -> None:
    non_str: Any = b"not-a-str"
    none_val: Any = None

    with pytest.raises(TypeError):
        Hash.make(non_str)

    with pytest.raises(TypeError):
        prepare_bcrypt_secret(non_str)

    assert Hash.check("secret", "") is False
    assert Hash.check("secret", "plain-text-password") is False
    assert Hash.check("secret", "$2b$invalid") is False
    assert Hash.check(none_val, "$2b$04$invalid") is False
    assert Hash.check("secret", none_val) is False
    assert Hash.check("bad\ud800surrogate", "$2b$04$" + "." * 53) is False


@pytest.mark.asyncio
async def test_async_hashing_and_verification_concurrency() -> None:
    hashed = await hash_password_async("AsyncSecret!99", rounds=4)
    assert hashed.startswith("$2b$04$")

    results = await asyncio.gather(
        verify_password_async("AsyncSecret!99", hashed),
        Hash.check_async("AsyncSecret!99", hashed),
        verify_password_async("wrong-secret", hashed),
        Hash.make_async("AnotherSecret", rounds=4),
    )
    assert results[0] is True
    assert results[1] is True
    assert results[2] is False
    assert isinstance(results[3], str) and results[3].startswith("$2b$04$")

    with pytest.raises(ValueError):
        await Hash.make_async("secret", rounds=2)


def test_auth_service_provider_registers_hash_singleton() -> None:
    app = Application(
        auto_discover_providers=False,
        debug=True,
        config={
            "app": {"env": "testing", "debug": True},
            "auth": {"hash": {"rounds": 4}},
        },
        providers=[AuthServiceProvider],
    )
    assert app.container.has(Hash)
    resolved_a = app.container.resolve(Hash)
    resolved_b = app.container.resolve(Hash)
    assert resolved_a is resolved_b

    with bind_container(app.container):
        hashed = resolved_a.make("provider-pw")
        assert hashed.startswith("$2b$04$")
        assert resolved_b.check("provider-pw", hashed) is True


def test_top_level_import_keeps_hashing_lazy() -> None:
    code = (
        "import sys, swiftpy\n"
        "assert 'swiftpy.support.hashing' not in sys.modules\n"
        "assert 'bcrypt' not in sys.modules\n"
        "h = swiftpy.hash_password('lazy-check', rounds=4)\n"
        "assert swiftpy.verify_password('lazy-check', h) is True\n"
        "assert 'swiftpy.support.hashing' in sys.modules\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    importlib.invalidate_caches()
