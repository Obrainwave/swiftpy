from __future__ import annotations

import copy
import inspect
import logging
import math
import time
import weakref
from collections.abc import Callable, Iterator
from typing import Any

from swiftpy.auth.api_key_guard import ApiKeyGuard
from swiftpy.auth.exceptions import (
    AuthenticationError,
    AuthenticationServiceUnavailableError,
    JWTError,
)
from swiftpy.auth.jwt_guard import JWTGuard
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.auth.token_blacklist import TokenBlacklist, is_token_blacklist_enabled
from swiftpy.auth.user_context import (
    CONTEXT_AUTH_GUARD_KEY,
    CONTEXT_LOGGED_OUT_KEY,
    CONTEXT_USER_KEY,
    CONTEXT_USER_MODEL_KEY,
    SENSITIVE_USER_KEYS,
    resolve_user_model_class,
)
from swiftpy.core.config import Config
from swiftpy.core.container import Container, get_container
from swiftpy.core.context import Context
from swiftpy.database.orm.model import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.http.request import Request

logger = logging.getLogger("swiftpy.auth.manager")

_CONTEXT_USER_KEY = CONTEXT_USER_KEY
_CONTEXT_USER_MODEL_KEY = CONTEXT_USER_MODEL_KEY
_CONTEXT_AUTH_GUARD_KEY = CONTEXT_AUTH_GUARD_KEY

_SENSITIVE_KEYS: frozenset[str] = SENSITIVE_USER_KEYS


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


def _adopt_request_state_user() -> Any:
    """
    Adopt a user already stored in `request.state` (for example by a guard that ran in a
    different task) into `Context`. Never decodes credentials: authentication stays with
    guards (`JWTGuard`, `require_jwt`), which also enforce token revocation.
    """
    existing = Context.get(_CONTEXT_USER_KEY)
    if existing is not None or Context.get(CONTEXT_LOGGED_OUT_KEY):
        return existing

    req = Context.get("request")
    if not isinstance(req, Request) or not isinstance(req.state, dict):
        return None
    if req.state.get(CONTEXT_LOGGED_OUT_KEY):
        return None
    state_user = req.state.get("user")
    if state_user is None:
        return None
    Context.set(_CONTEXT_USER_KEY, state_user)
    if "auth_guard" in req.state:
        Context.set(_CONTEXT_AUTH_GUARD_KEY, req.state["auth_guard"])
    return state_user


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

    ctx_payload = Context.get(_CONTEXT_USER_KEY)
    if isinstance(ctx_payload, dict):
        for k, v in ctx_payload.items():
            if k not in hidden_fields and k not in _SENSITIVE_KEYS and k not in data:
                data[k] = v

    return AuthUser(data, model=model_inst)


def set_user_model(model: Any, guard: str | None = None) -> None:
    """
    Store the authenticated ORM model instance in the current task-scoped Context.

    Call this from your authentication middleware after fetching the user from the
    database. ``auth().user()`` reads from this key.
    """
    Context.set(CONTEXT_LOGGED_OUT_KEY, False)
    Context.set(_CONTEXT_USER_MODEL_KEY, model)
    if guard is not None:
        Context.set(_CONTEXT_AUTH_GUARD_KEY, guard)
    user_dict: dict[str, Any] | None = None
    if hasattr(model, "to_dict") and callable(model.to_dict):
        user_dict = model.to_dict()
        Context.set(_CONTEXT_USER_KEY, user_dict)
    elif isinstance(model, dict):
        user_dict = model
        Context.set(_CONTEXT_USER_KEY, user_dict)

    req = Context.get("request")
    if isinstance(req, Request) and isinstance(req.state, dict):
        req.state.pop(CONTEXT_LOGGED_OUT_KEY, None)
        if user_dict is not None:
            req.state["user"] = user_dict
        if guard is not None:
            req.state["auth_guard"] = guard


def current_user_model() -> Any | None:
    """
    Retrieve the authenticated ORM Model instance for the current request task.
    Returns None if no model has been stored via set_user_model().
    """
    model = Context.get(_CONTEXT_USER_MODEL_KEY)
    if model is not None:
        return model
    _adopt_request_state_user()
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
        if Context.get(_CONTEXT_USER_KEY) is None and Context.get(_CONTEXT_USER_MODEL_KEY) is None:
            _adopt_request_state_user()

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

    @staticmethod
    def _extract_bearer_token(request: Request | None) -> str | None:
        if request is None:
            return None
        auth_header = request.headers.get("authorization")
        if not auth_header:
            return None
        parts = auth_header.strip().split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        token = parts[1].strip()
        return token or None

    @staticmethod
    def _resolve_blacklist_and_issuer(
        *,
        blacklist: TokenBlacklist | None = None,
        issuer: JWTIssuer | None = None,
    ) -> tuple[TokenBlacklist | None, JWTIssuer | None]:
        active_blacklist = blacklist
        active_issuer = issuer
        try:
            container = get_container()
        except RuntimeError:
            container = None

        if container is not None:
            cfg = container.resolve(Config) if container.has(Config) else None
            if (
                active_blacklist is None
                and is_token_blacklist_enabled(cfg)
                and container.has(TokenBlacklist)
            ):
                active_blacklist = container.resolve(TokenBlacklist)
            if active_issuer is None and container.has(JWTIssuer):
                active_issuer = container.resolve(JWTIssuer)
            elif active_issuer is None and cfg is not None:
                active_issuer = JWTIssuer.from_config(cfg)

        return active_blacklist, active_issuer

    @staticmethod
    def _clear_auth_context(request: Request | None = None) -> None:
        Context.set(CONTEXT_LOGGED_OUT_KEY, True)
        Context.set(_CONTEXT_USER_KEY, None)
        Context.set(_CONTEXT_USER_MODEL_KEY, None)
        Context.set(_CONTEXT_AUTH_GUARD_KEY, None)
        req = request if isinstance(request, Request) else Context.get("request")
        if isinstance(req, Request) and hasattr(req, "state"):
            req.state.pop("user", None)
            req.state.pop("auth_guard", None)
            req.state[CONTEXT_LOGGED_OUT_KEY] = True

    @staticmethod
    async def _revoke_raw_token(
        blacklist: TokenBlacklist,
        raw_token: str,
        *,
        issuer: JWTIssuer | None,
        token_label: str,
    ) -> None:
        try:
            await blacklist.revoke_token(raw_token, issuer=issuer)
        except JWTError:
            raise
        except Exception as exc:
            logger.exception("Failed to revoke %s token during logout", token_label)
            raise AuthenticationServiceUnavailableError(
                "Authentication service unavailable"
            ) from exc

    async def logout(
        self,
        request: Request | None = None,
        *,
        token: str | None = None,
        refresh_token: str | None = None,
        blacklist: TokenBlacklist | None = None,
        issuer: JWTIssuer | None = None,
        clear_context: bool = True,
    ) -> None:
        """
        Revoke the current session's JWT token(s) in `TokenBlacklist` and clear request auth context.
        """
        raw_req = request if request is not None else Context.get("request")
        req = raw_req if isinstance(raw_req, Request) else None
        target_token = (
            token if token is not None else self._extract_bearer_token(req)
        )
        active_blacklist, active_issuer = self._resolve_blacklist_and_issuer(
            blacklist=blacklist,
            issuer=issuer,
        )

        if active_blacklist is not None:
            if target_token is not None:
                await self._revoke_raw_token(
                    active_blacklist,
                    target_token,
                    issuer=active_issuer,
                    token_label="access",
                )
            else:
                ctx_payload = Context.get(_CONTEXT_USER_KEY)
                if (
                    not isinstance(ctx_payload, dict)
                    and req is not None
                    and isinstance(req.state.get("user"), dict)
                ):
                    ctx_payload = req.state.get("user")
                if isinstance(ctx_payload, dict) and ctx_payload.get("jti"):
                    raw_exp = ctx_payload.get("exp")
                    ttl: int | None = None
                    should_revoke = True
                    if (
                        raw_exp is not None
                        and not isinstance(raw_exp, bool)
                        and isinstance(raw_exp, (int, float))
                        and math.isfinite(raw_exp)
                    ):
                        remaining = int(raw_exp - time.time())
                        if remaining <= 0:
                            should_revoke = False
                        else:
                            ttl = remaining
                    if should_revoke:
                        try:
                            await active_blacklist.revoke(
                                str(ctx_payload["jti"]),
                                ttl=ttl,
                            )
                        except Exception as exc:
                            logger.exception("Failed to revoke context JTI during logout")
                            raise AuthenticationServiceUnavailableError(
                                "Authentication service unavailable"
                            ) from exc

            if refresh_token is not None:
                await self._revoke_raw_token(
                    active_blacklist,
                    refresh_token,
                    issuer=active_issuer,
                    token_label="refresh",
                )

        if clear_context:
            self._clear_auth_context(req)

    async def logout_all(
        self,
        subject: str | int | None = None,
        *,
        blacklist: TokenBlacklist | None = None,
        ttl: int | None = None,
        clear_context: bool = True,
    ) -> None:
        """
        Revoke all tokens issued for `subject` (or the currently authenticated user)
        via `TokenBlacklist.revoke_all_for_subject()`.
        """
        current_subject = self.id()
        if current_subject is None:
            raw_user = Context.get(_CONTEXT_USER_KEY)
            if isinstance(raw_user, dict):
                current_subject = raw_user.get("sub") or raw_user.get("id")

        target_subject = current_subject if subject is None else subject
        if target_subject is None or str(target_subject) == "":
            raise AuthenticationError(
                "No authenticated user or subject provided for logout_all()."
            )

        active_blacklist, _ = self._resolve_blacklist_and_issuer(blacklist=blacklist)
        if active_blacklist is not None:
            try:
                await active_blacklist.revoke_all_for_subject(
                    target_subject,
                    ttl=ttl,
                )
            except ValueError:
                raise
            except Exception as exc:
                logger.exception(
                    "Failed to revoke all tokens for subject %s",
                    target_subject,
                )
                raise AuthenticationServiceUnavailableError(
                    "Authentication service unavailable"
                ) from exc

        if clear_context and (
            subject is None
            or (
                current_subject is not None
                and str(target_subject) == str(current_subject)
            )
        ):
            self._clear_auth_context()


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
            if ctx_user is None:
                ctx_user = _adopt_request_state_user()
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
        if ctx_user is None:
            ctx_user = _adopt_request_state_user()
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
    user = Context.get("__swiftpy_current_user__")
    if user is None:
        user = _adopt_request_state_user()
    return user


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
