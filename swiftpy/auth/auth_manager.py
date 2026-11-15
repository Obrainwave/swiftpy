from __future__ import annotations

import inspect
import weakref
from collections.abc import Callable, Iterator
from typing import Any

from swiftpy.auth.api_key_guard import ApiKeyGuard
from swiftpy.auth.jwt_guard import JWTGuard
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.auth.token_blacklist import TokenBlacklist
from swiftpy.core.config import Config
from swiftpy.core.container import Container, get_container
from swiftpy.core.context import Context
from swiftpy.database.pool import DatabasePool
from swiftpy.http.request import Request


class CurrentUser(dict[str, Any]):
    """
    Task-scoped authenticated user context resolvable through the DI container.

    Resolves lazily from `Context` at access time when constructed without explicit
    payload data, stays synchronized with context updates so C-level readers like
    `dict(user)` and `json.dumps(user)` see live data, and is never cached in
    the container scope while unauthenticated.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._lazy = data is None
        self._last_synced: dict[str, Any] = {}
        if data is None:
            ctx_user = Context.get("__swiftpy_current_user__")
            initial = dict(ctx_user) if isinstance(ctx_user, dict) else {}
            super().__init__(initial)
            self._last_synced = dict(initial)
            Context.watch(
                "__swiftpy_current_user__",
                weakref.WeakMethod(self._apply_context_payload),
            )
        else:
            super().__init__(data)

    def _apply_context_payload(self, payload: Any) -> None:
        if not self._lazy:
            return
        target = dict(payload) if isinstance(payload, dict) else {}
        if target != self._last_synced or dict.keys(self) != target.keys():
            super().clear()
            super().update(target)
            self._last_synced = target

    def _sync(self) -> None:
        if not self._lazy:
            return
        ctx_user = Context.get("__swiftpy_current_user__")
        target = ctx_user if isinstance(ctx_user, dict) else {}
        if target != self._last_synced:
            self._apply_context_payload(target)

    @property
    def _cache_in_scope(self) -> bool:
        self._sync()
        return super().__len__() > 0

    def __getitem__(self, key: str) -> Any:
        self._sync()
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self._sync()
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        self._sync()
        return super().__contains__(key)

    def __len__(self) -> int:
        self._sync()
        return super().__len__()

    def __bool__(self) -> bool:
        self._sync()
        return super().__len__() > 0

    def __iter__(self) -> Iterator[str]:
        self._sync()
        return super().__iter__()

    def keys(self) -> Any:
        self._sync()
        return super().keys()

    def values(self) -> Any:
        self._sync()
        return super().values()

    def items(self) -> Any:
        self._sync()
        return super().items()

    def copy(self) -> dict[str, Any]:
        self._sync()
        return super().copy()

    def to_dict(self) -> dict[str, Any]:
        self._sync()
        return super().copy()

    def __repr__(self) -> str:
        self._sync()
        return super().__repr__()

    def __eq__(self, other: object) -> bool:
        self._sync()
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        self._sync()
        return super().__ne__(other)

    @property
    def id(self) -> Any:
        self._sync()
        return super().get("id") or super().get("sub")

    @property
    def email(self) -> Any:
        self._sync()
        return super().get("email")

    @property
    def is_authenticated(self) -> bool:
        self._sync()
        return super().__len__() > 0


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
        self._custom_creators: dict[str, Callable[[Container], Any]] = {
            "jwt": self._create_default_jwt_guard,
            "api_key": self._create_default_api_key_guard,
        }

    @staticmethod
    def _create_default_jwt_guard(c: Container) -> JWTGuard:
        issuer = c.resolve(JWTIssuer) if c.has(JWTIssuer) else None
        blacklist = c.resolve(TokenBlacklist) if c.has(TokenBlacklist) else None
        return JWTGuard(issuer=issuer, blacklist=blacklist)

    @staticmethod
    def _create_default_api_key_guard(c: Container) -> ApiKeyGuard:
        cfg = c.resolve(Config) if c.has(Config) else None
        pool = c.resolve(DatabasePool) if c.has(DatabasePool) else None
        return ApiKeyGuard.from_config(config=cfg, pool=pool)

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
        """Register or override a guard creator factory and invalidate any cached guard."""
        self._custom_creators[name] = creator
        self._guards.pop(name, None)

    def _resolve(self, name: str) -> Any:
        if name in self._custom_creators:
            return self._custom_creators[name](self.container)

        raise ValueError(f"Auth guard [{name}] is not defined.")

    async def authenticate(
        self,
        request: Request | None = None,
        guard_name: str | None = None,
    ) -> Any:
        """
        Authenticate the request by dispatching to the guard instance registered
        for `guard_name` (or the default configured guard).
        """
        resolved_name = guard_name or self.default_guard_name()
        g = self.guard(resolved_name)

        if hasattr(g, "authenticate") and callable(g.authenticate):
            res = g.authenticate(request)
            if inspect.isawaitable(res):
                return await res
            return res

        if callable(g):
            res = g(request)
            if inspect.isawaitable(res):
                return await res
            return res

        raise TypeError(f"Guard [{resolved_name}] does not support authenticate()")

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
            lambda _: CurrentUser(),
        )
