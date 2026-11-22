from __future__ import annotations

import inspect
import logging
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, get_type_hints

from swiftpy.database.exceptions import ModelNotFoundError, RecordNotFoundError
from swiftpy.filesystem.exceptions import FileValidationError
from swiftpy.http.exceptions import (
    ClientDisconnect,
    HttpException,
    PayloadTooLargeError,
    TooManyRequestsError,
    abort,
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


def _infer_exception_handler_order(
    handler: Callable[..., Any],
    *,
    exception_type: type[Exception] | None = None,
) -> tuple[str, str]:
    """Resolve the supported (request, exc) or (exc, request) convention.

    We accept both conventions, but require an unambiguous result derived from
    the parameter names or the resolved type hints. Ambiguous or unsupported
    signatures raise TypeError during registration so the handler contract is
    explicit instead of guessing at runtime.
    """
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise TypeError(f"Invalid exception handler signature for {handler!r}") from exc

    params = list(sig.parameters.values())
    if len(params) != 2:
        raise TypeError(
            "Exception handlers must accept exactly two parameters: (request, exc) or (exc, request)."
        )

    try:
        hints = get_type_hints(handler)
    except (AttributeError, NameError, TypeError, ValueError):
        hints = {}

    first_name = params[0].name.lower()
    second_name = params[1].name.lower()
    first_hint = hints.get(params[0].name, params[0].annotation)
    second_hint = hints.get(params[1].name, params[1].annotation)

    request_like = (
        first_name in {"request", "req"}
        or getattr(first_hint, "__name__", "") in {"Request", "HttpRequest"}
    )
    exception_like = (
        first_name in {"exc", "error"}
        or (
            exception_type is not None
            and isinstance(first_hint, type)
            and issubclass(first_hint, BaseException)
            and issubclass(first_hint, exception_type)
        )
    )
    second_request_like = (
        second_name in {"request", "req"}
        or getattr(second_hint, "__name__", "") in {"Request", "HttpRequest"}
    )
    second_exception_like = (
        second_name in {"exc", "error"}
        or (
            exception_type is not None
            and isinstance(second_hint, type)
            and issubclass(second_hint, BaseException)
            and issubclass(second_hint, exception_type)
        )
    )

    if request_like and second_exception_like:
        return "request_first", "request_first"
    if exception_like and second_request_like:
        return "exception_first", "exception_first"

    if first_name in {"request", "req"} and second_name in {"exc", "error"}:
        return "request_first", "request_first"
    if first_name in {"exc", "error"} and second_name in {"request", "req"}:
        return "exception_first", "exception_first"

    if request_like and second_request_like:
        raise TypeError(
            "Ambiguous exception handler signature: both parameters appear to be Request-like. "
            "Use (request, exc) or (exc, request) with explicit annotations."
        )
    if exception_like and second_exception_like:
        raise TypeError(
            "Ambiguous exception handler signature: both parameters appear to be exception-like. "
            "Use (request, exc) or (exc, request) with explicit annotations."
        )

    raise TypeError(
        "Unsupported exception handler signature; expected (request, exc) or (exc, request)."
    )


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
        handler: Callable[..., Any],
    ) -> None:
        """Register a custom handler for a specific exception class."""
        _infer_exception_handler_order(handler, exception_type=exc_type)
        self._custom_handlers[exc_type] = handler

    def __call__(
        self,
        exc_type: type[E],
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register a custom exception handler."""

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.register(exc_type, handler)
            return handler

        return decorator

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
                    order = _infer_exception_handler_order(handler, exception_type=cls)
                    if order[0] == "request_first":
                        res = handler(request, exc)
                    else:
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

        # 2. Database Record / Model Not Found errors automatically map to HTTP 404
        if isinstance(exc, (ModelNotFoundError, RecordNotFoundError)):
            logger.info(
                "HTTP 404 %s %s: %s",
                req_method,
                req_path,
                exc.message,
            )
            return json_response(
                {"detail": exc.message},
                status_code=404,
            )

        # 3. Framework validation errors ONLY (no duck-typing on .errors)
        if isinstance(exc, (ValidationError, ValidationException, FileValidationError)):
            logger.info(
                "HTTP 422 %s %s: %s",
                req_method,
                req_path,
                exc.message,
            )
            headers = exc.headers or {}
            if isinstance(exc, ValidationException) and exc.is_single:
                resp = json_response(exc.to_dict(), status_code=422)
            else:
                resp = unprocessable(exc.errors)
            if headers:
                resp.headers.update(headers)
            return resp

        # 4. HTTP Exceptions (HttpException, NotFoundError, UnauthorizedError, etc.)
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
            detail = (
                "Internal Server Error"
                if (not self.debug and exc.status_code >= 500)
                else exc.message
            )
            resp = json_response(
                {"detail": detail},
                status_code=exc.status_code,
            )
            if headers:
                resp.headers.update(headers)
            return resp

        # 5. Unhandled server exceptions
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
    "TooManyRequestsError",
    "UnauthorizedError",
    "ValidationError",
    "abort",
]
