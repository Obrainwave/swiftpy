from __future__ import annotations

from swiftpy.core.context import Context
from swiftpy.core.exceptions import HttpException
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


class AdminMiddleware(Middleware):
    """
    Authorization ASGI middleware for administrative access control.

    Retrieves the authenticated user from task-isolated `Context` or `request.state`.
    Verifies that the user exists and has administrative privileges (`user.is_admin` is True).
    Raises HTTP 401 Unauthorized if unauthenticated, or HTTP 403 Forbidden if not an administrator.
    """

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        user = Context.get("auth.user") or request.state.get("user")
        if not user:
            raise HttpException(
                status_code=401,
                message="Unauthenticated: Please authenticate first",
            )

        is_admin = getattr(user, "is_admin", False)
        if not is_admin:
            raise HttpException(
                status_code=403,
                message="Forbidden: Administrator access required",
            )

        return await next_handler(request)

