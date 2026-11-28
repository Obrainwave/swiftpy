from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

import asyncpg
import pytest

from swiftpy.auth.auth_manager import AuthUser
from swiftpy.core.application import Application
from swiftpy.core.config import ConfigurationError
from swiftpy.core.container import bind_container
from swiftpy.database.orm import Model
from swiftpy.database.orm.registry import (
    restore_model_registry,
    snapshot_model_registry,
)
from swiftpy.database.pool import DatabasePool
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient
from swiftpy.validation import FormRequest, UniqueRule, exists, unique, validate
from swiftpy.validation.rules.registry import RuleRegistry, default_registry


@pytest.fixture
async def accounts_table(ensure_db_pool: DatabasePool) -> AsyncGenerator[str, None]:
    if ensure_db_pool is None:
        pytest.skip("PostgreSQL is not available in local test environment")
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
    app = Application(debug=False)
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


class _UpdateEmailForm(FormRequest):
    def rules(self) -> dict[str, Any]:
        return {
            "email": f"required|email|unique:{self.route('tbl')},email,{self.route('id')}",
        }


@pytest.mark.asyncio
async def test_request_validate_unique_update_route(
    ensure_db_pool: DatabasePool, accounts_table: str
) -> None:
    """Verify `await request.validate({'email': f'required|email|unique:users,email,{id}'})` and `FormRequest.route()`."""
    app = Application(auto_discover_providers=False, debug=False)
    app.container.instance(DatabasePool, ensure_db_pool)

    @app.put("/users/{id}")
    async def update_user(request: Request, id: int) -> Response:
        validated = await request.validate(
            {
                "email": f"required|email|unique:{accounts_table},email,{id}",
            }
        )
        return json_response(
            {
                "id": id,
                "route_id": request.route("id"),
                "email": validated["email"],
                "email_attr": validated.email,
            }
        )

    @app.put("/forms/{tbl}/{id}")
    async def update_via_form(form: _UpdateEmailForm, id: int) -> Response:
        clean = form.validated()
        return json_response({"id": id, "route_id": form.route("id"), "email": clean.email})

    client = TestClient(app)
    # 1. Updating user 1 with own existing email passes (stringified INT id coerced cleanly)
    own_res = await client.put("/users/1", json={"email": "alice@example.com"})
    own_res.assert_ok()
    assert own_res.json() == {
        "id": 1,
        "route_id": 1,
        "email": "alice@example.com",
        "email_attr": "alice@example.com",
    }

    # 2. Updating user 1 with user 2's email fails with 422
    dup_res = await client.put("/users/1", json={"email": "bob@example.com"})
    assert dup_res.status_code == 422
    assert dup_res.json() == {
        "errors": {"email": ["The email has already been taken."]}
    }

    # 3. Updating user 1 with brand-new email passes
    new_res = await client.put("/users/1", json={"email": "alice.updated@example.com"})
    new_res.assert_ok()
    assert new_res.json()["email"] == "alice.updated@example.com"

    # 4. Dynamic FormRequest using self.route() inside rules() works identically
    form_own = await client.put(
        f"/forms/{accounts_table}/1", json={"email": "alice@example.com"}
    )
    form_own.assert_ok()
    assert form_own.json() == {
        "id": 1,
        "route_id": 1,
        "email": "alice@example.com",
    }

    form_dup = await client.put(
        f"/forms/{accounts_table}/1", json={"email": "bob@example.com"}
    )
    assert form_dup.status_code == 422


@pytest.mark.asyncio
async def test_unique_rule_null_tokens_custom_pk_model_and_fluent_ignore(
    ensure_db_pool: DatabasePool,
) -> None:
    """Verify NULL/None string tokens, Model/AuthUser ignore targets, fluent .ignore(), and int->str PK coercion."""
    custom_table = f"test_custom_pk_{uuid.uuid4().hex[:8]}"
    str_pk_table = f"test_str_pk_{uuid.uuid4().hex[:8]}"
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE "{custom_table}" (
                account_uuid UUID PRIMARY KEY,
                email VARCHAR(120) NOT NULL
            );
            INSERT INTO "{custom_table}" (account_uuid, email) VALUES
                ('11111111-1111-1111-1111-111111111111', 'first@example.com'),
                ('22222222-2222-2222-2222-222222222222', 'second@example.com');

            CREATE TABLE "{str_pk_table}" (
                id TEXT PRIMARY KEY,
                email VARCHAR(120) NOT NULL
            );
            INSERT INTO "{str_pk_table}" (id, email) VALUES
                ('1', 'str1@example.com'),
                ('2', 'str2@example.com');
            """
        )

    class _CustomAccount(Model):
        __table__ = custom_table
        __primary_key__ = "account_uuid"
        __fillable__: ClassVar[list[str]] = ["account_uuid", "email"]

        account_uuid: str
        email: str

    try:
        # 1. NULL / None / empty ignore tokens keep id_column intact & do not ignore rows
        for null_expr in (
            f"unique:{custom_table},email,NULL,account_uuid",
            f"unique:{custom_table},email,None,account_uuid",
            f"unique:{custom_table},email,,account_uuid",
        ):
            parsed_null = default_registry.parse(null_expr)
            assert parsed_null.ignore_id is None
            assert parsed_null.id_column == "account_uuid"
            res_null = await validate(
                {"email": "first@example.com"},
                {"email": null_expr},
            )
            assert res_null.is_valid is False

        # 2. UUID PK in string rule ignores matching UUID row & rejects others
        res_uuid_own = await validate(
            {"email": "first@example.com"},
            {"email": f"unique:{custom_table},email,11111111-1111-1111-1111-111111111111,account_uuid"},
        )
        assert res_uuid_own.is_valid is True

        res_uuid_other = await validate(
            {"email": "second@example.com"},
            {"email": f"unique:{custom_table},email,11111111-1111-1111-1111-111111111111,account_uuid"},
        )
        assert res_uuid_other.is_valid is False

        # 3. Model instance + fluent .ignore() infers __primary_key__ automatically
        acc = _CustomAccount(
            account_uuid="11111111-1111-1111-1111-111111111111",
            email="first@example.com",
        )
        res_model = await validate(
            {"email": "first@example.com"},
            {"email": [unique(custom_table, "email", pool=ensure_db_pool).ignore(acc)]},
        )
        assert res_model.is_valid is True

        # 4. AuthUser wrapping Model infers model PK cleanly
        auth_user = AuthUser({"id": "ignored", "email": "first@example.com"}, model=acc)
        res_auth_user = await validate(
            {"email": "first@example.com"},
            {"email": [unique(custom_table, "email", ignore_id=auth_user, pool=ensure_db_pool)]},
        )
        assert res_auth_user.is_valid is True

        # 5. Unsaved Model with None primary key ignores nothing
        unsaved_acc = _CustomAccount(email="first@example.com")
        res_unsaved = await validate(
            {"email": "first@example.com"},
            {"email": [unique(custom_table, "email", pool=ensure_db_pool).ignore(unsaved_acc)]},
        )
        assert res_unsaved.is_valid is False

        # 6. Integer ignore_id against TEXT primary key storing numeric IDs works cleanly (both int and string rule)
        res_int_for_text_own = await validate(
            {"email": "str1@example.com"},
            {"email": [unique(str_pk_table, "email", ignore_id=1, pool=ensure_db_pool)]},
        )
        assert res_int_for_text_own.is_valid is True

        res_str_rule_for_text_own = await validate(
            {"email": "str1@example.com"},
            {"email": f"unique:{str_pk_table},email,1"},
        )
        assert res_str_rule_for_text_own.is_valid is True

        res_int_for_text_other = await validate(
            {"email": "str2@example.com"},
            {"email": [unique(str_pk_table, "email", ignore_id=1, pool=ensure_db_pool)]},
        )
        assert res_int_for_text_other.is_valid is False

        # 7. Boolean ignore_id is rejected eagerly with TypeError
        with pytest.raises(TypeError, match="boolean"):
            unique(str_pk_table, "email", ignore_id=True)
        with pytest.raises(TypeError, match="boolean"):
            unique(str_pk_table, "email").ignore(False)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{custom_table}" CASCADE;')
            await conn.execute(f'DROP TABLE IF EXISTS "{str_pk_table}" CASCADE;')


@pytest.mark.asyncio
async def test_unique_validation_create_and_update_with_real_orm_records(
    ensure_db_pool: DatabasePool,
) -> None:
    """
    End-to-end PostgreSQL verification for `request.validate({'email': f'required|email|unique:users,email,{id}'})`
    across record creation, updating own record, duplicate rejections, nonexistent ignore IDs,
    missing/invalid email failures, extra-field stripping, and controller abort behavior.
    """
    users_table = f"test_users_crud_{uuid.uuid4().hex[:8]}"
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE "{users_table}" (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(150) NOT NULL UNIQUE
            );
            """
        )

    snap = snapshot_model_registry()

    class _CrudUser(Model):
        __table__ = users_table
        __fillable__: ClassVar[list[str]] = ["name", "email"]

        id: int
        name: str
        email: str

    app = Application(auto_discover_providers=False, debug=False)
    app.container.instance(DatabasePool, ensure_db_pool)

    create_executed_count = 0
    update_executed_count = 0

    @app.post("/users")
    async def create_user(request: Request) -> Response:
        nonlocal create_executed_count
        validated = await request.validate(
            {
                "name": "required|str|min:2",
                "email": f"required|email|unique:{users_table},email",
            }
        )
        create_executed_count += 1
        created = await _CrudUser.create(dict(validated))
        return json_response(
            {
                "id": created.id,
                "name": created.name,
                "email": created.email,
                "validated_keys": sorted(validated.keys()),
            },
            status_code=201,
        )

    @app.put("/users/{user_id}")
    async def update_user(request: Request, user_id: int) -> Response:
        nonlocal update_executed_count
        validated = await request.validate(
            {
                "email": f"required|email|unique:{users_table},email,{user_id}",
            }
        )
        update_executed_count += 1
        record = await _CrudUser.find(user_id)
        if record is None:
            return json_response({"detail": "Not Found"}, status_code=404)
        await record.update({"email": validated.email})
        return json_response(
            {
                "id": record.id,
                "email": record.email,
                "validated_keys": sorted(validated.keys()),
            }
        )

    try:
        with bind_container(app.container):
            alice = await _CrudUser.create({"name": "Alice", "email": "alice@example.com"})
            bob = await _CrudUser.create({"name": "Bob", "email": "bob@example.com"})

            client = TestClient(app)

            # 1. Creating a user with a new email passes and persists
            res_create_ok = await client.post(
                "/users",
                json={
                    "name": "Carol",
                    "email": "carol@example.com",
                    "is_admin": True,
                },
            )
            assert res_create_ok.status_code == 201
            assert res_create_ok.json()["validated_keys"] == ["email", "name"]
            assert create_executed_count == 1

            # 2. Creating a user with an existing email fails with 422 without entering handler body
            res_create_dup = await client.post(
                "/users",
                json={"name": "Eve", "email": "bob@example.com"},
            )
            assert res_create_dup.status_code == 422
            assert res_create_dup.json() == {
                "errors": {"email": ["The email has already been taken."]}
            }
            assert create_executed_count == 1

            # 3. Updating Alice without changing her email passes (ignoring her own ID)
            res_keep_own = await client.put(
                f"/users/{alice.id}",
                json={"email": "alice@example.com"},
            )
            res_keep_own.assert_ok()
            assert res_keep_own.json()["email"] == "alice@example.com"
            assert update_executed_count == 1

            # 4. Updating Alice with Bob's email fails with 422 and aborts before update logic
            res_take_bob = await client.put(
                f"/users/{alice.id}",
                json={"email": "bob@example.com"},
            )
            assert res_take_bob.status_code == 422
            assert res_take_bob.json() == {
                "errors": {"email": ["The email has already been taken."]}
            }
            assert update_executed_count == 1
            reloaded_alice = await _CrudUser.find(alice.id)
            assert reloaded_alice is not None and reloaded_alice.email == "alice@example.com"

            # 5. Updating Alice with a new email + extra untrusted field passes and strips extra field
            res_new_email = await client.put(
                f"/users/{alice.id}",
                json={"email": "alice.new@example.com", "role": "superuser"},
            )
            res_new_email.assert_ok()
            assert res_new_email.json() == {
                "id": alice.id,
                "email": "alice.new@example.com",
                "validated_keys": ["email"],
            }
            reloaded_alice = await _CrudUser.find(alice.id)
            assert reloaded_alice is not None and reloaded_alice.email == "alice.new@example.com"

            # 6. Ignoring an ID that doesn't exist must NOT bypass Bob's duplicate email
            res_nonexistent_id = await client.put(
                "/users/999999",
                json={"email": bob.email},
            )
            assert res_nonexistent_id.status_code == 422
            assert res_nonexistent_id.json() == {
                "errors": {"email": ["The email has already been taken."]}
            }

            # 7. Invalid email fails email validation rule
            res_bad_email = await client.put(
                f"/users/{alice.id}",
                json={"email": "not-an-email"},
            )
            assert res_bad_email.status_code == 422
            assert "valid email" in res_bad_email.json()["errors"]["email"][0]

            # 8. Missing or blank required email fails required rule
            res_missing = await client.put(f"/users/{alice.id}", json={})
            assert res_missing.status_code == 422
            assert "required" in res_missing.json()["errors"]["email"][0]

            res_blank = await client.put(f"/users/{alice.id}", json={"email": ""})
            assert res_blank.status_code == 422
            assert "required" in res_blank.json()["errors"]["email"][0]
    finally:
        restore_model_registry(snap)
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{users_table}" CASCADE;')


@pytest.mark.asyncio
async def test_unique_rule_parameterized_query_missing_column_and_async_concurrency(
    ensure_db_pool: DatabasePool, accounts_table: str
) -> None:
    """Verify parameterized SQL args, missing column failures, malformed DSL errors, and non-blocking async concurrency."""
    recorded_calls: list[tuple[str, tuple[Any, ...]]] = []

    class _RecordingConn:
        async def fetchval(self, query: str, *args: Any) -> Any:
            recorded_calls.append((query, args))
            return None

    ignored_payload = "42; DROP TABLE users;--"
    rule = UniqueRule(
        table="users",
        column="email",
        ignore_id=ignored_payload,
        id_column="id",
    )
    row = await rule._fetchval_unique(
        _RecordingConn(),
        '"email"',
        "$1",
        "alice@example.com",
        use_savepoint=False,
    )
    assert row is None
    assert len(recorded_calls) == 1
    executed_sql, bound_args = recorded_calls[0]
    assert 'SELECT 1 FROM "users" WHERE "email" = $1 AND "id" != $2 LIMIT 1' == executed_sql
    assert bound_args == ("alice@example.com", ignored_payload)

    # Canonical integer string ignore_id from string rule binds int on the FIRST query (single query, no retry)
    recorded_calls.clear()
    parsed_int_rule = default_registry.parse("unique:users,email,42")
    assert isinstance(parsed_int_rule, UniqueRule)
    await parsed_int_rule._fetchval_unique(
        _RecordingConn(),
        '"email"',
        "$1",
        "alice@example.com",
        use_savepoint=False,
    )
    assert len(recorded_calls) == 1
    assert recorded_calls[0][1] == ("alice@example.com", 42)

    # Missing column on an existing table must raise DB error (never silently report unique=True)
    with pytest.raises(asyncpg.UndefinedColumnError):
        await validate(
            {"email": "new@example.com"},
            {"email": [unique(accounts_table, "nonexistent_col", pool=ensure_db_pool)]},
        )

    # Malformed unique rules (including >4 positional args or empty id_column) raise ConfigurationError predictably
    reg = RuleRegistry()
    for bad_rule in (
        "unique",
        "unique:",
        "unique:users",
        "unique:,email",
        "unique:users,email,1,",
        "unique:users,email,1,id,extra",
    ):
        with pytest.raises(ConfigurationError):
            reg.parse_rules(bad_rule)

    # Concurrent async unique checks run without blocking the event loop
    tick_counter = 0

    async def _ticker() -> None:
        nonlocal tick_counter
        for _ in range(3):
            tick_counter += 1
            await asyncio.sleep(0.005)

    results, _ = await asyncio.gather(
        asyncio.gather(
            validate(
                {"email": "alice@example.com"},
                {"email": f"unique:{accounts_table},email,1"},
            ),
            validate(
                {"email": "bob@example.com"},
                {"email": f"unique:{accounts_table},email,1"},
            ),
            validate(
                {"email": "brandnew@example.com"},
                {"email": f"unique:{accounts_table},email"},
            ),
        ),
        _ticker(),
    )
    assert results[0].is_valid is True
    assert results[1].is_valid is False
    assert results[2].is_valid is True
    assert tick_counter == 3
