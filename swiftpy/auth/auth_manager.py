from __future__ import annotations

import copy
import inspect
import logging
import math
import os
import secrets
import time
import types
import weakref
from collections.abc import Callable, Iterator, Mapping
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    Union,
    get_args,
    get_origin,
    overload,
)

from swiftpy.auth.api_key_guard import ApiKeyGuard
from swiftpy.auth.events import (
    Attempting,
    Authenticated,
    Failed,
    Lockout,
    Login,
    Logout,
    dispatch_auth_event,
)
from swiftpy.auth.exceptions import (
    AuthenticationError,
    AuthenticationServiceUnavailableError,
    JWTError,
    TooManyRequestsError,
)
from swiftpy.auth.jwt_guard import JWTGuard
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.auth.throttle import LoginThrottle
from swiftpy.auth.token_blacklist import TokenBlacklist, is_token_blacklist_enabled
from swiftpy.auth.user_context import (
    CONTEXT_AUTH_GUARD_KEY,
    CONTEXT_AUTH_REMEMBER_TOKEN_KEY,
    CONTEXT_AUTH_TOKENS_KEY,
    CONTEXT_AUTH_VIA_REMEMBER_KEY,
    CONTEXT_LOGGED_OUT_KEY,
    CONTEXT_USER_KEY,
    CONTEXT_USER_MODEL_KEY,
    SENSITIVE_USER_KEYS,
    _expects_int_pk,
    extract_remember_cookie_from_request,
    format_forget_remember_cookie,
    format_remember_set_cookie,
    is_sensitive_key,
    queue_auth_cookie,
    resolve_user_model_class,
)
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container, get_container
from swiftpy.core.context import Context
from swiftpy.database.identifiers import validate_identifier
from swiftpy.database.orm.model import Model, ModelQueryBuilder
from swiftpy.database.pool import DatabasePool
from swiftpy.http.request import Request
from swiftpy.schemas.base import BaseSchema
from swiftpy.support.hashing import Hash

if TYPE_CHECKING:
    from swiftpy.validation.form_request import FormRequest

logger = logging.getLogger("swiftpy.auth.manager")

_CONTEXT_USER_KEY = CONTEXT_USER_KEY
_CONTEXT_USER_MODEL_KEY = CONTEXT_USER_MODEL_KEY
_CONTEXT_AUTH_GUARD_KEY = CONTEXT_AUTH_GUARD_KEY

_SENSITIVE_KEYS: frozenset[str] = SENSITIVE_USER_KEYS
_DUMMY_BCRYPT_HASH = (
    "$2b$12$Tu7.0HFP3JgZtjcyhyh./uyjQ9LLnxaFCE1QjeBapJm7sD/cAYdl6"
)
_THROTTLE_PRIORITY_KEYS: tuple[str, ...] = ("email", "username", "login", "phone")


class Callback:
    """
    Hashable wrapper allowing closures to be supplied inside credential dictionaries
    (as either key or value) or passed to :meth:`Auth.attempt_when`.
    """

    def __init__(self, fn: Callable[..., Any]) -> None:
        if not callable(fn):
            raise TypeError("Callback requires a callable function or lambda.")
        self.fn = fn

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.fn(*args, **kwargs)


def _is_bool_field_hint(hint: Any) -> bool:
    if hint is bool:
        return True
    if isinstance(hint, str):
        cleaned = hint.replace("Optional[", "").rstrip("]").strip()
        parts = {p.strip() for p in cleaned.split("|")}
        return "bool" in parts and parts <= {"bool", "None", "NoneType"}
    origin = get_origin(hint)
    if origin is Annotated:
        args = get_args(hint)
        return _is_bool_field_hint(args[0]) if args else False
    if origin in (Union, types.UnionType):
        non_none = [a for a in get_args(hint) if a is not type(None)]
        return len(non_none) == 1 and _is_bool_field_hint(non_none[0])
    return False


def _coerce_credential_value(user_cls: type[Model], column: str, value: Any) -> Any:
    fields = getattr(user_cls, "__fields__", {})
    hint = fields.get(column)
    if (
        _is_bool_field_hint(hint)
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value in (0, 1)
    ):
        return bool(value)
    return value


def _coerce_credentials_mapping(
    credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
) -> Mapping[Any, Any]:
    if isinstance(credentials, BaseSchema):
        fields_set = credentials.fields_set
        out: dict[str, Any] = {}
        for fname in credentials.__class__.__schema_field_order__:
            if fname in fields_set:
                out[fname] = getattr(credentials, fname)
        extra_fields = getattr(credentials, "__extra_fields__", None)
        if isinstance(extra_fields, Mapping):
            out.update(extra_fields)
        return out
    if getattr(type(credentials), "_is_swiftpy_form_request", False) is True:
        raw_form_data = getattr(credentials, "data", None)
        if isinstance(raw_form_data, Mapping):
            return dict(raw_form_data)
    if not isinstance(credentials, Mapping):
        raise TypeError("credentials must be a mapping, BaseSchema, or FormRequest.")
    return credentials


def _partition_credentials(
    credentials: Mapping[Any, Any],
    user_cls: type[Model],
) -> tuple[list[tuple[str, Any]], list[Callable[..., Any]], str | None, bool]:
    column_filters: list[tuple[str, Any]] = []
    closures: list[Callable[..., Any]] = []
    raw_password: str | None = None
    has_password_key = False

    for key, val in credentials.items():
        if isinstance(key, Callback) or callable(key):
            closures.append(key)
            if isinstance(val, Callback) or callable(val):
                closures.append(val)
            continue

        if not isinstance(key, str):
            raise TypeError(
                f"Credential key must be a column name string or Callback/callable, got {type(key).__name__}."
            )

        if key == "password":
            has_password_key = True
            raw_password = val if isinstance(val, str) else None
            continue

        if isinstance(val, Callback) or callable(val):
            closures.append(val)
            continue

        safe_col = validate_identifier(key, allow_dotted=True, allow_wildcard=False)
        column_filters.append((safe_col, _coerce_credential_value(user_cls, safe_col, val)))

    return column_filters, closures, raw_password, has_password_key


def _sanitize_credentials_for_event(credentials: Mapping[Any, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, val in credentials.items():
        if isinstance(key, Callback) or callable(key):
            continue
        if not isinstance(key, str):
            continue
        if is_sensitive_key(key):
            continue
        if isinstance(val, Callback) or callable(val):
            sanitized[key] = "<callback>"
        else:
            sanitized[key] = val
    return sanitized


def _extract_throttle_identifier(credentials: Mapping[Any, Any]) -> str:
    for preferred in _THROTTLE_PRIORITY_KEYS:
        if preferred in credentials:
            raw = credentials[preferred]
            if not callable(raw) and raw is not None:
                return str(raw)
    string_keys = sorted(
        k
        for k, v in credentials.items()
        if isinstance(k, str)
        and k != "password"
        and not callable(v)
        and v is not None
    )
    if string_keys:
        return str(credentials[string_keys[0]])
    return ""


def _extract_client_ip(
    request: Request | None = None,
    explicit_ip: str | None = None,
) -> str:
    if explicit_ip is not None and explicit_ip.strip():
        return explicit_ip.strip()
    req = request if request is not None else Context.get("request")
    if isinstance(req, Request):
        resolved = req.ip()
        if resolved is not None:
            return resolved
    return "unknown"


def _resolve_active_container_and_config() -> tuple[Container | None, Config | None]:
    container: Container | None = None
    cfg: Config | None = None
    try:
        container = get_container()
        if container.has(Config):
            cfg = container.resolve(Config)
    except RuntimeError:
        container = None
    if cfg is None:
        try:
            cfg = Config.get_instance()
        except (ConfigurationError, RuntimeError):
            cfg = None
    return container, cfg


def _has_configured_jwt_credentials(
    container: Container | None,
    cfg: Config | None,
) -> bool:
    if container is not None and container.has(JWTIssuer):
        return True
    if cfg is not None:
        for key in (
            "auth.guards.jwt.secret",
            "auth.jwt.secret",
            "jwt_secret",
            "auth.guards.jwt.private_key",
            "auth.jwt.private_key",
        ):
            val = cfg.get(key)
            if isinstance(val, str) and val.strip():
                return True
    env_secret = os.environ.get("JWT_SECRET")
    return bool(env_secret and env_secret.strip())


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
        val = self.get("id")
        if val is not None:
            return val
        if self._model is not None:
            pk_attr = getattr(self._model, "__primary_key__", "id")
            pk_val = self.get(pk_attr)
            if pk_val is not None:
                return pk_val
            model_pk = getattr(self._model, pk_attr, None)
            if model_pk is not None:
                return model_pk
        return self.get("sub")

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
        if name in self and (
            self[name] is not None
            or self._model is None
            or not hasattr(self._model, name)
        ):
            return self[name]
        if self._model is not None and hasattr(self._model, name):
            hidden = set(getattr(self._model, "__hidden__", ()) or ())
            if name not in hidden:
                return getattr(self._model, name)
        if name in self:
            return self[name]
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self[name] = value

    def __repr__(self) -> str:
        return f"AuthUser({super().__repr__()})"


def _is_logged_out() -> bool:
    """Return True if the current task Context or active Request state has been logged out."""
    if Context.get(CONTEXT_LOGGED_OUT_KEY):
        return True
    req = Context.get("request")
    return bool(
        isinstance(req, Request)
        and isinstance(req.state, dict)
        and req.state.get(CONTEXT_LOGGED_OUT_KEY)
    )


def _adopt_request_state_user() -> Any:
    """
    Adopt a user already stored in `request.state` or `Context['auth.user']` (for example
    by a guard or middleware that ran in a different task) into `Context`. Never decodes
    credentials: authentication stays with guards (`JWTGuard`, `require_jwt`), which also
    enforce token revocation.
    """
    if _is_logged_out():
        return None

    req = Context.get("request")

    existing = Context.get(_CONTEXT_USER_KEY)
    if existing is not None:
        return existing

    auth_user = Context.get("auth.user")
    if auth_user is not None:
        Context.set(_CONTEXT_USER_KEY, auth_user)
        return auth_user

    if not isinstance(req, Request) or not isinstance(req.state, dict):
        return None
    state_user = req.state.get("user")
    if state_user is None:
        return None
    Context.set(_CONTEXT_USER_KEY, state_user)
    Context.set("auth.user", state_user)
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
        pk_name = str(getattr(user_cls, "__primary_key__", "id"))

        for field_name, field_type in user_cls.__fields__.items():
            if field_name in hidden_fields or is_sensitive_key(field_name):
                continue
            if field_name in raw:
                val = raw[field_name]
                if field_type in (int, "int") or (
                    field_name == pk_name
                    and _expects_int_pk(field_type)
                    and isinstance(val, str)
                    and val.isdigit()
                ):
                    try:
                        val = int(val)
                    except (ValueError, TypeError):
                        pass
                data[field_name] = val
            elif field_name == pk_name and "sub" in raw:
                val = raw["sub"]
                if field_type in (int, "int") or (
                    _expects_int_pk(field_type)
                    and isinstance(val, str)
                    and val.isdigit()
                ):
                    try:
                        val = int(val)
                    except (ValueError, TypeError):
                        pass
                data[pk_name] = val
            elif field_name in factories:
                data[field_name] = factories[field_name]()
            elif field_name in defaults:
                data[field_name] = copy.deepcopy(defaults[field_name])
            else:
                data[field_name] = None

        for k, v in raw.items():
            if (
                k not in hidden_fields
                and not is_sensitive_key(k)
                and k not in data
            ):
                data[k] = v

        return AuthUser(data, model=None)

    clean = {
        k: v
        for k, v in raw.items()
        if not is_sensitive_key(k)
    }
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
    data: dict[str, Any]
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
        if f not in hidden_fields and not is_sensitive_key(str(f)) and f not in data:
            data[f] = getattr(model, f, None)

    ctx_payload = Context.get(_CONTEXT_USER_KEY)
    if isinstance(ctx_payload, dict):
        for k, v in ctx_payload.items():
            k_str = str(k)
            if (
                k_str not in hidden_fields
                and not is_sensitive_key(k_str)
                and k_str not in data
            ):
                data[k_str] = v

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
        Context.set("auth.user", user_dict)
    elif isinstance(model, dict):
        user_dict = model
        Context.set(_CONTEXT_USER_KEY, user_dict)
        Context.set("auth.user", user_dict)
    else:
        Context.set("auth.user", model)

    req = Context.get("request")
    if isinstance(req, Request) and isinstance(req.state, dict):
        req.state.pop(CONTEXT_LOGGED_OUT_KEY, None)
        req.state["user"] = user_dict if user_dict is not None else model
        if guard is not None:
            req.state["auth_guard"] = guard


def current_user_model() -> Any | None:
    """
    Retrieve the authenticated ORM Model instance for the current request task.
    Returns None if no model has been stored via set_user_model().
    """
    if _is_logged_out():
        return None
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

    def _resolve_user(self) -> AuthUser | None:
        if _is_logged_out():
            return None
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
            if not any(not is_sensitive_key(str(k)) for k in raw_user):
                return None
            built = _build_auth_user_from_dict(raw_user)
            return built if built.is_authenticated else None

        return None

    @overload
    def user(self, *, nullable: Literal[False] = False) -> AuthUser: ...

    @overload
    def user(self, *, nullable: Literal[True]) -> AuthUser | None: ...

    def user(self, *, nullable: bool = False) -> AuthUser | None:
        """
        Return the authenticated user for the current request task.

        Returns an `AuthUser` containing all declared fields of the User model
        with all `__hidden__` fields excluded. When unauthenticated (or when
        the active guard does not match the requested guard), raises
        `AuthenticationError` unless `nullable=True` is passed (which returns
        `None`). Equivalent strict access is also available via `user_or_fail()`.
        """
        resolved = self._resolve_user()
        if resolved is None and not nullable:
            raise AuthenticationError("Unauthenticated.")
        return resolved

    def user_or_fail(self) -> AuthUser:
        """
        Return the authenticated user for the current request task, raising
        `AuthenticationError` if unauthenticated.
        """
        resolved = self._resolve_user()
        if resolved is None:
            raise AuthenticationError("Unauthenticated.")
        return resolved

    def check(self) -> bool:
        """Return True if the current request task has an authenticated user."""
        return self._resolve_user() is not None

    def guest(self) -> bool:
        """Return True if the current request task is unauthenticated."""
        return not self.check()

    def id(self) -> Any | None:
        """Return the primary key identifier of the authenticated user, or None."""
        u = self._resolve_user()
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
        Context.set("auth.user", None)
        Context.set(_CONTEXT_USER_MODEL_KEY, None)
        Context.set(_CONTEXT_AUTH_GUARD_KEY, None)
        Context.set(CONTEXT_AUTH_VIA_REMEMBER_KEY, False)
        Context.set(CONTEXT_AUTH_REMEMBER_TOKEN_KEY, None)
        Context.set(CONTEXT_AUTH_TOKENS_KEY, None)
        req = request if isinstance(request, Request) else Context.get("request")
        if isinstance(req, Request) and hasattr(req, "state"):
            req.state.pop("user", None)
            req.state.pop("auth_guard", None)
            req.state.pop(CONTEXT_AUTH_VIA_REMEMBER_KEY, None)
            req.state[CONTEXT_LOGGED_OUT_KEY] = True

    def _guard_name(self) -> str:
        if self._guard is not None:
            return self._guard
        _, cfg = _resolve_active_container_and_config()
        if cfg is not None:
            configured = cfg.get("auth.defaults.guard") or cfg.get("auth.default")
            if isinstance(configured, str) and configured.strip():
                return configured.strip()
        return "jwt"

    @staticmethod
    def _resolve_throttle() -> LoginThrottle:
        return LoginThrottle()

    @staticmethod
    def _resolve_user_model_class() -> type[Model]:
        user_cls = resolve_user_model_class()
        if user_cls is None:
            raise ConfigurationError(
                "Unable to resolve application User model. Configure 'auth.model' or define app.models.user.User."
            )
        return user_cls

    def via_remember(self) -> bool:
        """Return True if the active request was authenticated using a remember-me cookie."""
        if bool(Context.get(CONTEXT_AUTH_VIA_REMEMBER_KEY)):
            return True
        req = Context.get("request")
        if isinstance(req, Request) and hasattr(req, "state"):
            return bool(req.state.get(CONTEXT_AUTH_VIA_REMEMBER_KEY))
        return False

    viaRemember = via_remember

    def tokens(self) -> dict[str, Any] | None:
        """Return the JWT access/refresh token pair issued during login, if any."""
        val = Context.get(CONTEXT_AUTH_TOKENS_KEY)
        return dict(val) if isinstance(val, dict) else None

    def access_token(self) -> str | None:
        """Return the active access token issued during login, if any."""
        pair = self.tokens()
        if pair is None:
            return None
        tok = pair.get("access_token")
        return tok if isinstance(tok, str) else None

    def refresh_token(self) -> str | None:
        """Return the active refresh token issued during login, if any."""
        pair = self.tokens()
        if pair is None:
            return None
        tok = pair.get("refresh_token")
        return tok if isinstance(tok, str) else None

    def remember_token(self) -> str | None:
        """Return the remember-me token active for the current session, if any."""
        val = Context.get(CONTEXT_AUTH_REMEMBER_TOKEN_KEY)
        if isinstance(val, str) and val:
            return val
        model_inst = current_user_model()
        if model_inst is not None:
            tok = getattr(model_inst, "remember_token", None)
            if isinstance(tok, str) and tok:
                return tok
        return None

    async def too_many_attempts(self, identifier: str, ip: str | None = None) -> bool:
        """Return True if login attempts for `identifier` + `ip` are currently throttled."""
        resolved_ip = _extract_client_ip(explicit_ip=ip)
        return await self._resolve_throttle().too_many_attempts(identifier, resolved_ip)

    async def available_in(self, identifier: str, ip: str | None = None) -> int:
        """Return remaining lockout seconds for `identifier` + `ip`."""
        resolved_ip = _extract_client_ip(explicit_ip=ip)
        return await self._resolve_throttle().available_in(identifier, resolved_ip)

    async def clear_throttle(self, identifier: str, ip: str | None = None) -> None:
        """Clear recorded failed login attempts and lockout for `identifier` + `ip`."""
        resolved_ip = _extract_client_ip(explicit_ip=ip)
        await self._resolve_throttle().clear(identifier, resolved_ip)

    async def _verify_credentials_candidate(
        self,
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
    ) -> tuple[bool, Model | None]:
        creds_map = _coerce_credentials_mapping(credentials)
        if not creds_map:
            return False, None

        user_cls = self._resolve_user_model_class()
        column_filters, closures, raw_password, has_password_key = _partition_credentials(
            creds_map, user_cls
        )
        if not has_password_key:
            await Hash.check_async("", _DUMMY_BCRYPT_HASH)
            return False, None

        query = user_cls.query()
        for col, val in column_filters:
            query = query.where(col, val)

        for closure in closures:
            closure_res = closure(query)
            if inspect.isawaitable(closure_res):
                closure_res = await closure_res
            if isinstance(closure_res, ModelQueryBuilder):
                query = closure_res

        candidate = await query.first()
        if candidate is None or raw_password is None:
            await Hash.check_async(raw_password or "", _DUMMY_BCRYPT_HASH)
            return False, candidate

        stored_hash = getattr(candidate, "password", None) or getattr(
            candidate, "password_hash", None
        )
        if not isinstance(stored_hash, str) or not stored_hash:
            await Hash.check_async(raw_password, _DUMMY_BCRYPT_HASH)
            return False, candidate

        matched = await Hash.check_async(raw_password, stored_hash)
        return matched, candidate

    async def _attempt_internal(
        self,
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
        callback: Callable[[Any], Any] | None = None,
        remember: bool = False,
        *,
        ip: str | None = None,
    ) -> bool:
        creds_map = _coerce_credentials_mapping(credentials)
        if callback is not None and not callable(callback):
            raise TypeError("attempt_when callback must be callable.")

        guard_name = self._guard_name()
        identifier = _extract_throttle_identifier(creds_map)
        client_ip = _extract_client_ip(explicit_ip=ip)
        throttle = self._resolve_throttle()

        if throttle.enabled and await throttle.too_many_attempts(identifier, client_ip):
            retry_after = await throttle.available_in(identifier, client_ip)
            await dispatch_auth_event(
                Lockout(
                    identifier=identifier,
                    ip=client_ip,
                    max_attempts=throttle.max_attempts,
                    retry_after=retry_after,
                    guard=guard_name,
                )
            )
            raise TooManyRequestsError(
                f"Too many login attempts. Please try again in {retry_after} seconds.",
                retry_after=retry_after,
            )

        safe_creds = _sanitize_credentials_for_event(creds_map)
        await dispatch_auth_event(
            Attempting(guard=guard_name, credentials=safe_creds, remember=remember)
        )

        matched, candidate = await self._verify_credentials_candidate(creds_map)
        if not matched or candidate is None:
            if throttle.enabled:
                await throttle.hit(identifier, client_ip)
            await dispatch_auth_event(
                Failed(guard=guard_name, user=candidate, credentials=safe_creds)
            )
            return False

        if callback is not None:
            verdict = callback(candidate)
            if inspect.isawaitable(verdict):
                verdict = await verdict
            if not bool(verdict):
                if throttle.enabled:
                    await throttle.hit(identifier, client_ip)
                await dispatch_auth_event(
                    Failed(guard=guard_name, user=candidate, credentials=safe_creds)
                )
                return False

        if throttle.enabled:
            await throttle.clear(identifier, client_ip)

        await self.login(candidate, remember=remember)
        return True

    async def attempt(
        self,
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
        remember: bool = False,
        *,
        ip: str | None = None,
    ) -> bool:
        """
        Verify credentials against the User model, start an authenticated session,
        issue tokens when JWT is configured, and enforce default login throttling.
        """
        return await self._attempt_internal(
            credentials,
            callback=None,
            remember=remember,
            ip=ip,
        )

    async def attempt_when(
        self,
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
        callback: Callable[[Any], Any],
        remember: bool = False,
        *,
        ip: str | None = None,
    ) -> bool:
        """
        Verify credentials and inspect the resolved User via ``callback`` before
        completing authentication.
        """
        return await self._attempt_internal(
            credentials,
            callback=callback,
            remember=remember,
            ip=ip,
        )

    attemptWhen = attempt_when

    async def login(self, user: Any, remember: bool = False) -> Any:
        """Log in an existing User model or mapping and optionally set a remember-me cookie."""
        if user is None:
            raise ValueError("Cannot log in a None user.")

        guard_name = self._guard_name()
        container, cfg = _resolve_active_container_and_config()
        raw_req = Context.get("request")
        req = raw_req if isinstance(raw_req, Request) else None

        set_user_model(user, guard=guard_name)
        Context.set(CONTEXT_AUTH_VIA_REMEMBER_KEY, False)
        if isinstance(req, Request) and hasattr(req, "state"):
            req.state[CONTEXT_AUTH_VIA_REMEMBER_KEY] = False

        pk_attr = getattr(user, "__primary_key__", "id")
        if isinstance(user, Model):
            user_id = getattr(user, pk_attr, None)
        elif isinstance(user, Mapping):
            user_id = user.get(pk_attr) if pk_attr in user else user.get("sub")
        else:
            user_id = getattr(user, pk_attr, None)

        if remember:
            if user_id is None:
                raise ValueError("Cannot remember a user without a primary key.")
            token = secrets.token_urlsafe(45)[:60]
            if isinstance(user, Model):
                fields = getattr(user.__class__, "__fields__", {})
                if "remember_token" not in fields:
                    raise ConfigurationError(
                        f"Model {user.__class__.__name__} must declare a 'remember_token' field to use remember=True."
                    )
                await user.__class__.where(pk_attr, user_id).update(
                    {"remember_token": token}
                )
                user.remember_token = token
                if isinstance(getattr(user, "_original", None), dict):
                    user._original["remember_token"] = token
                if hasattr(user, "_modified_keys"):
                    user._modified_keys.discard("remember_token")
            elif isinstance(user, dict):
                user["remember_token"] = token

            Context.set(CONTEXT_AUTH_REMEMBER_TOKEN_KEY, token)
            cookie_name, cookie_hdr = format_remember_set_cookie(
                user_id,
                token,
                config=cfg,
                request=req,
            )
            queue_auth_cookie(cookie_hdr, cookie_name=cookie_name, request=req)
        else:
            Context.set(CONTEXT_AUTH_REMEMBER_TOKEN_KEY, None)

        Context.set(CONTEXT_AUTH_TOKENS_KEY, None)
        if user_id is not None and _has_configured_jwt_credentials(container, cfg):
            issuer: JWTIssuer | None = None
            if container is not None and container.has(JWTIssuer):
                issuer = container.resolve(JWTIssuer)
            else:
                issuer = JWTIssuer.from_config(cfg)
            pair = issuer.issue_token_pair(user, user=user)
            Context.set(CONTEXT_AUTH_TOKENS_KEY, pair)
            Context.set(_CONTEXT_AUTH_GUARD_KEY, guard_name)
            if isinstance(req, Request) and hasattr(req, "state"):
                req.state["auth_guard"] = guard_name

        await dispatch_auth_event(Authenticated(guard=guard_name, user=user))
        await dispatch_auth_event(
            Login(guard=guard_name, user=user, remember=remember)
        )
        return user

    async def login_using_id(self, user_id: Any, remember: bool = False) -> Any:
        """Authenticate a user by primary key and return the User model (or False)."""
        user_cls = self._resolve_user_model_class()
        pk_attr = getattr(user_cls, "__primary_key__", "id")
        pk_type = getattr(user_cls, "__fields__", {}).get(pk_attr)
        lookup: Any = (
            int(user_id)
            if isinstance(user_id, str) and user_id.isdigit() and _expects_int_pk(pk_type)
            else user_id
        )
        user = await user_cls.find(lookup)
        if user is None:
            return False
        return await self.login(user, remember=remember)

    loginUsingId = login_using_id
    login_sing_id = login_using_id

    async def once(
        self,
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
    ) -> bool:
        """
        Authenticate a user for the current request only without cookies,
        sessions, remember tokens, JWT persistence, or dispatching Login.
        """
        guard_name = self._guard_name()
        is_supported = (
            isinstance(credentials, (Mapping, BaseSchema))
            or getattr(type(credentials), "_is_swiftpy_form_request", False) is True
        )
        creds_map = (
            _coerce_credentials_mapping(credentials)
            if is_supported
            else None
        )
        safe_creds = (
            _sanitize_credentials_for_event(creds_map)
            if creds_map is not None
            else {}
        )
        matched, candidate = await self._verify_credentials_candidate(
            creds_map if creds_map is not None else credentials
        )
        if not matched or candidate is None:
            await dispatch_auth_event(
                Failed(guard=guard_name, user=candidate, credentials=safe_creds)
            )
            return False

        set_user_model(candidate, guard=guard_name)
        Context.set(CONTEXT_AUTH_VIA_REMEMBER_KEY, False)
        Context.set(CONTEXT_AUTH_REMEMBER_TOKEN_KEY, None)
        Context.set(CONTEXT_AUTH_TOKENS_KEY, None)
        await dispatch_auth_event(Authenticated(guard=guard_name, user=candidate))
        return True

    async def once_using_id(self, user_id: Any) -> bool:
        """Authenticate a user by ID for the current request only."""
        user_cls = self._resolve_user_model_class()
        pk_attr = getattr(user_cls, "__primary_key__", "id")
        pk_type = getattr(user_cls, "__fields__", {}).get(pk_attr)
        lookup: Any = (
            int(user_id)
            if isinstance(user_id, str) and user_id.isdigit() and _expects_int_pk(pk_type)
            else user_id
        )
        user = await user_cls.find(lookup)
        if user is None:
            return False

        guard_name = self._guard_name()
        set_user_model(user, guard=guard_name)
        Context.set(CONTEXT_AUTH_VIA_REMEMBER_KEY, False)
        Context.set(CONTEXT_AUTH_REMEMBER_TOKEN_KEY, None)
        Context.set(CONTEXT_AUTH_TOKENS_KEY, None)
        await dispatch_auth_event(Authenticated(guard=guard_name, user=user))
        return True

    onceUsingId = once_using_id

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
        Revoke the current session's JWT token(s) in `TokenBlacklist`, invalidate
        any active remember-me token, and clear request auth context.
        """
        raw_req = request if request is not None else Context.get("request")
        req = raw_req if isinstance(raw_req, Request) else None
        guard_name = self._guard_name()
        logged_out_model = current_user_model()
        logged_out_user = (
            logged_out_model
            if logged_out_model is not None
            else Context.get(_CONTEXT_USER_KEY)
        )
        was_via_remember = self.via_remember()
        had_remember_ctx = was_via_remember or bool(
            Context.get(CONTEXT_AUTH_REMEMBER_TOKEN_KEY)
        )
        has_remember_cookie = extract_remember_cookie_from_request(req) is not None

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

        await self._invalidate_remember_state(logged_out_model, req, was_via_remember=was_via_remember, had_remember_ctx=had_remember_ctx, has_remember_cookie=has_remember_cookie)

        if clear_context:
            self._clear_auth_context(req)

        await dispatch_auth_event(Logout(guard=guard_name, user=logged_out_user))

    async def _invalidate_remember_state(
        self,
        model_inst: Any,
        req: Request | None,
        *,
        was_via_remember: bool,
        had_remember_ctx: bool,
        has_remember_cookie: bool,
    ) -> None:
        container, cfg = _resolve_active_container_and_config()
        if (
            isinstance(model_inst, Model)
            and "remember_token" in getattr(model_inst.__class__, "__fields__", {})
            and (
                bool(getattr(model_inst, "remember_token", None))
                or was_via_remember
                or had_remember_ctx
            )
        ):
            pool = (
                container.resolve(DatabasePool)
                if container is not None and container.has(DatabasePool)
                else None
            )
            if pool is not None and pool.is_initialized:
                pk_attr = getattr(model_inst.__class__, "__primary_key__", "id")
                pk_val = getattr(model_inst, pk_attr, None)
                if pk_val is not None:
                    await model_inst.__class__.where(pk_attr, pk_val).update(
                        {"remember_token": None}
                    )
                    model_inst.remember_token = None
                    if isinstance(getattr(model_inst, "_original", None), dict):
                        model_inst._original["remember_token"] = None
                    if hasattr(model_inst, "_modified_keys"):
                        model_inst._modified_keys.discard("remember_token")

        if was_via_remember or had_remember_ctx or has_remember_cookie:
            cookie_name, forget_hdr = format_forget_remember_cookie(
                config=cfg, request=req
            )
            queue_auth_cookie(forget_hdr, cookie_name=cookie_name, request=req)

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
        raw_req = Context.get("request")
        req = raw_req if isinstance(raw_req, Request) else None
        logged_out_model = current_user_model()
        was_via_remember = self.via_remember()
        had_remember_ctx = was_via_remember or bool(
            Context.get(CONTEXT_AUTH_REMEMBER_TOKEN_KEY)
        )
        has_remember_cookie = extract_remember_cookie_from_request(req) is not None

        current_subject = self.id()
        if current_subject is None:
            raw_user = Context.get(_CONTEXT_USER_KEY)
            if isinstance(raw_user, dict):
                sub_val = raw_user.get("sub")
                current_subject = (
                    sub_val if sub_val is not None else raw_user.get("id")
                )

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

        is_current_subject = subject is None or (
            current_subject is not None and str(target_subject) == str(current_subject)
        )
        if is_current_subject:
            await self._invalidate_remember_state(
                logged_out_model,
                req,
                was_via_remember=was_via_remember,
                had_remember_ctx=had_remember_ctx,
                has_remember_cookie=has_remember_cookie,
            )

        if clear_context and is_current_subject:
            self._clear_auth_context(req)


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
        val = super().get("id")
        return val if val is not None else super().get("sub")

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
    if _is_logged_out():
        return None
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

    async def attempt(
        self,
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
        remember: bool = False,
        *,
        guard_name: str | None = None,
        ip: str | None = None,
    ) -> bool:
        """Delegate credential verification and session/token creation to AuthProxy."""
        return await AuthProxy(guard=guard_name).attempt(
            credentials, remember=remember, ip=ip
        )

    async def attempt_when(
        self,
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
        callback: Callable[[Any], Any],
        remember: bool = False,
        *,
        guard_name: str | None = None,
        ip: str | None = None,
    ) -> bool:
        """Delegate conditional authentication to AuthProxy."""
        return await AuthProxy(guard=guard_name).attempt_when(
            credentials, callback, remember=remember, ip=ip
        )

    attemptWhen = attempt_when

    async def login(
        self,
        user: Any,
        remember: bool = False,
        *,
        guard_name: str | None = None,
    ) -> Any:
        """Delegate direct user login to AuthProxy."""
        return await AuthProxy(guard=guard_name).login(user, remember=remember)

    async def login_using_id(
        self,
        user_id: Any,
        remember: bool = False,
        *,
        guard_name: str | None = None,
    ) -> Any:
        """Delegate ID-based login to AuthProxy."""
        return await AuthProxy(guard=guard_name).login_using_id(
            user_id, remember=remember
        )

    loginUsingId = login_using_id
    login_sing_id = login_using_id

    async def once(
        self,
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
        *,
        guard_name: str | None = None,
    ) -> bool:
        """Delegate stateless single-request authentication to AuthProxy."""
        return await AuthProxy(guard=guard_name).once(credentials)

    async def once_using_id(
        self,
        user_id: Any,
        *,
        guard_name: str | None = None,
    ) -> bool:
        """Delegate stateless single-request ID authentication to AuthProxy."""
        return await AuthProxy(guard=guard_name).once_using_id(user_id)

    onceUsingId = once_using_id

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


class Auth:
    """
    Static authentication facade providing Laravel-inspired ``Auth.attempt()``,
    ``Auth.attempt_when()``, ``Auth.login()``, ``Auth.login_using_id()``,
    ``Auth.once()``, and session/context inspection helpers.
    """

    @staticmethod
    def guard(name: str | None = None) -> AuthProxy:
        """Return an AuthProxy bound to ``name`` (or the default guard)."""
        return AuthProxy(guard=name)

    @overload
    @staticmethod
    def user(*, nullable: Literal[False] = False) -> AuthUser: ...

    @overload
    @staticmethod
    def user(*, nullable: Literal[True]) -> AuthUser | None: ...

    @staticmethod
    def user(*, nullable: bool = False) -> AuthUser | None:
        """
        Return the authenticated user for the current task, raising
        `AuthenticationError` if unauthenticated unless `nullable=True`.
        """
        resolved = AuthProxy()._resolve_user()
        if resolved is None and not nullable:
            raise AuthenticationError("Unauthenticated.")
        return resolved

    @staticmethod
    def user_or_fail() -> AuthUser:
        """Return the authenticated user for the current task, raising AuthenticationError if unauthenticated."""
        return AuthProxy().user_or_fail()

    @staticmethod
    def check() -> bool:
        """Return True if the current request task is authenticated."""
        return AuthProxy().check()

    @staticmethod
    def guest() -> bool:
        """Return True if the current request task is not authenticated."""
        return AuthProxy().guest()

    @staticmethod
    def id() -> Any | None:
        """Return the active authenticated user's primary key, or None."""
        return AuthProxy().id()

    @staticmethod
    def via_remember() -> bool:
        """Return True if the current user was authenticated via a remember-me cookie."""
        return AuthProxy().via_remember()

    viaRemember = via_remember

    @staticmethod
    def tokens() -> dict[str, Any] | None:
        """Return the active JWT token pair issued during login, if available."""
        return AuthProxy().tokens()

    @staticmethod
    def access_token() -> str | None:
        """Return the access token issued during login, if available."""
        return AuthProxy().access_token()

    @staticmethod
    def refresh_token() -> str | None:
        """Return the refresh token issued during login, if available."""
        return AuthProxy().refresh_token()

    @staticmethod
    def remember_token() -> str | None:
        """Return the active remember-me token for the current user, if any."""
        return AuthProxy().remember_token()

    @staticmethod
    async def attempt(
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
        remember: bool = False,
        *,
        ip: str | None = None,
    ) -> bool:
        """Verify credentials and authenticate the matching user if valid."""
        return await AuthProxy().attempt(credentials, remember=remember, ip=ip)

    @staticmethod
    async def attempt_when(
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
        callback: Callable[[Any], Any],
        remember: bool = False,
        *,
        ip: str | None = None,
    ) -> bool:
        """Verify credentials and require ``callback(user)`` to return truthy."""
        return await AuthProxy().attempt_when(
            credentials, callback, remember=remember, ip=ip
        )

    attemptWhen = attempt_when

    @staticmethod
    async def login(user: Any, remember: bool = False) -> Any:
        """Log in a resolved user model instance (optionally with remember-me)."""
        return await AuthProxy().login(user, remember=remember)

    @staticmethod
    async def login_using_id(user_id: Any, remember: bool = False) -> Any:
        """Log in a user by primary key (returning User model or False)."""
        return await AuthProxy().login_using_id(user_id, remember=remember)

    loginUsingId = login_using_id
    login_sing_id = login_using_id

    @staticmethod
    async def once(
        credentials: Mapping[Any, Any] | BaseSchema | FormRequest[Any],
    ) -> bool:
        """Authenticate a user for a single request with no cookies/Login event."""
        return await AuthProxy().once(credentials)

    @staticmethod
    async def once_using_id(user_id: Any) -> bool:
        """Authenticate a user by ID for a single request only."""
        return await AuthProxy().once_using_id(user_id)

    onceUsingId = once_using_id

    @staticmethod
    async def too_many_attempts(identifier: str, ip: str | None = None) -> bool:
        """Return True if login attempts for ``identifier`` + ``ip`` are throttled."""
        return await AuthProxy().too_many_attempts(identifier, ip=ip)

    @staticmethod
    async def available_in(identifier: str, ip: str | None = None) -> int:
        """Return remaining throttle lockout seconds for ``identifier`` + ``ip``."""
        return await AuthProxy().available_in(identifier, ip=ip)

    @staticmethod
    async def clear_throttle(identifier: str, ip: str | None = None) -> None:
        """Clear recorded throttle failures and lockouts for ``identifier`` + ``ip``."""
        await AuthProxy().clear_throttle(identifier, ip=ip)

    @staticmethod
    async def authenticate(request: Request | None = None) -> Any:
        """Authenticate the incoming request via the active guard."""
        return await AuthProxy().authenticate(request)

    @staticmethod
    async def logout(
        request: Request | None = None,
        *,
        token: str | None = None,
        refresh_token: str | None = None,
        blacklist: TokenBlacklist | None = None,
        issuer: JWTIssuer | None = None,
        clear_context: bool = True,
    ) -> None:
        """Log out the current authenticated user, revoking tokens & cookies."""
        await AuthProxy().logout(
            request=request,
            token=token,
            refresh_token=refresh_token,
            blacklist=blacklist,
            issuer=issuer,
            clear_context=clear_context,
        )

    @staticmethod
    async def logout_all(
        subject: str | int | None = None,
        *,
        blacklist: TokenBlacklist | None = None,
        ttl: int | None = None,
        clear_context: bool = True,
    ) -> None:
        """Revoke all tokens issued for ``subject`` or the current user."""
        await AuthProxy().logout_all(
            subject=subject,
            blacklist=blacklist,
            ttl=ttl,
            clear_context=clear_context,
        )


__all__ = [
    "Auth",
    "AuthManager",
    "AuthProxy",
    "AuthUser",
    "Callback",
    "CurrentUser",
    "auth",
    "current_user",
    "current_user_model",
    "resolve_user_model_class",
    "set_user_model",
]
