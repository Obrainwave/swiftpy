"""
Shared helpers that bind an authenticated user into the task-scoped `Context`.

Used by `JWTIssuer` (when an access token is issued), `require_jwt()` /
`JWTGuard` (when an access token is verified) and `AuthManager`. Keeping the
logic here avoids duplicating user-model resolution across the auth modules.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from typing import Any

import asyncpg

from swiftpy.core.config import Config
from swiftpy.core.container import get_container
from swiftpy.core.context import Context
from swiftpy.database.orm.model import Model
from swiftpy.database.orm.registry import _MODEL_REGISTRY
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import QueryError

logger = logging.getLogger("swiftpy.auth.user_context")

CONTEXT_USER_KEY = "__swiftpy_current_user__"
CONTEXT_USER_MODEL_KEY = "__swiftpy_current_user_model__"
CONTEXT_AUTH_GUARD_KEY = "__swiftpy_auth_guard__"
CONTEXT_LOGGED_OUT_KEY = "__swiftpy_logged_out__"

SENSITIVE_USER_KEYS: frozenset[str] = frozenset({
    "password",
    "password_hash",
    "key_hash",
    "secret",
    "token",
})

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
    Context.set(CONTEXT_AUTH_GUARD_KEY, "jwt")

    request = Context.get("request")
    state = getattr(request, "state", None)
    if isinstance(state, dict):
        state["user"] = user_ctx
        state["auth_guard"] = "jwt"
        state.pop(CONTEXT_LOGGED_OUT_KEY, None)

    return user_ctx


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
    lookup: Any = int(sub) if sub.isdigit() and pk_type in (int, "int", None) else sub
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


__all__ = [
    "CONTEXT_AUTH_GUARD_KEY",
    "CONTEXT_LOGGED_OUT_KEY",
    "CONTEXT_USER_KEY",
    "CONTEXT_USER_MODEL_KEY",
    "SENSITIVE_USER_KEYS",
    "TokenSubject",
    "auth_option_enabled",
    "bind_jwt_user_context",
    "current_model_for_subject",
    "extract_subject_and_user",
    "hydrate_jwt_user_context",
    "resolve_user_model_class",
]
