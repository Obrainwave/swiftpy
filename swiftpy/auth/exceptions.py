from __future__ import annotations

from swiftpy.core.config import Config
from swiftpy.http.exceptions import HttpException


class JWTError(Exception):
    """Base exception for all JWT-related errors."""


class InvalidTokenError(JWTError):
    """Raised when a token is structurally malformed or invalid."""


class TokenExpiredError(JWTError):
    """Raised when a token's exp claim is in the past."""


class InvalidSignatureError(InvalidTokenError):
    """Raised when a token's signature cannot be verified."""


class InvalidAlgorithmError(InvalidTokenError):
    """Raised when an algorithm is disallowed, missing, or mismatched."""


class BaseTokenBlacklist:
    """Base interface for JWT token revocation stores."""

    async def is_revoked(
        self,
        jti: str,
        subject: str | int | None = None,
        issued_at: int | None = None,
    ) -> bool:
        """Return True when a token JTI or subject cutoff has been revoked."""
        raise NotImplementedError

    async def revoke(self, jti: str, ttl: int | None = None) -> None:
        """Revoke an individual token JTI with an optional TTL in seconds."""
        raise NotImplementedError


def is_token_blacklist_enabled(config: Config | None = None) -> bool:
    """Return whether JWT token blacklist revocation checking is enabled."""
    cfg = config
    if cfg is None:
        try:
            cfg = Config.get_instance()
        except (RuntimeError, ValueError):
            return True

    for key in (
        "auth.guards.jwt.blacklist_enabled",
        "auth.jwt.blacklist_enabled",
        "auth.blacklist_enabled",
    ):
        val = cfg.get(key)
        if val is not None:
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.strip().lower() not in ("false", "0", "no", "off")
            return bool(val)
    return True


class AuthenticationError(HttpException):
    """Base exception for authentication failures resulting in HTTP 401."""

    def __init__(
        self,
        message: str = "Unauthorized",
        status_code: int = 401,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=status_code,
            headers=headers or {"WWW-Authenticate": "Bearer"},
        )


class TokenRevokedError(AuthenticationError, JWTError):
    """Raised when a valid token has been revoked or already rotated in the blacklist."""

    def __init__(self, message: str = "Invalid or expired token") -> None:
        super().__init__(message=message, status_code=401)


class InvalidApiKeyError(AuthenticationError):
    """Raised when an API key is invalid, inactive, or expired."""

    def __init__(self, message: str = "Invalid API key") -> None:
        super().__init__(
            message=message,
            status_code=401,
            headers={"WWW-Authenticate": "ApiKey"},
        )


class AuthenticationServiceUnavailableError(HttpException):
    """Raised when authentication infrastructure (e.g. Redis, Database) is unavailable, resulting in HTTP 503."""

    def __init__(
        self,
        message: str = "Authentication service unavailable",
        status_code: int = 503,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=status_code,
            headers=headers or {"Retry-After": "30"},
        )


__all__ = [
    "AuthenticationError",
    "AuthenticationServiceUnavailableError",
    "BaseTokenBlacklist",
    "InvalidAlgorithmError",
    "InvalidApiKeyError",
    "InvalidSignatureError",
    "InvalidTokenError",
    "JWTError",
    "TokenExpiredError",
    "TokenRevokedError",
    "is_token_blacklist_enabled",
]
