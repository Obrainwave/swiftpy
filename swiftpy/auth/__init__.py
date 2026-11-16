from __future__ import annotations

from swiftpy.auth.api_key_guard import (
    ApiKeyGuard,
    generate_api_key,
    get_server_pepper,
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
from swiftpy.support.hashing import (
    Hash,
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)

__all__ = [
    "ApiKeyGuard",
    "AuthManager",
    "AuthenticationError",
    "CurrentUser",
    "Hash",
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
    "generate_api_key",
    "get_server_pepper",
    "hash_api_key",
    "hash_password",
    "hash_password_async",
    "require_api_key",
    "require_jwt",
    "verify_api_key_hash",
    "verify_password",
    "verify_password_async",
]
