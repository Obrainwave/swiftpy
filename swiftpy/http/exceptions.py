from __future__ import annotations


class HttpException(Exception):
    """
    Base HTTP exception for request pipeline errors.
    Automatically caught and converted into JSON responses by Application.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.headers = headers
        super().__init__(message)


class PayloadTooLargeError(HttpException):
    """Exception mapped automatically to HTTP 413 Payload Too Large."""

    def __init__(
        self,
        message: str = "Payload Too Large",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message=message, status_code=413, headers=headers)


class ClientDisconnect(Exception):
    """Raised when the ASGI client disconnects before the request body is fully read."""


__all__ = [
    "ClientDisconnect",
    "HttpException",
    "PayloadTooLargeError",
]
