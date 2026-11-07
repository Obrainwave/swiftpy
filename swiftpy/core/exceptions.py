from __future__ import annotations

import inspect
import logging
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from swiftpy.http.exceptions import (
    ClientDisconnect,
    HttpException,
    PayloadTooLargeError,
)
from swiftpy.http.response import Response, json_response, unprocessable
from swiftpy.validation.exceptions import ValidationException

if TYPE_CHECKING:
    from swiftpy.http.request import Request

logger = logging.getLogger("swiftpy.core.exceptions")
E = TypeVar("E", bound=Exception)


class NotFoundError(HttpException):
    """Exception mapped automatically to HTTP 404 Not Found."""

    def __init__(
        self,
        message: str = "Not Found",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message=message, status_code=404, headers=headers)


class UnauthorizedError(HttpException):
    """Exception mapped automatically to HTTP 401 Unauthorized."""

    def __init__(
        self,
        message: str = "Unauthorized",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message=message, status_code=401, headers=headers)


class ForbiddenError(HttpException):
    """Exception mapped automatically to HTTP 403 Forbidden."""

    def __init__(
        self,
        message: str = "Forbidden",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message=message, status_code=403, headers=headers)


class ValidationError(HttpException):
    """Exception mapped automatically to HTTP 422 Unprocessable Entity."""

    def __init__(
        self,
        errors: dict[str, list[str]] | None = None,
        message: str = "Validation failed",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.errors = errors or {}
        super().__init__(message=message, status_code=422, headers=headers)


class MethodNotAllowedError(HttpException):
    """Exception mapped automatically to HTTP 405 Method Not Allowed."""

    def __init__(
        self,
        message: str = "Method Not Allowed",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message=message, status_code=405, headers=headers)


class BadRequestError(HttpException):
    """Exception mapped automatically to HTTP 400 Bad Request."""

    def __init__(
        self,
        message: str = "Bad Request",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message=message, status_code=400, headers=headers)


class InternalServerError(HttpException):
    """Exception mapped automatically to HTTP 500 Internal Server Error."""

    def __init__(
        self,
        message: str = "Internal Server Error",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message=message, status_code=500, headers=headers)


class ExceptionHandler:
    """
    Global application exception handler.
    Formats HTTP status codes, structured validation errors,
    and returns full tracebacks in development or safe messages in production.
    """

    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self._custom_handlers: dict[type[Exception], Callable[..., Any]] = {}

    def register(
        self,
        exc_type: type[E],
        handler: Callable[[E, Request | None], Any],
    ) -> None:
        """Register a custom handler for a specific exception class."""
        self._custom_handlers[exc_type] = handler

    async def handle(
        self,
        exc: Exception,
        request: Request | None = None,
    ) -> Response:
        """Process an exception and return a typed HTTP Response."""
        req_method = request.method if request is not None else "-"
        req_path = request.path if request is not None else "-"

        # 1. Custom exception handlers selected by MRO specificity
        for cls in type(exc).__mro__:
            if not issubclass(cls, Exception):
                continue
            handler = self._custom_handlers.get(cls)
            if handler is not None:
                try:
                    res = handler(exc, request)
                    if inspect.isawaitable(res):
                        res = await res
                    if isinstance(res, Response):
                        return res
                    return json_response(res)
                except Exception:
                    logger.exception(
                        "Custom exception handler for %s raised an unhandled error",
                        cls.__name__,
                    )
                    return json_response(
                        {"detail": "Internal Server Error"},
                        status_code=500,
                    )

        # 2. Framework validation errors ONLY (no duck-typing on .errors)
        if isinstance(exc, (ValidationError, ValidationException)):
            logger.info(
                "HTTP 422 %s %s: %s",
                req_method,
                req_path,
                exc.message,
            )
            headers = exc.headers or {}
            resp = unprocessable(exc.errors)
            if headers:
                resp.headers.update(headers)
            return resp

        # 3. HTTP Exceptions (HttpException, NotFoundError, UnauthorizedError, etc.)
        if isinstance(exc, HttpException):
            if exc.status_code >= 500:
                logger.error(
                    "HTTP %d %s %s: %s",
                    exc.status_code,
                    req_method,
                    req_path,
                    exc.message,
                )
            else:
                logger.info(
                    "HTTP %d %s %s: %s",
                    exc.status_code,
                    req_method,
                    req_path,
                    exc.message,
                )
            headers = exc.headers or {}
            resp = json_response(
                {"detail": exc.message},
                status_code=exc.status_code,
            )
            if headers:
                resp.headers.update(headers)
            return resp

        # 4. Unhandled server exceptions
        logger.exception("Unhandled exception during request processing")

        if self.debug:
            tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
            return json_response(
                {
                    "error": "Internal Server Error",
                    "exception": exc.__class__.__name__,
                    "message": str(exc),
                    "traceback": tb,
                },
                status_code=500,
            )

        return json_response(
            {"detail": "Internal Server Error"},
            status_code=500,
        )


__all__ = [
    "BadRequestError",
    "ClientDisconnect",
    "ExceptionHandler",
    "ForbiddenError",
    "HttpException",
    "InternalServerError",
    "MethodNotAllowedError",
    "NotFoundError",
    "PayloadTooLargeError",
    "UnauthorizedError",
    "ValidationError",
]
