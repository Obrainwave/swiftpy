from __future__ import annotations

import copy
import importlib
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
from swiftpy.database.orm.model import Model
from swiftpy.database.orm.registry import _MODEL_REGISTRY
from swiftpy.database.pool import DatabasePool
from swiftpy.http.request import Request

_CONTEXT_USER_KEY = "__swiftpy_current_user__"
_CONTEXT_USER_MODEL_KEY = "__swiftpy_current_user_model__"
_CONTEXT_AUTH_GUARD_KEY = "__swiftpy_auth_guard__"

_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "password",
    "password_hash",
    "key_hash",
    "secret",
    "token",
})


class AuthUser(dict[str, Any]):
    """
    Authenticated user entity wrapping task-scoped user context.

    Provides both attribute-style (`user.name`, `user.id`) and dict-style (`user['name']`)
    access to all declared fields of the application's User model, with all `__hidden__`
    fields strictly excluded.
    """

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        model: Model | None = None,
    ) -> None:
        self._model: Model | None = model
        super().__init__(data or {})

    @property
    def model(self) -> Model | None:
        """Return the underlying ORM Model instance if one was resolved or provided."""
        return self._model

    @property
    def id(self) -> Any:
        """Return the user primary key or token subject identifier."""
        return self.get("id") or self.get("sub")

    @property
    def email(self) -> Any:
        """Return the user email address."""
        return self.get("email")

    @property
    def is_authenticated(self) -> bool:
        """Return True if this user entity contains authenticated attributes."""
        return len(self) > 0

    def to_dict(self) -> dict[str, Any]:
        """Return a copy of the declared attributes without hidden fields."""
        return dict(self)

    def __getattr__(self, name: str) -> Any:
        if name in self:
            return self[name]
        if self._model is not None and hasattr(self._model, name):
            hidden = set(getattr(self._model, "__hidden__", ()) or ())
            if name not in hidden:
                return getattr(self._model, name)
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self[name] = value

    def __repr__(self) -> str:
        return f"AuthUser({super().__repr__()})"


def resolve_user_model_class() -> type[Model] | None:
    """
    Resolve the application's User model class.

    Checks:
    1. Container Config for 'auth.model', 'auth.user_model', or 'auth.providers.users.model'.
    2. Global _MODEL_REGISTRY for a model named 'User' or ending with 'User'.
    3. Import fallback for 'app.models.user.User'.
    """
    try:
        container = get_container()
        if container.has(Config):
            cfg = container.resolve(Config)
            model_setting = (
                cfg.get("auth.model")
                or cfg.get("auth.user_model")
                or cfg.get("auth.providers.users.model")
            )
            if isinstance(model_setting, type) and issubclass(model_setting, Model):
                return model_setting
            if isinstance(model_setting, str) and model_setting:
                if "." in model_setting:
                    mod_name, cls_name = model_setting.rsplit(".", 1)
                    mod = importlib.import_module(mod_name)
                    target = getattr(mod, cls_name, None)
                    if isinstance(target, type) and issubclass(target, Model):
                        return target
                elif model_setting in _MODEL_REGISTRY:
                    variants = _MODEL_REGISTRY[model_setting]
                    if variants:
                        return next(iter(variants.values()))
    except (RuntimeError, KeyError, AttributeError, ImportError, ValueError):
        pass

    if "User" in _MODEL_REGISTRY:
        candidates = _MODEL_REGISTRY["User"]
        if candidates:
            for qname, cls in candidates.items():
                if "app.models" in qname:
                    return cls
            return next(iter(candidates.values()))

    for model_name, candidates in _MODEL_REGISTRY.items():
        if model_name.lower().endswith("user") and candidates:
            return next(iter(candidates.values()))

    try:
        mod = importlib.import_module("app.models.user")
        target = getattr(mod, "User", None)
        if isinstance(target, type) and issubclass(target, Model):
            return target
    except (ImportError, AttributeError):
        pass

    return None


def _build_auth_user_from_dict(raw: dict[str, Any]) -> AuthUser:
    user_cls = resolve_user_model_class()
    if user_cls is not None and hasattr(user_cls, "__fields__"):
        hidden_fields = set(getattr(user_cls, "__hidden__", ()) or ())
        data: dict[str, Any] = {}
        defaults = getattr(user_cls, "__field_defaults__", {})
        factories = getattr(user_cls, "__field_factories__", {})

        for field_name, field_type in user_cls.__fields__.items():
            if field_name in hidden_fields:
                continue
            if field_name in raw:
                val = raw[field_name]
                if field_type in (int, "int") or (field_name == "id" and isinstance(val, str) and val.isdigit()):
                    try:
                        val = int(val)
                    except (ValueError, TypeError):
                        pass
                data[field_name] = val
            elif field_name == "id" and "sub" in raw:
                val = raw["sub"]
                if field_type in (int, "int") or (isinstance(val, str) and val.isdigit()):
                    try:
                        val = int(val)
                    except (ValueError, TypeError):
                        pass
                data["id"] = val
            elif field_name in factories:
                data[field_name] = factories[field_name]()
            elif field_name in defaults:
                data[field_name] = copy.deepcopy(defaults[field_name])
            else:
                data[field_name] = None

        for k, v in raw.items():
            if k not in hidden_fields and k not in _SENSITIVE_KEYS and k not in data:
                data[k] = v

        return AuthUser(data, model=None)

    clean = {k: v for k, v in raw.items() if k not in _SENSITIVE_KEYS}
    if "id" not in clean and "sub" in clean:
        sub_val = clean["sub"]
        if isinstance(sub_val, str) and sub_val.isdigit():
            try:
                sub_val = int(sub_val)
            except (ValueError, TypeError):
                pass
        clean["id"] = sub_val
    return AuthUser(clean, model=None)


def _build_auth_user_from_model(model: Any) -> AuthUser:
    if hasattr(model, "to_dict") and callable(model.to_dict):
        data = model.to_dict()
    elif isinstance(model, dict):
        return _build_auth_user_from_dict(model)
    else:
        data = {}

    model_inst = model if isinstance(model, Model) else None

    fields_config = getattr(model, "__fields__", {})
    hidden_fields = set(getattr(model, "__hidden__", ()) or ())
    for f in fields_config:
        if f not in hidden_fields and f not in data:
            data[f] = getattr(model, f, None)

    return AuthUser(data, model=model_inst)


def set_user_model(model: Any, guard: str | None = None) -> None:
    """
    Store the authenticated ORM model instance in the current task-scoped Context.

    Call this from your authentication middleware after fetching the user from the
    database. ``auth().user()`` reads from this key.
    """
    Context.set(_CONTEXT_USER_MODEL_KEY, model)
    if guard is not None:
        Context.set(_CONTEXT_AUTH_GUARD_KEY, guard)
    if hasattr(model, "to_dict") and callable(model.to_dict):
        Context.set(_CONTEXT_USER_KEY, model.to_dict())
    elif isinstance(model, dict):
        Context.set(_CONTEXT_USER_KEY, model)


def current_user_model() -> Any | None:
    """
    Retrieve the authenticated ORM Model instance for the current request task.
    Returns None if no model has been stored via set_user_model().
    """
    return Context.get(_CONTEXT_USER_MODEL_KEY)


class AuthProxy:
    """
    Request-scoped authentication proxy for a specific guard or default guard.

    Provides Laravel-like `.check()`, `.user()`, `.guest()`, and `.id()`
    methods bound to the current task-scoped Context.
    """

    def __init__(self, guard: str | None = None) -> None:
        self._guard = guard

    def user(self) -> AuthUser | None:
        """
        Return the authenticated user for the current request task.

        Returns an `AuthUser` containing all declared fields of the User model
        with all `__hidden__` fields excluded. Returns `None` if unauthenticated
        or if the active guard does not match the requested guard.
        """
        if self._guard is not None:
            active_guard = Context.get(_CONTEXT_AUTH_GUARD_KEY)
            if active_guard is None:
                req = Context.get("request")
                if req is not None and hasattr(req, "state"):
                    active_guard = req.state.get("auth_guard")
            if active_guard is not None and active_guard != self._guard:
                return None

        model_inst = current_user_model()
        if model_inst is not None:
            return _build_auth_user_from_model(model_inst)

        raw_user = current_user()
        if raw_user is None:
            return None

        if hasattr(raw_user, "to_dict") and callable(raw_user.to_dict):
            return _build_auth_user_from_model(raw_user)

        if isinstance(raw_user, dict):
            return _build_auth_user_from_dict(raw_user)

        return None

    def check(self) -> bool:
        """Return True if the current request task has an authenticated user."""
        return self.user() is not None

    def guest(self) -> bool:
        """Return True if the current request task is unauthenticated."""
        return not self.check()

    def id(self) -> Any | None:
        """Return the primary key identifier of the authenticated user, or None."""
        u = self.user()
        return u.id if u is not None else None

    async def authenticate(self, request: Request | None = None) -> Any:
        """Authenticate incoming request using the configured guard."""
        try:
            container = get_container()
            auth_mgr = (
                container.resolve(AuthManager)
                if container.has(AuthManager)
                else AuthManager(container)
            )
        except RuntimeError:
            auth_mgr = AuthManager()

        res = auth_mgr.authenticate(request, guard_name=self._guard)
        if inspect.isawaitable(res):
            return await res
        return res


def auth(guard: str | None = None) -> AuthProxy:
    """
    Return a request-scoped authentication proxy.

    Args:
        guard: Optional guard name (e.g. ``"jwt"``, ``"api_key"``).
               If omitted, the default configured guard is used.

    Returns:
        An :class:`AuthProxy` with ``.check()``, ``.user()``, ``.guest()``, and ``.id()`` methods.

    Example::

        from swiftpy.auth_manager import auth

        if auth().check():
            user = auth().user()
            print(user.name, user.email)

        if auth("api_key").check():
            ...
    """
    return AuthProxy(guard=guard)


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

    def __getattr__(self, name: str) -> Any:
        self._sync()
        if name in self:
            return self[name]
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self[name] = value


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
        Context.set("__swiftpy_auth_guard__", resolved_name)
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


__all__ = [
    "AuthManager",
    "AuthProxy",
    "AuthUser",
    "CurrentUser",
    "auth",
    "current_user",
    "current_user_model",
    "resolve_user_model_class",
    "set_user_model",
]
