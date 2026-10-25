from __future__ import annotations

from swiftpy.auth.api_key_guard import (
    ApiKeyGuard,
    hash_api_key,
    require_api_key,
    verify_api_key_hash,
)
from swiftpy.auth.auth_manager import AuthManager, CurrentUser, current_user
from swiftpy.auth.exceptions import (
    AuthenticationError,
    InvalidApiKeyError,
    TokenRevokedError,
)
from swiftpy.auth.jwt_guard import JWTGuard, require_jwt
from swiftpy.auth.jwt_issuer import (
    InvalidAlgorithmError,
    InvalidSignatureError,
    InvalidTokenError,
    JWTError,
    JWTIssuer,
    TokenExpiredError,
)
from swiftpy.auth.token_blacklist import TokenBlacklist

__all__ = [
    "ApiKeyGuard",
    "AuthManager",
    "AuthenticationError",
    "CurrentUser",
    "InvalidAlgorithmError",
    "InvalidApiKeyError",
    "InvalidSignatureError",
    "InvalidTokenError",
    "JWTError",
    "JWTGuard",
    "JWTIssuer",
    "TokenBlacklist",
    "TokenExpiredError",
    "TokenRevokedError",
    "current_user",
    "hash_api_key",
    "require_api_key",
    "require_jwt",
    "verify_api_key_hash",
]
