from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

import pytest

from swiftpy.core.application import Application
from swiftpy.database.pool import DatabasePool
from swiftpy.http.response import Response, json_response
from swiftpy.validation import FormRequest, exists, unique, validate


@pytest.fixture
async def accounts_table(ensure_db_pool: DatabasePool) -> AsyncGenerator[str, None]:
    table_name = f"test_acc_{uuid.uuid4().hex[:8]}"
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) NOT NULL,
                email VARCHAR(100) NOT NULL
            );
            """
        )
        await conn.execute(
            f"""
            INSERT INTO "{table_name}" (id, username, email)
            VALUES (1, 'alice', 'alice@example.com'),
                   (2, 'bob', 'bob@example.com');
            """
        )

    try:
        yield table_name
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{table_name}";')


def _make_http_scope(
    method: str = "GET",
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 50000),
    }


async def _simulate_request(
    app: Application,
    method: str,
    path: str,
    body: dict[str, Any] | bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    if isinstance(body, dict):
        raw_body = json.dumps(body).encode("utf-8")
        raw_headers = [(b"content-type", b"application/json")]
    elif isinstance(body, bytes):
        raw_body = body
        raw_headers = [(b"content-type", b"application/json")]
    else:
        raw_body = b""
        raw_headers = []

    scope = _make_http_scope(method=method, path=path, headers=raw_headers)

    status_code = 0
    resp_body = bytearray()
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code, resp_body
        if message["type"] == "http.response.start":
            status_code = message["status"]
        elif message["type"] == "http.response.body":
            resp_body.extend(message.get("body", b""))

    await app(scope, receive, send)

    try:
        data = json.loads(resp_body.decode("utf-8")) if resp_body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = {"raw": resp_body.decode("utf-8", errors="replace")}

    return status_code, data


@pytest.mark.asyncio
async def test_unique_rule(ensure_db_pool: DatabasePool, accounts_table: str) -> None:
    # 1. Unique email not in database -> passes
    res_valid = await validate(
        {"email": "charlie@example.com"},
        {"email": [unique(accounts_table, "email", pool=ensure_db_pool)]},
    )
    assert res_valid.is_valid is True

    # 2. Duplicate email -> fails
    res_dup = await validate(
        {"email": "alice@example.com"},
        {"email": [unique(accounts_table, "email", pool=ensure_db_pool)]},
    )
    assert res_dup.is_valid is False
    assert res_dup.first_error("email") == "The email has already been taken."

    # 3. Unique with ignore_id -> passes when id matches ignored record
    res_ignored = await validate(
        {"email": "alice@example.com"},
        {
            "email": [
                unique(
                    accounts_table,
                    "email",
                    ignore_id=1,
                    pool=ensure_db_pool,
                )
            ]
        },
    )
    assert res_ignored.is_valid is True

    # 4. Unique with ignore_id -> fails when id belongs to a different record
    res_diff_id = await validate(
        {"email": "alice@example.com"},
        {
            "email": [
                unique(
                    accounts_table,
                    "email",
                    ignore_id=2,
                    pool=ensure_db_pool,
                )
            ]
        },
    )
    assert res_diff_id.is_valid is False


@pytest.mark.asyncio
async def test_exists_rule(ensure_db_pool: DatabasePool, accounts_table: str) -> None:
    # 1. Exists in database -> passes
    res_exists = await validate(
        {"email": "bob@example.com"},
        {"email": [exists(accounts_table, "email", pool=ensure_db_pool)]},
    )
    assert res_exists.is_valid is True

    # 2. Does not exist in database -> fails
    res_missing = await validate(
        {"email": "ghost@example.com"},
        {"email": [exists(accounts_table, "email", pool=ensure_db_pool)]},
    )
    assert res_missing.is_valid is False
    assert res_missing.first_error("email") == "The selected email is invalid."


@pytest.mark.asyncio
async def test_db_rules_string_syntax(
    ensure_db_pool: DatabasePool, accounts_table: str
) -> None:
    schema = {
        "email": f"required|email|unique:{accounts_table},email",
        "existing_user": f"required|exists:{accounts_table},username",
    }

    # Valid check
    res_valid = await validate(
        {
            "email": "newuser@example.com",
            "existing_user": "alice",
        },
        schema,
    )
    assert res_valid.is_valid is True

    # Invalid check
    res_invalid = await validate(
        {
            "email": "alice@example.com",
            "existing_user": "nonexistent_person",
        },
        schema,
    )
    assert res_invalid.is_valid is False
    assert "email" in res_invalid.errors
    assert "existing_user" in res_invalid.errors


@pytest.mark.asyncio
async def test_sql_injection_defense() -> None:
    # Malicious table names must be rejected upfront
    with pytest.raises(ValueError, match="Invalid table identifier"):
        unique("accounts; DROP TABLE users;--", "email")

    with pytest.raises(ValueError, match="Invalid column identifier"):
        exists("accounts", "email; DROP TABLE users;--")

    with pytest.raises(ValueError, match="Invalid column identifier"):
        unique("accounts", "email", ignore_id=1, id_column="id;--")


@pytest.mark.asyncio
async def test_db_rules_error_handling_does_not_silently_pass(
    ensure_db_pool: DatabasePool,
) -> None:
    non_existent_table = f"non_existent_{uuid.uuid4().hex[:8]}"

    # UniqueRule on missing table must raise, NOT return is_valid=True
    with pytest.raises(Exception) as exc_unique:
        await validate(
            {"email": "test@example.com"},
            {"email": [unique(non_existent_table, "email", pool=ensure_db_pool)]},
        )
    assert "password" not in str(exc_unique.value).lower()

    # ExistsRule on missing table must raise, NOT return is_valid=True
    with pytest.raises(Exception) as exc_exists:
        await validate(
            {"email": "test@example.com"},
            {"email": [exists(non_existent_table, "email", pool=ensure_db_pool)]},
        )
    assert "password" not in str(exc_exists.value).lower()


class MissingTableRequest(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "email": "required|email|unique:missing_table_that_does_not_exist,email"
    }


@pytest.mark.asyncio
async def test_db_failure_in_pipeline_does_not_leak_internals(
    ensure_db_pool: DatabasePool,
) -> None:
    app = Application()
    app.container.instance(DatabasePool, ensure_db_pool)

    @app.post("/register")
    async def register(form: MissingTableRequest) -> Response:
        return json_response({"ok": True})

    status, res = await _simulate_request(
        app, "POST", "/register", body={"email": "alice@example.com"}
    )
    # The HTTP framework must catch unhandled exceptions, return 500, and NOT leak queries/secrets
    assert status == 500
    assert res == {"detail": "Internal Server Error"}
    assert "SELECT" not in str(res)
    assert "password" not in str(res)
