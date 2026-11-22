"""
Shared helpers that bind an authenticated user into the task-scoped `Context`.

Used by `JWTIssuer` (when an access token is issued), `require_jwt()` /
`JWTGuard` (when an access token is verified) and `AuthManager`. Keeping the
logic here avoids duplicating user-model resolution across the auth modules.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import importlib
import logging
import os
import secrets
import types
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin
from weakref import WeakKeyDictionary

import asyncpg

from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import get_container
from swiftpy.core.context import Context
from swiftpy.database.orm.model import Model
from swiftpy.database.orm.registry import _MODEL_REGISTRY
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import QueryError
from swiftpy.support.keys import decode_signing_key, resolve_configured_app_key

if TYPE_CHECKING:
    from swiftpy.http.request import Request

logger = logging.getLogger("swiftpy.auth.user_context")

CONTEXT_USER_KEY = "__swiftpy_current_user__"
CONTEXT_USER_MODEL_KEY = "__swiftpy_current_user_model__"
CONTEXT_AUTH_GUARD_KEY = "__swiftpy_auth_guard__"
CONTEXT_LOGGED_OUT_KEY = "__swiftpy_logged_out__"
CONTEXT_AUTH_VIA_REMEMBER_KEY = "__swiftpy_auth_via_remember__"
CONTEXT_AUTH_REMEMBER_TOKEN_KEY = "__swiftpy_auth_remember_token__"
CONTEXT_AUTH_TOKENS_KEY = "__swiftpy_auth_tokens__"
CONTEXT_AUTH_QUEUED_COOKIES_KEY = "__swiftpy_auth_queued_cookies__"

SENSITIVE_USER_KEYS: frozenset[str] = frozenset({
    "password",
    "password_hash",
    "key_hash",
    "secret",
    "token",
    "remember_token",
    "access_token",
    "refresh_token",
    "api_key",
    "private_key",
    "passphrase",
})

_SENSITIVE_KEY_SUFFIXES: tuple[str, ...] = (
    "_token",
    "_secret",
    "_password",
    "_hash",
    "_passphrase",
    "api_key",
)


def is_sensitive_key(key: str) -> bool:
    """Return True if a field/credential key carries a password, token, secret, or hash."""
    lower = key.strip().lower()
    if lower in SENSITIVE_USER_KEYS:
        return True
    if "password" in lower or "secret" in lower:
        return True
    return lower.endswith(_SENSITIVE_KEY_SUFFIXES)

_EPHEMERAL_REMEMBER_KEYS: WeakKeyDictionary[Config, bytes] = WeakKeyDictionary()
_NO_CONFIG_EPHEMERAL_KEY: list[bytes] = []

type TokenSubject = str | int | Model | Mapping[str, Any]

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def auth_option_enabled(config: Config | None, name: str) -> bool:
    """Return whether the opt-in boolean option ``auth.<name>`` is enabled (default: False)."""
    if config is None:
        return False
    value = config.get(f"auth.{name}")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return False


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


def _model_pk(model: Model) -> str:
    return str(getattr(model, getattr(model, "__primary_key__", "id"), None))


def current_model_for_subject(sub: str) -> Model | None:
    """Return the model stored via `set_user_model()` in this task if it matches `sub`."""
    existing = Context.get(CONTEXT_USER_MODEL_KEY)
    if isinstance(existing, Model) and _model_pk(existing) == sub:
        return existing
    return None


def _safe_fields(source: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): v for k, v in source.items() if k not in SENSITIVE_USER_KEYS}


def extract_subject_and_user(
    subject: TokenSubject,
    user: Model | Mapping[str, Any] | None = None,
) -> tuple[str, Model | None, dict[str, Any]]:
    """
    Resolve the JWT ``sub`` string, an optional ORM model and the safe (non-sensitive)
    user fields from a token ``subject`` and optional explicit ``user``.
    """
    user_model: Model | None = None
    fields: dict[str, Any] = {}

    if isinstance(subject, Model):
        user_model = subject
        pk_attr = getattr(subject, "__primary_key__", "id")
        pk_val = getattr(subject, pk_attr, None)
        if pk_val is None:
            raise ValueError(
                f"Cannot issue a JWT for {subject.__class__.__name__} without "
                f"a primary key '{pk_attr}'."
            )
        sub = str(pk_val)
        fields.update(_safe_fields(subject.to_dict()))
    elif isinstance(subject, Mapping):
        raw_id = subject.get("id") if subject.get("id") is not None else subject.get("sub")
        if raw_id is None:
            raise ValueError("A mapping used as JWT subject must contain an 'id' or 'sub' key.")
        sub = str(raw_id)
        fields.update(_safe_fields(subject))
    else:
        sub = str(subject)

    if isinstance(user, Model):
        if _model_pk(user) != sub:
            raise ValueError(
                f"user {user.__class__.__name__}({_model_pk(user)}) does not match JWT subject '{sub}'."
            )
        user_model = user
        for key, value in _safe_fields(user.to_dict()).items():
            fields.setdefault(key, value)
    elif isinstance(user, Mapping):
        user_sub = _context_subject(user)
        if user_sub is not None and user_sub != sub:
            raise ValueError(f"user '{user_sub}' does not match JWT subject '{sub}'.")
        for key, value in _safe_fields(user).items():
            fields.setdefault(key, value)

    if user_model is None:
        user_model = current_model_for_subject(sub)
        if user_model is not None:
            for key, value in _safe_fields(user_model.to_dict()).items():
                fields.setdefault(key, value)

    return sub, user_model, fields


def _context_subject(ctx: Mapping[str, Any]) -> str | None:
    raw = ctx.get("sub") if ctx.get("sub") is not None else ctx.get("id")
    return None if raw is None else str(raw)


def bind_jwt_user_context(
    payload: Mapping[str, Any],
    *,
    user_model: Model | None = None,
    extra_fields: Mapping[str, Any] | None = None,
    preserve_different_subject: bool = False,
) -> dict[str, Any]:
    """
    Populate task-scoped `Context` and the active `Request.state` with the user
    described by a JWT ``payload`` (plus optional model/extra fields).

    `CurrentUser` follows `Context` live, so already-injected instances update
    immediately. With ``preserve_different_subject=True`` an already-authenticated
    user with a different subject is left untouched and returned unchanged.
    """
    raw_sub = payload.get("sub")
    sub = str(raw_sub) if raw_sub is not None else ""

    existing = Context.get(CONTEXT_USER_KEY)
    existing_sub = _context_subject(existing) if isinstance(existing, dict) else None
    if (
        preserve_different_subject
        and isinstance(existing, dict)
        and existing
        and existing_sub is not None
        and existing_sub != sub
    ):
        return existing

    model = user_model if user_model is not None else current_model_for_subject(sub)

    user_ctx: dict[str, Any] = {}
    if isinstance(existing, dict) and existing_sub == sub:
        user_ctx.update(_safe_fields(existing))
    if model is not None:
        user_ctx.update(_safe_fields(model.to_dict()))
    if extra_fields:
        user_ctx.update(_safe_fields(extra_fields))
    user_ctx.update(_safe_fields(payload))

    Context.set(CONTEXT_LOGGED_OUT_KEY, False)
    if model is not None:
        Context.set(CONTEXT_USER_MODEL_KEY, model)
    else:
        previous = Context.get(CONTEXT_USER_MODEL_KEY)
        if isinstance(previous, Model) and _model_pk(previous) != sub:
            Context.set(CONTEXT_USER_MODEL_KEY, None)
    Context.set(CONTEXT_USER_KEY, user_ctx)
    Context.set("auth.user", user_ctx)
    Context.set(CONTEXT_AUTH_GUARD_KEY, "jwt")

    request = Context.get("request")
    state = getattr(request, "state", None)
    if isinstance(state, dict):
        state["user"] = user_ctx
        state["auth_guard"] = "jwt"
        state.pop(CONTEXT_LOGGED_OUT_KEY, None)

    return user_ctx


def _expects_int_pk(pk_type: Any) -> bool:
    """Return True if a primary key field hint represents an integer (including Optional[int])."""
    if pk_type is None or pk_type is int:
        return True
    if isinstance(pk_type, str):
        cleaned = pk_type.strip()
        if cleaned in {"int", "int | None", "None | int", "Optional[int]"}:
            return True
        if "|" in cleaned:
            parts = {p.strip() for p in cleaned.split("|")}
            return "int" in parts and parts <= {"int", "None", "NoneType"}
        return False
    origin = get_origin(pk_type)
    if origin is Annotated:
        args = get_args(pk_type)
        return _expects_int_pk(args[0]) if args else False
    if origin in (Union, types.UnionType):
        non_none_args = [a for a in get_args(pk_type) if a is not type(None)]
        return len(non_none_args) == 1 and _expects_int_pk(non_none_args[0])
    return False


async def _load_user_model(sub: str) -> Model | None:
    """Load the user model for ``sub`` when a DB pool is ready; degrade to None on DB errors."""
    try:
        container = get_container()
        pool = container.resolve(DatabasePool) if container.has(DatabasePool) else None
    except RuntimeError:
        return None
    if pool is None or not pool.is_initialized:
        return None

    user_cls = resolve_user_model_class()
    if user_cls is None:
        return None

    pk_name = getattr(user_cls, "__primary_key__", "id")
    pk_type = getattr(user_cls, "__fields__", {}).get(pk_name)
    lookup: Any = int(sub) if sub.isdigit() and _expects_int_pk(pk_type) else sub
    try:
        return await user_cls.find(lookup)
    except (QueryError, asyncpg.PostgresError, asyncpg.InterfaceError, OSError, RuntimeError):
        logger.warning(
            "Could not hydrate %s for JWT subject; continuing with token claims only",
            user_cls.__name__,
            exc_info=True,
        )
        return None


async def hydrate_jwt_user_context(
    payload: Mapping[str, Any],
    *,
    preserve_different_subject: bool = False,
) -> dict[str, Any]:
    """
    Bind a verified JWT ``payload`` into `Context`.

    When the opt-in ``auth.hydrate_user`` option is enabled and a database pool is
    initialized, the User model is loaded by primary key (one query) so `CurrentUser`
    and `auth().user()` expose every non-hidden model field, even for tokens that carry
    only ``sub``. Otherwise only the token claims are bound (no database access).
    """
    raw_sub = payload.get("sub")
    sub = str(raw_sub) if raw_sub is not None else ""
    model = current_model_for_subject(sub) if sub else None

    if model is None and sub:
        try:
            container = get_container()
            cfg = container.resolve(Config) if container.has(Config) else None
        except RuntimeError:
            cfg = None
        if auth_option_enabled(cfg, "hydrate_user"):
            model = await _load_user_model(sub)

    return bind_jwt_user_context(
        payload,
        user_model=model,
        preserve_different_subject=preserve_different_subject,
    )


def _resolve_active_config(config: Config | None = None) -> Config | None:
    if config is not None:
        return config
    try:
        container = get_container()
        if container.has(Config):
            return container.resolve(Config)
    except RuntimeError:
        pass
    try:
        return Config.get_instance()
    except (ConfigurationError, RuntimeError):
        return None


def _is_debug_mode(config: Config | None = None) -> bool:
    cfg = _resolve_active_config(config)
    if cfg is None:
        app_env = str(os.environ.get("APP_ENV") or "development").lower()
        return app_env not in ("production", "prod")
    app_env = str(cfg.get("app.env") or cfg.get("env") or "development").lower()
    debug_val = cfg.get("app.debug") if cfg.get("app.debug") is not None else cfg.get("debug")
    if debug_val is not None:
        return bool(debug_val)
    return app_env not in ("production", "prod")


def resolve_remember_cookie_name(config: Config | None = None) -> str:
    """Return the configured cookie name used to store remember-me tokens."""
    cfg = _resolve_active_config(config)
    if cfg is not None:
        name = cfg.get("auth.remember.cookie_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    env_name = os.environ.get("AUTH_REMEMBER_COOKIE")
    if env_name and env_name.strip():
        return env_name.strip()
    return "swiftpy_remember"


def resolve_remember_cookie_ttl(config: Config | None = None) -> int:
    """Return remember-me cookie Max-Age lifetime in seconds (default 30 days)."""
    cfg = _resolve_active_config(config)
    raw: Any = None
    if cfg is not None:
        raw = cfg.get("auth.remember.ttl")
    if raw is None:
        raw = os.environ.get("AUTH_REMEMBER_TTL")
    if raw is not None:
        try:
            ttl = int(raw)
            if ttl > 0:
                return ttl
        except (TypeError, ValueError):
            pass
    return 2592000


def resolve_remember_signing_key(config: Config | None = None) -> bytes:
    """Resolve the HMAC signing key used to authenticate remember-me cookies."""
    cfg = _resolve_active_config(config)
    raw_app_key = resolve_configured_app_key(cfg)
    if raw_app_key:
        return decode_signing_key(raw_app_key)

    raw_jwt_secret: Any = None
    if cfg is not None:
        raw_jwt_secret = (
            cfg.get("auth.guards.jwt.secret")
            or cfg.get("auth.jwt.secret")
            or cfg.get("jwt_secret")
        )
    if raw_jwt_secret is None:
        raw_jwt_secret = os.environ.get("JWT_SECRET")
    if isinstance(raw_jwt_secret, str) and raw_jwt_secret.strip():
        return decode_signing_key(raw_jwt_secret)

    if not _is_debug_mode(cfg):
        raise ConfigurationError(
            "APP_KEY or JWT_SECRET must be configured to sign remember-me cookies in production."
        )

    if cfg is not None:
        key = _EPHEMERAL_REMEMBER_KEYS.get(cfg)
        if key is None:
            key = secrets.token_bytes(32)
            _EPHEMERAL_REMEMBER_KEYS[cfg] = key
        return key

    if not _NO_CONFIG_EPHEMERAL_KEY:
        _NO_CONFIG_EPHEMERAL_KEY.append(secrets.token_bytes(32))
    return _NO_CONFIG_EPHEMERAL_KEY[0]


def _b64url_encode_str(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(b"=").decode("ascii")


def _b64url_decode_str(encoded: str) -> str | None:
    unpadded = encoded.rstrip("=")
    rem = len(unpadded) % 4
    if rem == 1:
        return None
    padded = unpadded + ("=" * (4 - rem) if rem > 0 else "")
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return raw.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def build_remember_cookie_value(
    user_id: str | int,
    remember_token: str,
    *,
    config: Config | None = None,
) -> str:
    """Create an HMAC-SHA256 signed remember-me cookie payload for ``(user_id, remember_token)``."""
    encoded_id = _b64url_encode_str(str(user_id))
    encoded_tok = _b64url_encode_str(remember_token)
    payload = f"{encoded_id}.{encoded_tok}"
    signing_key = resolve_remember_signing_key(config)
    sig = hmac.new(signing_key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_remember_cookie_value(
    raw_cookie: str,
    *,
    config: Config | None = None,
) -> tuple[str, str] | None:
    """Verify a signed remember-me cookie value and return ``(user_id_str, remember_token)``."""
    if not isinstance(raw_cookie, str) or not raw_cookie:
        return None
    parts = raw_cookie.strip().split(".")
    if len(parts) != 3:
        return None
    encoded_id, encoded_tok, provided_sig = parts
    if not encoded_id or not encoded_tok or not provided_sig:
        return None

    payload = f"{encoded_id}.{encoded_tok}"
    try:
        signing_key = resolve_remember_signing_key(config)
    except ConfigurationError:
        return None
    expected_sig = hmac.new(
        signing_key, payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        return None

    user_id_str = _b64url_decode_str(encoded_id)
    remember_token = _b64url_decode_str(encoded_tok)
    if not user_id_str or not remember_token:
        return None
    return user_id_str, remember_token


def _resolve_cookie_secure(
    *,
    config: Config | None = None,
    request: Request | None = None,
    secure: bool | None = None,
) -> bool:
    if secure is not None:
        return secure
    cfg = _resolve_active_config(config)
    if cfg is not None:
        explicit = cfg.get("auth.remember.secure")
        if isinstance(explicit, bool):
            return explicit
    req = request if request is not None else Context.get("request")
    if req is not None and getattr(req, "scheme", "http").lower() == "https":
        return True
    if cfg is not None:
        app_url = cfg.get("app.url")
        if isinstance(app_url, str) and app_url.strip().lower().startswith("https://"):
            return True
        app_env = str(cfg.get("app.env") or cfg.get("env") or "").lower()
        if app_env in ("production", "prod"):
            return True
    return False


def _resolve_cookie_same_site(config: Config | None = None) -> str:
    cfg = _resolve_active_config(config)
    raw: Any = cfg.get("auth.remember.same_site") if cfg is not None else None
    if isinstance(raw, str):
        normalized = raw.strip().capitalize()
        if normalized in {"Lax", "Strict", "None"}:
            return normalized
    return "Lax"


def _resolve_cookie_path(config: Config | None = None) -> str:
    cfg = _resolve_active_config(config)
    raw: Any = cfg.get("auth.remember.path") if cfg is not None else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "/"


def _build_remember_cookie_parts(
    name: str,
    value: str,
    *,
    max_age: int,
    expires: str | None = None,
    config: Config | None = None,
    request: Request | None = None,
    secure: bool | None = None,
) -> str:
    same_site = _resolve_cookie_same_site(config)
    is_secure = _resolve_cookie_secure(config=config, request=request, secure=secure)
    if same_site == "None":
        is_secure = True
    path = _resolve_cookie_path(config)
    parts = [
        f"{name}={value}",
        f"Path={path}",
        f"Max-Age={max_age}",
    ]
    if expires is not None:
        parts.append(f"Expires={expires}")
    parts.extend(["HttpOnly", f"SameSite={same_site}"])
    if is_secure:
        parts.append("Secure")
    return "; ".join(parts)


def format_remember_set_cookie(
    user_id: str | int,
    remember_token: str,
    *,
    config: Config | None = None,
    request: Request | None = None,
    secure: bool | None = None,
) -> tuple[str, str]:
    """Build `(cookie_name, Set-Cookie header value)` for a remember-me cookie."""
    name = resolve_remember_cookie_name(config)
    ttl = resolve_remember_cookie_ttl(config)
    value = build_remember_cookie_value(user_id, remember_token, config=config)
    header = _build_remember_cookie_parts(
        name,
        value,
        max_age=ttl,
        config=config,
        request=request,
        secure=secure,
    )
    return name, header


def format_forget_remember_cookie(
    *,
    config: Config | None = None,
    request: Request | None = None,
    secure: bool | None = None,
) -> tuple[str, str]:
    """Build `(cookie_name, Set-Cookie header value)` expiring the remember-me cookie."""
    name = resolve_remember_cookie_name(config)
    header = _build_remember_cookie_parts(
        name,
        "",
        max_age=0,
        expires="Thu, 01 Jan 1970 00:00:00 GMT",
        config=config,
        request=request,
        secure=secure,
    )
    return name, header


def queue_auth_cookie(
    cookie_header: str,
    *,
    cookie_name: str | None = None,
    request: Request | None = None,
) -> None:
    """Queue a ``Set-Cookie`` header onto the active task Context and Request state."""
    prefix = f"{cookie_name}=" if cookie_name else None
    existing = Context.get(CONTEXT_AUTH_QUEUED_COOKIES_KEY)
    cookies = list(existing) if isinstance(existing, list) else []
    if prefix is not None:
        cookies = [c for c in cookies if not str(c).startswith(prefix)]
    cookies.append(cookie_header)
    Context.set(CONTEXT_AUTH_QUEUED_COOKIES_KEY, cookies)

    req = request if request is not None else Context.get("request")
    state = getattr(req, "state", None)
    if isinstance(state, dict):
        state[CONTEXT_AUTH_QUEUED_COOKIES_KEY] = list(cookies)


def pop_queued_auth_cookies(request: Request | None = None) -> list[str]:
    """Retrieve queued ``Set-Cookie`` headers from Context or Request state and clear them."""
    ctx_cookies = Context.get(CONTEXT_AUTH_QUEUED_COOKIES_KEY)
    cookies: list[str] = list(ctx_cookies) if isinstance(ctx_cookies, list) else []
    Context.set(CONTEXT_AUTH_QUEUED_COOKIES_KEY, [])

    state = getattr(request, "state", None)
    if isinstance(state, dict):
        state_cookies = state.pop(CONTEXT_AUTH_QUEUED_COOKIES_KEY, None)
        if not cookies and isinstance(state_cookies, list):
            cookies = list(state_cookies)
    return cookies


def extract_remember_cookie_from_request(
    request: Request | None,
    *,
    config: Config | None = None,
) -> str | None:
    """Extract raw signed remember-me token from request cookies or ``X-Remember-Token``."""
    if request is None:
        return None
    cookie_name = resolve_remember_cookie_name(config)
    cookie_fn = getattr(request, "cookie", None)
    if callable(cookie_fn):
        cookie_val = cookie_fn(cookie_name)
        if isinstance(cookie_val, str) and cookie_val:
            return cookie_val
    headers = getattr(request, "headers", None)
    if headers is not None and hasattr(headers, "get"):
        header_val = headers.get("x-remember-token")
        if isinstance(header_val, str) and header_val.strip():
            return header_val.strip()
    return None


async def authenticate_remember_cookie(
    request: Request,
    *,
    config: Config | None = None,
    guard: str = "jwt",
) -> Model | None:
    """
    Validate a request's signed remember-me cookie against the database user record
    and hydrate authenticated task context if valid.
    """
    if Context.get(CONTEXT_LOGGED_OUT_KEY):
        return None
    req_state = getattr(request, "state", None)
    if isinstance(req_state, dict) and req_state.get(CONTEXT_LOGGED_OUT_KEY):
        return None

    raw_cookie = extract_remember_cookie_from_request(request, config=config)
    if not raw_cookie:
        return None

    verified = verify_remember_cookie_value(raw_cookie, config=config)
    if verified is None:
        return None
    user_id_str, raw_token = verified

    user = await _load_user_model(user_id_str)
    if user is None:
        return None

    stored_token = getattr(user, "remember_token", None)
    if not isinstance(stored_token, str) or not stored_token:
        return None
    if not hmac.compare_digest(stored_token, raw_token):
        return None

    safe_dict = (
        _safe_fields(user.to_dict())
        if hasattr(user, "to_dict") and callable(user.to_dict)
        else {}
    )
    Context.set(CONTEXT_LOGGED_OUT_KEY, False)
    Context.set(CONTEXT_AUTH_VIA_REMEMBER_KEY, True)
    Context.set(CONTEXT_AUTH_REMEMBER_TOKEN_KEY, stored_token)
    Context.set(CONTEXT_USER_MODEL_KEY, user)
    Context.set(CONTEXT_USER_KEY, safe_dict)
    Context.set("auth.user", safe_dict)
    Context.set(CONTEXT_AUTH_GUARD_KEY, guard)

    if isinstance(req_state, dict):
        req_state["user"] = safe_dict
        req_state["auth_guard"] = guard
        req_state[CONTEXT_AUTH_VIA_REMEMBER_KEY] = True
        req_state.pop(CONTEXT_LOGGED_OUT_KEY, None)

    return user


__all__ = [
    "CONTEXT_AUTH_GUARD_KEY",
    "CONTEXT_AUTH_QUEUED_COOKIES_KEY",
    "CONTEXT_AUTH_REMEMBER_TOKEN_KEY",
    "CONTEXT_AUTH_TOKENS_KEY",
    "CONTEXT_AUTH_VIA_REMEMBER_KEY",
    "CONTEXT_LOGGED_OUT_KEY",
    "CONTEXT_USER_KEY",
    "CONTEXT_USER_MODEL_KEY",
    "SENSITIVE_USER_KEYS",
    "TokenSubject",
    "auth_option_enabled",
    "authenticate_remember_cookie",
    "bind_jwt_user_context",
    "build_remember_cookie_value",
    "current_model_for_subject",
    "extract_remember_cookie_from_request",
    "extract_subject_and_user",
    "format_forget_remember_cookie",
    "format_remember_set_cookie",
    "hydrate_jwt_user_context",
    "is_sensitive_key",
    "pop_queued_auth_cookies",
    "queue_auth_cookie",
    "resolve_remember_cookie_name",
    "resolve_remember_cookie_ttl",
    "resolve_remember_signing_key",
    "resolve_user_model_class",
    "verify_remember_cookie_value",
]
