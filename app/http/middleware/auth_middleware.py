from __future__ import annotations

from swiftpy.auth.auth_manager import AuthManager
from swiftpy.core.container import get_container
from swiftpy.core.context import Context
from swiftpy.core.exceptions import HttpException
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


class AuthMiddleware(Middleware):
    """
    Authentication ASGI middleware for SwiftPY.

    Validates incoming credentials through the application's AuthManager guard,
    binds the authenticated user entity to `request.state["user"]` and
    task-isolated `Context`, and raises HTTP 401 when authentication fails.
    """

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        container = get_container()
        auth_manager = (
            container.resolve(AuthManager)
            if container.has(AuthManager)
            else AuthManager(container)
        )
        user = await auth_manager.authenticate(request)
        if user is None:
            raise HttpException(
                status_code=401,
                message="Unauthenticated: Missing or invalid credentials",
            )

        Context.set("__swiftpy_logged_out__", False)
        Context.set("auth.user", user)
        Context.set("__swiftpy_current_user__", user)
        if hasattr(user, "__table__"):
            Context.set("__swiftpy_current_user_model__", user)
        request.state.pop("__swiftpy_logged_out__", None)
        request.state["user"] = user

        return await next_handler(request)
