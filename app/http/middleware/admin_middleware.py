from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from swiftpy.core.context import Context
from swiftpy.core.exceptions import HttpException
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


def _resolve_authenticated_user(request: Request) -> Any:
    """Resolve the active authenticated user across Context and request.state, respecting logout state."""
    if Context.get("__swiftpy_logged_out__") or request.state.get(
        "__swiftpy_logged_out__"
    ):
        return None
    user = Context.get("auth.user")
    if user is None:
        user = Context.get("__swiftpy_current_user__")
    if user is None:
        user = Context.get("__swiftpy_current_user_model__")
    if user is None:
        user = request.state.get("user")
    return user


def _is_admin_user(user: Any) -> bool:
    """Return True only when user carries an explicit boolean `is_admin is True` in a mapping or model."""
    if isinstance(user, Mapping):
        raw_flag = user.get("is_admin")
        if raw_flag is not None:
            return raw_flag is True
        if getattr(user, "is_admin", None) is True:
            return True
        model = getattr(user, "model", None) or Context.get(
            "__swiftpy_current_user_model__"
        )
        return getattr(model, "is_admin", None) is True
    return getattr(user, "is_admin", None) is True


class AdminMiddleware(Middleware):
    """
    Authorization ASGI middleware for administrative access control.

    Retrieves the authenticated user from task-isolated `Context` or `request.state`.
    Verifies that the user exists and has administrative privileges (`user.is_admin` is True).
    Raises HTTP 401 Unauthorized if unauthenticated, or HTTP 403 Forbidden if not an administrator.
    """

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        user = _resolve_authenticated_user(request)
        if user is None:
            raise HttpException(
                status_code=401,
                message="Unauthenticated: Please authenticate first",
            )

        if not _is_admin_user(user):
            raise HttpException(
                status_code=403,
                message="Forbidden: Administrator access required",
            )

        return await next_handler(request)
