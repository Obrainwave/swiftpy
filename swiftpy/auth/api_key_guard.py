from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import bcrypt

from swiftpy.auth.exceptions import (
    AuthenticationError,
    AuthenticationServiceUnavailableError,
    InvalidApiKeyError,
)
from swiftpy.core.container import get_container
from swiftpy.core.context import Context
from swiftpy.database.pool import DatabasePool
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response, unauthorized


def hash_api_key(raw_key: str, rounds: int = 12) -> str:
    """
    Hash a raw API key using bcrypt for secure storage in the database.
    """
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(raw_key.encode("utf-8"), salt).decode("utf-8")


def verify_api_key_hash(raw_key: str, hashed: str) -> bool:
    """
    Verify a raw API key against a stored bcrypt hash in constant time.
    """
    try:
        return bcrypt.checkpw(raw_key.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


async def require_api_key(
    request: Request | None = None,
    *,
    header_name: str = "x-api-key",
    table: str = "api_keys",
    hash_column: str = "key_hash",
    pool: DatabasePool | None = None,
    key_validator: Callable[[str], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """
    Parse an API key from request headers and validate it against a bcrypt hash in the database.

    Verifies:
      1. Presence of API key in request headers (e.g. X-API-Key or Authorization header)
      2. Bcrypt hash match in the database table
      3. Revocation status (revoked column is False)
      4. Expiration status (expires_at is None or in the future)

    Attaches the authenticated record to request.state and task Context.
    """
    # 1. Resolve request from argument or task Context
    req = request or Context.get("request")
    if req is None or not isinstance(req, Request):
        raise RuntimeError(
            "No active HTTP Request found. Pass 'request' explicitly or call within request pipeline."
        )

    # 2. Extract API key from headers and check for duplicate headers
    raw_key: str | None = None

    # Check custom header (case-insensitively)
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

    # Fallback to Authorization: ApiKey <key> or Bearer <key> if not found in custom header
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
        req.state["user"] = record_dict
        req.state["auth_guard"] = "api_key"
        Context.set("__swiftpy_current_user__", record_dict)
        return record_dict

    # 4. Validate table and column identifiers to prevent SQL injection
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table):
        raise ValueError(f"Invalid table identifier '{table}'")
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", hash_column):
        raise ValueError(f"Invalid column identifier '{hash_column}'")

    # 5. Resolve DatabasePool
    active_pool = pool
    if active_pool is None:
        try:
            container = get_container()
            if container.has(DatabasePool):
                active_pool = container.resolve(DatabasePool)
        except RuntimeError:
            pass

    if active_pool is None:
        raise RuntimeError(
            "DatabasePool is required to validate API keys against the database."
        )

    # 6. Query candidate API key records from database
    try:
        db = active_pool.get_pool()
        async with db.acquire() as conn:
            query = f'SELECT * FROM "{table}"'
            rows = await conn.fetch(query)
    except Exception as exc:
        raise AuthenticationServiceUnavailableError(
            f"Authentication service temporarily unavailable: database error ({exc})"
        ) from exc

    now = datetime.now(UTC)
    matched_record: dict[str, Any] | None = None

    for row in rows:
        row_dict = dict(row)
        stored_hash = row_dict.get(hash_column)
        if not stored_hash or not isinstance(stored_hash, str):
            continue

        if verify_api_key_hash(raw_key, stored_hash):
            # Check active status (supports 'is_active' and 'active' boolean columns)
            if row_dict.get("is_active") is False or row_dict.get("active") is False:
                raise InvalidApiKeyError("API key is inactive")

            # Check revocation
            if row_dict.get("revoked") is True:
                raise InvalidApiKeyError("API key has been revoked")

            # Check expiration
            expires_at = row_dict.get("expires_at")
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if now >= expires_at:
                    raise InvalidApiKeyError("API key has expired")

            matched_record = row_dict
            break

    if matched_record is None:
        raise InvalidApiKeyError("Invalid API key")

    # 7. Attach to request state and task context
    req.state["user"] = matched_record
    req.state["auth_guard"] = "api_key"
    Context.set("__swiftpy_current_user__", matched_record)

    return matched_record


class ApiKeyGuard(Middleware):
    """
    Middleware adapter for API key authentication.
    Applies require_api_key() and handles AuthenticationError (401) or
    AuthenticationServiceUnavailableError (503).
    """

    def __init__(
        self,
        table: str = "api_keys",
        hash_column: str = "key_hash",
        header_name: str = "x-api-key",
        pool: DatabasePool | None = None,
    ) -> None:
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table):
            raise ValueError(f"Invalid table identifier '{table}'")
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", hash_column):
            raise ValueError(f"Invalid column identifier '{hash_column}'")

        self.table = table
        self.hash_column = hash_column
        self.header_name = header_name
        self.pool = pool

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        try:
            await require_api_key(
                request,
                table=self.table,
                hash_column=self.hash_column,
                header_name=self.header_name,
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
