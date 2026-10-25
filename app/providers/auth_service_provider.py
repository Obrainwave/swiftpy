from __future__ import annotations

from swiftpy.auth import AuthManager, CurrentUser, JWTIssuer, TokenBlacklist
from swiftpy.core.context import Context
from swiftpy.core.providers.base import ServiceProvider


class AuthServiceProvider(ServiceProvider):
    """
    Register authentication services, token management, and task-scoped user resolution.
    """

    def register(self) -> None:
        self.container.singleton(JWTIssuer)
        self.container.singleton(TokenBlacklist)
        self.container.singleton(AuthManager)
        self.container.scoped(
            CurrentUser,
            lambda _: CurrentUser(Context.get("__swiftpy_current_user__") or {}),
        )

    def boot(self) -> None:
        pass
