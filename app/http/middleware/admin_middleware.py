from __future__ import annotations

from collections.abc import Mapping

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
        user = Context.get("auth.user")
        if user is None:
            user = Context.get("__swiftpy_current_user__")
        if user is None:
            user = request.state.get("user")
        if user is None:
            raise HttpException(
                status_code=401,
                message="Unauthenticated: Please authenticate first",
            )

        is_admin = (
            user.get("is_admin") is True
            if isinstance(user, Mapping)
            else getattr(user, "is_admin", None) is True
        )
        if not is_admin:
            raise HttpException(
                status_code=403,
                message="Forbidden: Administrator access required",
            )

        return await next_handler(request)

