from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import bcrypt

from swiftpy.auth.exceptions import (
    AuthenticationError,
    AuthenticationServiceUnavailableError,
    InvalidApiKeyError,
)
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import get_container
from swiftpy.core.context import Context
from swiftpy.database.identifiers import validate_identifier
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response, unauthorized

logger = logging.getLogger("swiftpy.auth.api_key")

_EPHEMERAL_PEPPER: bytes | None = None
_DUMMY_HASH: str = "0" * 64


def get_server_pepper(config: Config | None = None) -> bytes:
    """
    Retrieve the server pepper used for HMAC-SHA256 API key hashing (Decision D-2).

    In production (debug=False), fails fast if AUTH_KEY_PEPPER is missing.
    In debug/development mode, if missing, generates a per-process ephemeral pepper and logs a warning.
    """
    global _EPHEMERAL_PEPPER
    if config is None:
        try:
            cfg = Config.get_instance()
        except (RuntimeError, ValueError):
            cfg = Config()
    else:
        cfg = config

    raw_pepper = (
        cfg.get("auth.guards.api_key.pepper")
        or cfg.get("auth.api_key.pepper")
        or cfg.get("auth_key_pepper")
        or cfg.get("api_key_pepper")
    )
    if raw_pepper is not None and str(raw_pepper).strip():
        return str(raw_pepper).encode("utf-8")

    app_env = str(cfg.get("app.env") or cfg.get("env") or "development").lower()
    debug_val = cfg.get("app.debug") if cfg.get("app.debug") is not None else cfg.get("debug")
    if debug_val is not None:
        is_debug = bool(debug_val)
    else:
        is_debug = app_env not in ("production", "prod")

    if is_debug:
        if _EPHEMERAL_PEPPER is None:
            _EPHEMERAL_PEPPER = secrets.token_bytes(32)
            logger.warning(
                "SECURITY WARNING: No AUTH_KEY_PEPPER configured. Generated an ephemeral "
                "server pepper for debug/development. Set AUTH_KEY_PEPPER in your environment."
            )
        return _EPHEMERAL_PEPPER

    raise ConfigurationError(
        "AUTH_KEY_PEPPER is not configured. A server pepper of at least 32 bytes is required in production."
    )


def generate_api_key(prefix: str = "sk_live_") -> tuple[str, str, str]:
    """
    Generate a new API key pair adhering to the prefix.secret format (Decision D-2).

    Returns:
        tuple[str, str, str]: (full_key, key_prefix, secret)
    """
    clean_prefix = prefix.rstrip(".")
    random_suffix = secrets.token_hex(4)
    full_prefix = (
        f"{clean_prefix}{random_suffix}"
        if clean_prefix.endswith("_")
        else f"{clean_prefix}_{random_suffix}"
    )
    secret = secrets.token_urlsafe(32)
    full_key = f"{full_prefix}.{secret}"
    return full_key, full_prefix, secret


def hash_api_key(
    raw_key_or_secret: str,
    pepper: str | bytes | None = None,
    rounds: int = 12,
) -> str:
    """
    Hash an API key secret using HMAC-SHA256 with server pepper (Decision D-2),
    or fallback to bcrypt if rounds is explicitly provided without pepper.
    """
    if pepper is not None:
        p_bytes = pepper.encode("utf-8") if isinstance(pepper, str) else pepper
        return hmac.new(
            p_bytes, raw_key_or_secret.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    # Legacy bcrypt fallback
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(raw_key_or_secret.encode("utf-8"), salt).decode("utf-8")


def verify_api_key_hash(
    raw_key_or_secret: str,
    hashed: str,
    pepper: str | bytes | None = None,
) -> bool:
    """
    Verify an API key secret against its stored hash in constant time.
    Supports peppered HMAC-SHA256 (64 hex characters) and legacy bcrypt ($2...).
    """
    if hashed.startswith("$2"):
        try:
            return bcrypt.checkpw(
                raw_key_or_secret.encode("utf-8"), hashed.encode("utf-8")
            )
        except (ValueError, TypeError):
            return False

    if pepper is not None:
        p_bytes = pepper.encode("utf-8") if isinstance(pepper, str) else pepper
        computed = hmac.new(
            p_bytes, raw_key_or_secret.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, hashed)

    return False


async def _verify_api_key_hash_async(
    raw_key_or_secret: str,
    hashed: str,
    pepper: str | bytes | None = None,
) -> bool:
    """
    Non-blocking wrapper for API key hash verification.
    Offloads CPU-heavy bcrypt verification to the default executor so the event loop is never blocked.
    """
    if hashed.startswith("$2"):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, verify_api_key_hash, raw_key_or_secret, hashed, pepper
        )
    return verify_api_key_hash(raw_key_or_secret, hashed, pepper=pepper)


async def require_api_key(
    request: Request | None = None,
    *,
    header_name: str = "x-api-key",
    table: str = "api_keys",
    prefix_column: str = "key_prefix",
    hash_column: str = "key_hash",
    pepper: str | bytes | None = None,
    pool: DatabasePool | None = None,
    key_validator: Callable[[str], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """
    Parse an API key from request headers and validate it against the database (Decision D-2).

    Enforces:
      1. Strict '<prefix>.<secret>' key format (legacy unsegmented keys are rejected upfront)
      2. Prefix-indexed O(1) single-row lookup ('SELECT * FROM table WHERE key_prefix = $1 LIMIT 1')
      3. Constant-time HMAC-SHA256 verification with server pepper (or executor-offloaded bcrypt)
      4. Active transaction connection reuse via get_active_connection()
      5. Validation of active, revoked, and timezone-aware expires_at constraints
      6. Stripping of sensitive hash/secret fields before attaching to request.state and Context
    """
    # 1. Resolve request from argument or task Context
    req = request or Context.get("request")
    if req is None or not isinstance(req, Request):
        raise RuntimeError(
            "No active HTTP Request found. Pass 'request' explicitly or call within request pipeline."
        )

    # 2. Extract API key from headers and check for duplicate headers
    raw_key: str | None = None

    header_name_bytes = header_name.lower().encode("latin1")
    matching_custom_headers = [
        v.decode("latin1")
        for k, v in req.scope.get("headers", [])
        if k.lower() == header_name_bytes
    ]
    if len(matching_custom_headers) > 1:
        raise InvalidApiKeyError(f"Multiple '{header_name}' headers provided")
    if matching_custom_headers:
        raw_key = matching_custom_headers[0].strip()

    if not raw_key:
        raw_auth_headers = [
            v.decode("latin1")
            for k, v in req.scope.get("headers", [])
            if k.lower() == b"authorization"
        ]
        if len(raw_auth_headers) > 1:
            raise InvalidApiKeyError("Multiple Authorization headers provided")
        if raw_auth_headers:
            parts = raw_auth_headers[0].strip().split(" ", 1)
            if len(parts) == 2 and parts[0].lower() in {"apikey", "bearer"}:
                raw_key = parts[1].strip()

    if not raw_key:
        raise InvalidApiKeyError(f"Missing API key header '{header_name}'")

    # 3. Custom validator callback (if provided)
    if key_validator is not None:
        user_or_record = await key_validator(raw_key)
        if not user_or_record:
            raise InvalidApiKeyError("Invalid API key")

        record_dict = (
            dict(user_or_record)
            if isinstance(user_or_record, dict)
            else {"user": user_or_record}
        )
        record_dict.pop(hash_column, None)
        record_dict.pop("key_hash", None)
        record_dict.pop("secret", None)
        record_dict.pop("password", None)
        record_dict.pop("password_hash", None)
        req.state["user"] = record_dict
        req.state["auth_guard"] = "api_key"
        Context.set("__swiftpy_current_user__", record_dict)
        return record_dict

    # 4. Validate table and column identifiers to prevent SQL injection
    table = validate_identifier(table, param_name="table")
    valid_prefix_col = validate_identifier(prefix_column, param_name="prefix_column")
    hash_column = validate_identifier(hash_column, param_name="column")

    # 5. Enforce '<prefix>.<secret>' format (Decision D-2); reject legacy unsegmented keys upfront
    if "." not in raw_key:
        raise InvalidApiKeyError(
            "Invalid API key format. Expected '<prefix>.<secret>'."
        )

    prefix, secret = raw_key.split(".", 1)
    if not prefix or not secret:
        raise InvalidApiKeyError(
            "Invalid API key format. Expected '<prefix>.<secret>'."
        )

    # 6. Resolve active transaction connection or DatabasePool
    active_conn = get_active_connection()
    active_pool = pool
    if active_conn is None and active_pool is None:
        try:
            container = get_container()
            if container.has(DatabasePool):
                active_pool = container.resolve(DatabasePool)
        except RuntimeError:
            pass

    if active_conn is None and active_pool is None:
        raise RuntimeError(
            "DatabasePool is required to validate API keys against the database."
        )

    active_pepper = (
        pepper.encode("utf-8")
        if isinstance(pepper, str)
        else (pepper if pepper is not None else get_server_pepper())
    )

    # 7. Single-row O(1) indexed lookup by key_prefix
    query = f'SELECT * FROM "{table}" WHERE "{valid_prefix_col}" = $1 LIMIT 1'
    try:
        if active_conn is not None:
            row = await active_conn.fetchrow(query, prefix)
        else:
            assert active_pool is not None
            db = active_pool.get_pool()
            async with db.acquire() as conn:
                row = await conn.fetchrow(query, prefix)
    except Exception as exc:
        logger.exception(
            "Authentication service unavailable: database error during API key check"
        )
        raise AuthenticationServiceUnavailableError(
            "Authentication service unavailable"
        ) from exc

    if row is None:
        # Constant-time dummy comparison to prevent prefix-timing side channels
        dummy_computed = hmac.new(
            active_pepper, b"dummy_constant_time_comparison", hashlib.sha256
        ).hexdigest()
        hmac.compare_digest(dummy_computed, _DUMMY_HASH)
        raise InvalidApiKeyError("Invalid API key")

    row_dict = dict(row)
    stored_hash: str = str(row_dict.get(hash_column) or "")

    if not stored_hash or not await _verify_api_key_hash_async(
        secret, stored_hash, pepper=active_pepper
    ):
        raise InvalidApiKeyError("Invalid API key")

    # Check active status
    if row_dict.get("is_active") is False or row_dict.get("active") is False:
        raise InvalidApiKeyError("API key is inactive")

    # Check revocation
    if row_dict.get("revoked") is True:
        raise InvalidApiKeyError("API key has been revoked")

    # Check expiration with strict type validation (P2-A5)
    now = datetime.now(UTC)
    expires_at = row_dict.get("expires_at")
    if expires_at is not None:
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at)
            except ValueError as exc:
                raise InvalidApiKeyError("Invalid API key") from exc
        elif not isinstance(expires_at, datetime):
            raise InvalidApiKeyError("Invalid API key")

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if now >= expires_at:
            raise InvalidApiKeyError("API key has expired")

    # 8. Strip sensitive credentials before storing in request state / Context (P2-A2 requirement 4)
    safe_record = dict(row_dict)
    safe_record.pop(hash_column, None)
    safe_record.pop("key_hash", None)
    safe_record.pop("secret", None)
    safe_record.pop("password", None)
    safe_record.pop("password_hash", None)

    req.state["user"] = safe_record
    req.state["auth_guard"] = "api_key"
    Context.set("__swiftpy_current_user__", safe_record)

    return safe_record


class ApiKeyGuard(Middleware):
    """
    Middleware adapter for API key authentication (Decision D-2).
    Applies require_api_key() and handles AuthenticationError (401) or
    AuthenticationServiceUnavailableError (503).
    """

    def __init__(
        self,
        table: str = "api_keys",
        prefix_column: str = "key_prefix",
        hash_column: str = "key_hash",
        header_name: str = "x-api-key",
        pepper: str | bytes | None = None,
        pool: DatabasePool | None = None,
    ) -> None:
        self.table = validate_identifier(table, param_name="table")
        self.prefix_column = validate_identifier(prefix_column, param_name="prefix_column")
        self.hash_column = validate_identifier(hash_column, param_name="column")
        self.header_name = header_name
        self.pepper = pepper
        self.pool = pool

    @classmethod
    def from_config(
        cls,
        config: Config | None = None,
        pool: DatabasePool | None = None,
    ) -> ApiKeyGuard:
        """Construct an ApiKeyGuard reading settings from Config ('auth.guards.api_key.*')."""
        if config is None:
            try:
                cfg = Config.get_instance()
            except (RuntimeError, ValueError):
                cfg = Config()
        else:
            cfg = config

        guard_cfg: dict[str, Any] = {}
        guards = cfg.get("auth.guards")
        if isinstance(guards, dict) and isinstance(guards.get("api_key"), dict):
            guard_cfg = guards["api_key"]
        elif isinstance(cfg.get("auth.api_key"), dict):
            guard_cfg = cfg.get("auth.api_key")

        table = str(
            guard_cfg.get("table")
            or cfg.get("auth.guards.api_key.table")
            or "api_keys"
        )
        prefix_column = str(
            guard_cfg.get("prefix_column")
            or cfg.get("auth.guards.api_key.prefix_column")
            or "key_prefix"
        )
        hash_column = str(
            guard_cfg.get("hash_column")
            or cfg.get("auth.guards.api_key.hash_column")
            or "key_hash"
        )
        header_name = str(
            guard_cfg.get("header")
            or guard_cfg.get("header_name")
            or cfg.get("auth.guards.api_key.header")
            or "x-api-key"
        )
        pepper = guard_cfg.get("pepper") or cfg.get("auth.guards.api_key.pepper")

        return cls(
            table=table,
            prefix_column=prefix_column,
            hash_column=hash_column,
            header_name=header_name,
            pepper=str(pepper) if pepper is not None else None,
            pool=pool,
        )

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        try:
            await require_api_key(
                request,
                table=self.table,
                prefix_column=self.prefix_column,
                hash_column=self.hash_column,
                header_name=self.header_name,
                pepper=self.pepper,
                pool=self.pool,
            )
        except AuthenticationError as exc:
            return unauthorized(exc.message, headers=exc.headers)
        except AuthenticationServiceUnavailableError as exc:
            return json_response(
                {"detail": exc.message},
                status_code=exc.status_code,
                headers=exc.headers,
            )

        return await next_handler(request)


__all__ = [
    "ApiKeyGuard",
    "generate_api_key",
    "get_server_pepper",
    "hash_api_key",
    "require_api_key",
    "verify_api_key_hash",
]
