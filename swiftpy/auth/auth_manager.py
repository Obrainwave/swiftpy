from __future__ import annotations

from collections.abc import Callable
from typing import Any

from swiftpy.auth.api_key_guard import ApiKeyGuard
from swiftpy.auth.jwt_guard import JWTGuard
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.auth.token_blacklist import TokenBlacklist
from swiftpy.core.config import Config
from swiftpy.core.container import Container, get_container
from swiftpy.core.context import Context
from swiftpy.http.request import Request


class CurrentUser(dict[str, Any]):
    """
    Task-scoped authenticated user context resolvable through the DI container.

    Can be injected directly into route handlers or service constructors:
        async def profile(request: Request, user: CurrentUser) -> Response:
            ...
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        super().__init__(data or {})

    @property
    def id(self) -> Any:
        return self.get("id") or self.get("sub")

    @property
    def email(self) -> Any:
        return self.get("email")

    @property
    def is_authenticated(self) -> bool:
        return bool(self)


def current_user() -> Any:
    """
    Retrieve the authenticated user payload/record for the current request task.
    Returns None if unauthenticated.
    """
    return Context.get("__swiftpy_current_user__")


class AuthManager:
    """
    Central authentication coordinator.

    Resolves and caches configured guards ('jwt', 'api_key', or custom drivers),
    and exposes the authenticated current_user() through the DI container
    with strict task-scoped isolation.
    """

    def __init__(self, container: Container | None = None) -> None:
        self._container = container
        self._guards: dict[str, Any] = {}
        self._custom_creators: dict[str, Callable[[Container], Any]] = {}

    @property
    def container(self) -> Container:
        if self._container is None:
            self._container = get_container()
        return self._container

    def default_guard_name(self) -> str:
        """Retrieve the name of the default guard from configuration."""
        try:
            if self.container.has(Config):
                cfg = self.container.resolve(Config)
                guard_name = cfg.get("auth.defaults.guard") or cfg.get("auth.default")
                if guard_name and isinstance(guard_name, str):
                    return guard_name
        except RuntimeError:
            pass

        return "jwt"

    def guard(self, name: str | None = None) -> Any:
        """
        Resolve a guard driver instance by name (e.g. 'jwt', 'api_key').
        If name is None, resolves the default configured guard.
        """
        guard_name = name or self.default_guard_name()

        if guard_name in self._guards:
            return self._guards[guard_name]

        guard_instance = self._resolve(guard_name)
        self._guards[guard_name] = guard_instance
        return guard_instance

    def extend(self, name: str, creator: Callable[[Container], Any]) -> None:
        """Register a custom guard creator factory."""
        self._custom_creators[name] = creator

    def _resolve(self, name: str) -> Any:
        if name in self._custom_creators:
            return self._custom_creators[name](self.container)

        if name == "jwt":
            issuer = (
                self.container.resolve(JWTIssuer)
                if self.container.has(JWTIssuer)
                else None
            )
            blacklist = (
                self.container.resolve(TokenBlacklist)
                if self.container.has(TokenBlacklist)
                else None
            )
            return JWTGuard(issuer=issuer, blacklist=blacklist)

        if name == "api_key":
            return ApiKeyGuard()

        raise ValueError(f"Auth guard [{name}] is not defined.")

    async def authenticate(
        self,
        request: Request | None = None,
        guard_name: str | None = None,
    ) -> Any:
        """
        Authenticate the request using the specified or default guard.
        """
        g = self.guard(guard_name)
        if hasattr(g, "authenticate"):
            return await g.authenticate(request)

        # If guard is a middleware with require_jwt or require_api_key
        if guard_name == "jwt" or (
            guard_name is None and self.default_guard_name() == "jwt"
        ):
            from swiftpy.auth.jwt_guard import require_jwt

            return await require_jwt(request)

        if guard_name == "api_key" or (
            guard_name is None and self.default_guard_name() == "api_key"
        ):
            from swiftpy.auth.api_key_guard import require_api_key

            return await require_api_key(request)

        raise TypeError(f"Guard [{guard_name}] does not support authenticate()")

    def current_user(self) -> Any:
        """Return the current user from task-scoped context."""
        return current_user()

    def register_container_bindings(self, container: Container | None = None) -> None:
        """
        Register AuthManager as a singleton and CurrentUser as a task-scoped binding
        in the dependency injection container.
        """
        c = container or self.container
        c.instance(AuthManager, self)
        c.scoped(
            CurrentUser,
            lambda _: CurrentUser(Context.get("__swiftpy_current_user__") or {}),
        )
