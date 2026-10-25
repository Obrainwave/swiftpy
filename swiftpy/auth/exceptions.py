from swiftpy.auth.jwt_issuer import (
    InvalidAlgorithmError,
    InvalidSignatureError,
    InvalidTokenError,
    JWTError,
    TokenExpiredError,
)
from swiftpy.http.exceptions import HttpException


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


class TokenRevokedError(AuthenticationError):
    """Raised when a valid token has been revoked in the blacklist."""

    def __init__(self, message: str = "Token has been revoked") -> None:
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
        message: str = "Authentication service temporarily unavailable",
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
    "InvalidAlgorithmError",
    "InvalidApiKeyError",
    "InvalidSignatureError",
    "InvalidTokenError",
    "JWTError",
    "TokenExpiredError",
    "TokenRevokedError",
]
