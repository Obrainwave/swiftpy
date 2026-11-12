from __future__ import annotations

from swiftpy.core.application import Application
from swiftpy.core.exceptions import HttpException
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response


class ResourceConflictException(HttpException):
    """Raised when an operation conflicts with existing server state (HTTP 409)."""

    def __init__(self, message: str = "Resource conflict") -> None:
        super().__init__(status_code=409, message=message)


def register_exception_handlers(app: Application) -> None:
    """Register custom application error renderers onto the application."""

    @app.exception_handler(ResourceConflictException)
    async def handle_conflict(
        request: Request, exc: ResourceConflictException
    ) -> Response:
        return json_response(
            {"error": "conflict", "detail": exc.message}, status_code=409
        )
