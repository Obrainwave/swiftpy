from __future__ import annotations

from http import HTTPStatus
from typing import NoReturn


class HttpException(Exception):
    """
    Base HTTP exception for request pipeline errors.
    Automatically caught and converted into JSON responses by Application.
    Subclasses may declare class-level `status_code` and `message` defaults.
    """

    status_code: int = 500
    message: str = "Internal Server Error"

    def __init__(
        self,
        message: str | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        resolved_message = type(self).message if message is None else message
        resolved_status = (
            type(self).status_code if status_code is None else status_code
        )
        self.message: str = resolved_message
        self.status_code: int = resolved_status
        self.headers: dict[str, str] | None = headers
        super().__init__(resolved_message)


def _default_reason_phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP Error"


def abort(
    status_code: int,
    message: str | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> NoReturn:
    """Immediately abort handling the current HTTP request with an HttpException."""
    if isinstance(status_code, bool) or not isinstance(status_code, int) or status_code < 100 or status_code > 599:
        raise ValueError(
            f"HTTP status_code must be an integer between 100 and 599, got {status_code!r}"
        )
    resolved_message = (
        _default_reason_phrase(status_code) if message is None else message
    )
    raise HttpException(
        message=resolved_message,
        status_code=status_code,
        headers=headers,
    )


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
    "abort",
]
