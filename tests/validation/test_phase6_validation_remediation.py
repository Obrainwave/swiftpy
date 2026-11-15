from __future__ import annotations

import gc
import json
import uuid
import weakref
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

import pytest

from swiftpy.core.application import Application
from swiftpy.core.config import ConfigurationError
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import db
from swiftpy.http.response import Response, json_response
from swiftpy.validation import (
    BailRule,
    FormRequest,
    RegexRule,
    RuleRegistry,
    bail,
    exists,
    unique,
    validate,
)


def _make_http_scope(
    method: str = "POST",
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


async def _simulate_raw_request(
    app: Application,
    method: str,
    path: str,
    raw_body: bytes,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[str, Any]]:
    scope = _make_http_scope(method=method, path=path, headers=headers or [])
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
    data = json.loads(resp_body.decode("utf-8")) if resp_body else {}
    return status_code, data


async def _simulate_json_request(
    app: Application,
    method: str,
    path: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    return await _simulate_raw_request(
        app,
        method,
        path,
        json.dumps(payload).encode("utf-8"),
        [(b"content-type", b"application/json")],
    )


# ---------------------------------------------------------------------------
# VAL-1: Dynamic FormRequest.rules() per request & custom messages()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_val1_dynamic_form_request_rules_and_custom_messages() -> None:
    class ArticleRequest(FormRequest):
        def rules(self) -> dict[str, Any]:
            if self.request is not None and self.request.method == "PATCH":
                return {
                    "title": "str|min:3",
                    "body": "str",
                }
            return {
                "title": "required|str|min:3",
                "body": "required|str",
            }

        def messages(self) -> dict[str, str]:
            return {
                "title.required": "Custom: please supply an article title.",
                "title.min": "Custom: title must have at least 3 chars.",
            }

    app = Application()

    @app.post("/articles")
    async def create_article(form: ArticleRequest) -> Response:
        return json_response({"method": "POST", "validated": form.validated()})

    @app.patch("/articles")
    async def patch_article(form: ArticleRequest) -> Response:
        return json_response({"method": "PATCH", "validated": form.validated()})

    # 1. POST missing required fields -> triggers custom messages()
    status_post_fail, body_post_fail = await _simulate_json_request(
        app, "POST", "/articles", {"title": "a"}
    )
    assert status_post_fail == 422
    assert body_post_fail["errors"]["title"] == [
        "Custom: title must have at least 3 chars."
    ]
    assert "body" in body_post_fail["errors"]

    # 2. POST missing title -> custom required message
    status_post_req, body_post_req = await _simulate_json_request(
        app, "POST", "/articles", {"body": "content"}
    )
    assert status_post_req == 422
    assert body_post_req["errors"]["title"] == [
        "Custom: please supply an article title."
    ]

    # 3. PATCH without body or title -> dynamic rules() sees PATCH and allows partial update
    # Even AFTER POST evaluated first, cached class rules must NOT freeze PATCH!
    status_patch_ok, body_patch_ok = await _simulate_json_request(
        app, "PATCH", "/articles", {"title": "Updated Title"}
    )
    assert status_patch_ok == 200
    assert body_patch_ok == {
        "method": "PATCH",
        "validated": {"title": "Updated Title"},
    }


@pytest.mark.asyncio
async def test_val1_validator_supports_custom_messages_directly() -> None:
    res = await validate(
        {"name": "A"},
        {"name": "required|str|min:3", "email": "required|email"},
        messages={
            "name.min": "Too short buddy.",
            "email.required": "Email cannot be blank.",
        },
    )
    assert res.fails()
    assert res.errors["name"] == ["Too short buddy."]
    assert res.errors["email"] == ["Email cannot be blank."]


# ---------------------------------------------------------------------------
# VAL-2: Escaped regex patterns and eager ConfigurationError on invalid regex
# ---------------------------------------------------------------------------


def test_val2_regex_escapes_and_eager_compile_errors() -> None:
    registry = RuleRegistry()

    # Escaped pipe inside pipe-separated rule chain
    rules = registry.parse_rules(r"regex:^(foo\|bar)$|required")
    assert len(rules) == 2
    assert rules[0].validate("choice", "foo", {}) is True
    assert rules[0].validate("choice", "bar", {}) is True
    assert rules[0].validate("choice", "baz", {}) is False

    # Literal backslash inside regex (e.g., matching literal a\b and digits)
    bs_rules = registry.parse_rules(r"regex:^a\\b$|str")
    assert len(bs_rules) == 2
    assert bs_rules[0].validate("path", r"a\b", {}) is True
    assert bs_rules[0].validate("path", "ab", {}) is False

    digit_rules = registry.parse_rules(r"regex:^\d+$")
    assert len(digit_rules) == 1
    assert digit_rules[0].validate("pin", "1234", {}) is True
    assert digit_rules[0].validate("pin", "12a4", {}) is False

    # Trailing escaped backslash before pipe delimiter
    slash_then_min = registry.parse_rules(r"regex:^foo\\$|min:4")
    assert len(slash_then_min) == 2
    assert slash_then_min[0].validate("v", "foo\\", {}) is True

    # Invalid regex must fail eagerly with ConfigurationError (G-9)
    with pytest.raises(ConfigurationError):
        RegexRule("[unclosed")

    with pytest.raises(ConfigurationError):
        registry.parse_rules("regex:[unclosed")


# ---------------------------------------------------------------------------
# VAL-3: Eager ConfigurationError on malformed rule arguments / missing args
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_rule",
    [
        "min:abc",
        "max:xyz",
        "min:nan",
        "max:inf",
        "between:1,bad",
        "between:1,2,3",
        "min",
        "max",
        "between",
        "in",
        "not_in",
        "regex",
        "unique",
        "exists",
        "exists:users",
        "exists:users,id,extra",
        "exists:,id",
        "unique:users",
        "in:",
        "not_in:",
    ],
)
def test_val3_invalid_rule_arguments_raise_configuration_error(bad_rule: str) -> None:
    registry = RuleRegistry()
    with pytest.raises(ConfigurationError):
        registry.parse_rules(bad_rule)


# ---------------------------------------------------------------------------
# VAL-4: Database rules (unique/exists) transaction safety & SAVEPOINT isolation
# ---------------------------------------------------------------------------


@pytest.fixture
async def val_items_table(ensure_db_pool: DatabasePool) -> AsyncGenerator[str, None]:
    if ensure_db_pool is None:
        pytest.skip("PostgreSQL is not available in local test environment")
    table_name = f"test_val_items_{uuid.uuid4().hex[:8]}"
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                code INTEGER NOT NULL,
                email VARCHAR(120) NOT NULL
            );
            """
        )
        await conn.execute(
            f"""
            INSERT INTO "{table_name}" (id, code, email)
            VALUES (1, 100, 'alice@example.com'),
                   (2, 200, 'bob@example.com');
            """
        )
    try:
        yield table_name
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{table_name}";')


@pytest.mark.asyncio
async def test_val4_db_rules_savepoint_prevents_tx_poisoning_and_sees_active_tx(
    ensure_db_pool: DatabasePool,
    val_items_table: str,
) -> None:
    async with db.transaction() as tx:
        # 1. Insert uncommitted row inside TX; unique/exists MUST prefer active TX
        #    even when `pool=ensure_db_pool` is explicitly provided!
        await tx.execute(
            f'INSERT INTO "{val_items_table}" (id, code, email) VALUES ($1, $2, $3)',
            3,
            300,
            "uncommitted@example.com",
        )

        dup_in_tx = await validate(
            {"email": "uncommitted@example.com"},
            {"email": [unique(val_items_table, "email", pool=ensure_db_pool)]},
        )
        assert dup_in_tx.fails()

        exists_in_tx = await validate(
            {"code": 300},
            {"code": [exists(val_items_table, "code", pool=ensure_db_pool)]},
        )
        assert exists_in_tx.passes()

        # 2. Type error (asyncpg.DataError: passing string "not_an_int" for INTEGER column)
        #    must return validation failure cleanly AND NOT poison the outer TX!
        bad_type_res = await validate(
            {"code": "not_an_int"},
            {"code": [unique(val_items_table, "code", pool=ensure_db_pool)]},
        )
        assert bad_type_res.fails()

        # 3. Outer TX must still be completely usable after DataError inside rule!
        row_count = await tx.fetchval(f'SELECT COUNT(*) FROM "{val_items_table}"')
        assert row_count == 3

        # 4. String ignore_id (from rule string "unique:tbl,email,1") on SERIAL INT PK must work
        registry = RuleRegistry()
        update_own = await validate(
            {"email": "alice@example.com"},
            {"email": f"unique:{val_items_table},email,1"},
            registry=registry,
        )
        assert update_own.passes()

        update_other = await validate(
            {"email": "bob@example.com"},
            {"email": f"unique:{val_items_table},email,1"},
            registry=registry,
        )
        assert update_other.fails()


# ---------------------------------------------------------------------------
# VAL-5: Canonical deterministic rule order, FloatRule int coercion, BoolRule strip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_val5_deterministic_type_order_float_coercion_and_bool_strip() -> None:
    # "int|str" and "str|int" MUST produce identical results on both str and int inputs
    for sample in ("42", 42, "abc"):
        res_a = await validate({"v": sample}, {"v": "int|str"})
        res_b = await validate({"v": sample}, {"v": "str|int"})
        assert res_a.is_valid == res_b.is_valid
        assert res_a.validated_data == res_b.validated_data
        assert len(res_a.errors) == len(res_b.errors)

    # FloatRule coerces int -> float as well as numeric str -> float
    res_int_to_float = await validate({"price": 3}, {"price": "float"})
    assert res_int_to_float.passes()
    assert res_int_to_float.validated_data["price"] == 3.0
    assert type(res_int_to_float.validated_data["price"]) is float

    # BoolRule strips surrounding whitespace
    res_bool_true = await validate({"flag": " true "}, {"flag": "bool"})
    assert res_bool_true.passes()
    assert res_bool_true.validated_data["flag"] is True

    res_bool_false = await validate({"flag": " 0\n"}, {"flag": "bool"})
    assert res_bool_false.passes()
    assert res_bool_false.validated_data["flag"] is False


# ---------------------------------------------------------------------------
# VAL-6: Form/multipart FormRequest, narrowed DI fallback, set rejection,
#        cache safety, bail rule, wildcard validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_val6_form_request_urlencoded_and_multipart() -> None:
    class LoginFormRequest(FormRequest):
        schema: ClassVar[dict[str, Any]] = {
            "username": "required|str|min:3",
            "remember": "bool",
        }

    app = Application()

    @app.post("/login")
    async def login(form: LoginFormRequest) -> Response:
        return json_response({"validated": form.validated()})

    # 1. application/x-www-form-urlencoded
    status_url, body_url = await _simulate_raw_request(
        app,
        "POST",
        "/login",
        b"username=alice&remember=true",
        [(b"content-type", b"application/x-www-form-urlencoded")],
    )
    assert status_url == 200
    assert body_url["validated"] == {"username": "alice", "remember": True}

    # 2. multipart/form-data
    boundary = "----SwiftPyTestBoundary789"
    mp_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="username"\r\n\r\n'
        "charlie\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="remember"\r\n\r\n'
        "0\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    status_mp, body_mp = await _simulate_raw_request(
        app,
        "POST",
        "/login",
        mp_body,
        [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
    )
    assert status_mp == 200
    assert body_mp["validated"] == {"username": "charlie", "remember": False}


@pytest.mark.asyncio
async def test_val6_narrowed_di_fallback_does_not_mask_init_typeerror(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenInitRequest(FormRequest):
        schema: ClassVar[dict[str, Any]] = {"name": "required|str"}

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            raise TypeError("real bug inside FormRequest.__init__")

    # Direct resolution propagates internal TypeError instead of masking via DI fallback
    with pytest.raises(TypeError, match="real bug inside FormRequest.__init__"):
        BrokenInitRequest.resolve_instance()

    app = Application()

    @app.post("/broken")
    async def broken(form: BrokenInitRequest) -> Response:
        return json_response({"ok": True})

    # Through HTTP pipeline, internal TypeError surfaces as 500 unhandled exception (not DI fallback)
    status, _ = await _simulate_json_request(app, "POST", "/broken", {"name": "Alice"})
    assert status == 500
    assert "real bug inside FormRequest.__init__" in caplog.text


@pytest.mark.asyncio
async def test_val6_reject_set_rules() -> None:
    with pytest.raises((TypeError, ConfigurationError)):
        await validate({"x": "val"}, {"x": {"required", "int"}})


@pytest.mark.asyncio
async def test_val6_bail_and_wildcard_validation() -> None:
    calls: list[str] = []

    class RecordingMinRule:
        def __init__(self, label: str, ok: bool) -> None:
            self.label = label
            self.ok = ok

        def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
            calls.append(self.label)
            return self.ok

        def message(self, field: str) -> str:
            return f"{field} failed {self.label}"

    # Bail stops on first failure for the field
    res_bail = await validate(
        {"code": "x"},
        {
            "code": [
                "bail",
                RecordingMinRule("first", False),
                RecordingMinRule("second", False),
            ]
        },
    )
    assert res_bail.fails()
    assert calls == ["first"]
    assert res_bail.errors["code"] == ["code failed first"]

    # Wildcard array validation: items.* and nested users.*.email
    res_wild_ok = await validate(
        {
            "items": ["1", "2", "3"],
            "users": [{"email": "  ALICE@EXAMPLE.COM  "}, {"email": "BOB@EXAMPLE.COM"}],
        },
        {
            "items": "required",
            "items.*": "required|int|min:1",
            "users.*.email": "required|email:lowercase",
        },
    )
    assert res_wild_ok.passes()
    assert res_wild_ok.validated_data["items"] == [1, 2, 3]
    assert res_wild_ok.validated_data["users"] == [
        {"email": "alice@example.com"},
        {"email": "bob@example.com"},
    ]

    res_wild_fail = await validate(
        {"items": ["10", "x"]},
        {"items.*": "required|int"},
    )
    assert res_wild_fail.fails()
    assert "items.1" in res_wild_fail.errors

    assert isinstance(bail(), BailRule)


@pytest.mark.asyncio
async def test_val6_rule_invoker_cache_does_not_retain_dynamic_classes_or_collide() -> None:
    def _make_rule_cls(tag: str) -> type:
        class _DynamicRule:
            def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
                return bool(value == tag)

            def message(self, field: str) -> str:
                return f"expected {tag}"

        return _DynamicRule

    dyn_cls = _make_rule_cls("alpha")
    dyn_ref = weakref.ref(dyn_cls)
    res_ok = await validate({"x": "alpha"}, {"x": [dyn_cls()]})
    assert res_ok.passes()

    class _BaseRule:
        def passes(self, field: str, value: Any) -> bool:
            return bool(value == "base")

    class _SubAsyncRule(_BaseRule):
        async def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
            return bool(data.get("allow") is True and value == "sub")

    res_sub = await validate(
        {"x": "sub", "allow": True},
        {"x": [_SubAsyncRule()]},
    )
    assert res_sub.passes()

    del dyn_cls
    gc.collect()
    assert dyn_ref() is None
