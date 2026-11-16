"""
SwiftPY Support Utilities.

Cross-cutting helper functions and configuration resolvers.
"""

from __future__ import annotations

from swiftpy.support.hashing import (
    DEFAULT_BCRYPT_ROUNDS,
    MAX_BCRYPT_ROUNDS,
    MIN_BCRYPT_ROUNDS,
    Hash,
    hash_password,
    hash_password_async,
    prepare_bcrypt_secret,
    verify_password,
    verify_password_async,
)
from swiftpy.support.redis_config import build_redis_url, resolve_redis_params
from swiftpy.support.strings import pluralize, snake_case

__all__ = [
    "DEFAULT_BCRYPT_ROUNDS",
    "MAX_BCRYPT_ROUNDS",
    "MIN_BCRYPT_ROUNDS",
    "Hash",
    "build_redis_url",
    "hash_password",
    "hash_password_async",
    "pluralize",
    "prepare_bcrypt_secret",
    "resolve_redis_params",
    "snake_case",
    "verify_password",
    "verify_password_async",
]
