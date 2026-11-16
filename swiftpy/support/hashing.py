"""
Password hashing utilities and support facade for SwiftPY.

Provides Laravel-inspired ``Hash`` class methods alongside convenience helpers
using bcrypt with safe UTF-8 handling, SHA-256 pre-hashing above bcrypt's
72-byte ceiling, and asyncio thread offloading for non-blocking ASGI handlers.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import ClassVar

import bcrypt

from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import get_container

DEFAULT_BCRYPT_ROUNDS: int = 12
MIN_BCRYPT_ROUNDS: int = 4
MAX_BCRYPT_ROUNDS: int = 31

_BCRYPT_MAX_RAW_BYTES: int = 72
_BCRYPT_HASH_RE = re.compile(r"^\$2[aby]\$(\d{2})\$[./A-Za-z0-9]{53}$")


def prepare_bcrypt_secret(raw_secret: str) -> bytes:
    """
    Encode a secret or password for bcrypt.

    Inputs up to 72 UTF-8 bytes keep raw UTF-8 encoding for full interoperability
    with standard ``$2b$`` hashes; longer inputs are SHA-256 pre-hashed so
    ``bcrypt>=5.0`` neither truncates nor raises a 72-byte length error.
    """
    if not isinstance(raw_secret, str):
        raise TypeError("Secret value must be a str.")
    raw_bytes = raw_secret.encode("utf-8")
    if len(raw_bytes) > _BCRYPT_MAX_RAW_BYTES:
        return hashlib.sha256(raw_bytes).hexdigest().encode("ascii")
    return raw_bytes


def _validate_rounds(rounds: object) -> int:
    if isinstance(rounds, bool) or not isinstance(rounds, int):
        raise TypeError(
            f"Bcrypt rounds must be an integer between {MIN_BCRYPT_ROUNDS} "
            f"and {MAX_BCRYPT_ROUNDS}."
        )
    if not (MIN_BCRYPT_ROUNDS <= rounds <= MAX_BCRYPT_ROUNDS):
        raise ValueError(
            f"Bcrypt rounds must be between {MIN_BCRYPT_ROUNDS} "
            f"and {MAX_BCRYPT_ROUNDS} (got {rounds})."
        )
    return rounds


def _resolve_active_config(config: Config | None = None) -> Config | None:
    if config is not None:
        return config
    try:
        container = get_container()
        if container.has(Config):
            return container.resolve(Config)
    except RuntimeError:
        pass
    try:
        return Config.get_instance()
    except (ConfigurationError, RuntimeError, ValueError):
        return None


def _resolve_rounds(
    rounds: int | None = None,
    *,
    config: Config | None = None,
) -> int:
    if rounds is not None:
        return _validate_rounds(rounds)

    cfg = _resolve_active_config(config)
    if cfg is not None:
        for key in ("auth.hash.rounds", "auth.bcrypt_rounds", "auth.rounds"):
            raw = cfg.get(key)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            if isinstance(raw, bool):
                raise TypeError(f"Invalid boolean configuration for '{key}'.")
            if isinstance(raw, int):
                return _validate_rounds(raw)
            if isinstance(raw, str):
                try:
                    parsed = int(raw.strip())
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid integer configuration for '{key}': {raw!r}."
                    ) from exc
                return _validate_rounds(parsed)
            raise TypeError(
                f"Invalid configuration type for '{key}': expected int, got {type(raw).__name__}."
            )

    return DEFAULT_BCRYPT_ROUNDS


class Hash:
    """
    Class-level bcrypt password hashing facade with sync and async APIs.

    Examples:
        hashed = Hash.make("secret-password")
        ok = Hash.check("secret-password", hashed)

        hashed = await Hash.make_async("secret-password")
        ok = await Hash.check_async("secret-password", hashed)
    """

    DEFAULT_ROUNDS: ClassVar[int] = DEFAULT_BCRYPT_ROUNDS
    MIN_ROUNDS: ClassVar[int] = MIN_BCRYPT_ROUNDS
    MAX_ROUNDS: ClassVar[int] = MAX_BCRYPT_ROUNDS

    @classmethod
    def _hash_prepared(cls, value: str, resolved_rounds: int) -> str:
        secret = prepare_bcrypt_secret(value)
        salt = bcrypt.gensalt(rounds=resolved_rounds)
        return bcrypt.hashpw(secret, salt).decode("utf-8")

    @classmethod
    def make(cls, value: str, rounds: int | None = None) -> str:
        """Hash a plaintext password synchronously and return a UTF-8 ``$2b$`` string."""
        if not isinstance(value, str):
            raise TypeError("Password value must be a str.")
        resolved_rounds = _resolve_rounds(rounds)
        return cls._hash_prepared(value, resolved_rounds)

    @classmethod
    async def make_async(cls, value: str, rounds: int | None = None) -> str:
        """Hash a plaintext password in a worker thread without blocking the event loop."""
        if not isinstance(value, str):
            raise TypeError("Password value must be a str.")
        resolved_rounds = _resolve_rounds(rounds)
        return await asyncio.to_thread(cls._hash_prepared, value, resolved_rounds)

    @classmethod
    def check(cls, value: str, hashed: str) -> bool:
        """Verify a plaintext password against a stored bcrypt hash safely."""
        if not isinstance(value, str) or not isinstance(hashed, str):
            return False
        if not hashed.startswith("$2"):
            return False
        try:
            secret = prepare_bcrypt_secret(value)
            return bcrypt.checkpw(secret, hashed.encode("utf-8"))
        except (ValueError, TypeError):
            return False

    @classmethod
    async def check_async(cls, value: str, hashed: str) -> bool:
        """Verify a plaintext password asynchronously without blocking the event loop."""
        return await asyncio.to_thread(cls.check, value, hashed)

    @classmethod
    def needs_rehash(cls, hashed: str, rounds: int | None = None) -> bool:
        """Return True if ``hashed`` is invalid, non-bcrypt, or uses a different cost factor."""
        target_rounds = _resolve_rounds(rounds)
        if not isinstance(hashed, str):
            return True
        match = _BCRYPT_HASH_RE.fullmatch(hashed)
        if match is None:
            return True
        return int(match.group(1)) != target_rounds


def hash_password(password: str, rounds: int | None = None) -> str:
    """Hash a plaintext password synchronously via :meth:`Hash.make`."""
    return Hash.make(password, rounds=rounds)


def verify_password(password: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored hash via :meth:`Hash.check`."""
    return Hash.check(password, hashed)


async def hash_password_async(password: str, rounds: int | None = None) -> str:
    """Hash a plaintext password asynchronously via :meth:`Hash.make_async`."""
    return await Hash.make_async(password, rounds=rounds)


async def verify_password_async(password: str, hashed: str) -> bool:
    """Verify a plaintext password asynchronously via :meth:`Hash.check_async`."""
    return await Hash.check_async(password, hashed)


__all__ = [
    "DEFAULT_BCRYPT_ROUNDS",
    "MAX_BCRYPT_ROUNDS",
    "MIN_BCRYPT_ROUNDS",
    "Hash",
    "hash_password",
    "hash_password_async",
    "prepare_bcrypt_secret",
    "verify_password",
    "verify_password_async",
]
