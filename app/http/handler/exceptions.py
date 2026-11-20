from __future__ import annotations

from swiftpy.core.application import Application
from swiftpy.core.exceptions import HttpException


class ResourceConflictException(HttpException):
    """Raised when an operation conflicts with existing server state (HTTP 409)."""

    status_code = 409
    message = "Resource conflict"


def register_exception_handlers(app: Application) -> None:
    """Register custom exception renderers onto `app` when non-default JSON shapes are needed."""
    _ = app
