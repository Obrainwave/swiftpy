from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from swiftpy.validation import (
    BetweenRule,
    BoolRule,
    EmailRule,
    FloatRule,
    InRule,
    IntRule,
    MaxRule,
    MinRule,
    NotInRule,
    NullableRule,
    RegexRule,
    RequiredRule,
    StrRule,
    UrlRule,
    ValidationResult,
    ValidatorRule,
    between,
    bool_,
    email,
    float_,
    in_,
    int_,
    max_,
    min_,
    not_in,
    nullable,
    regex,
    register_rule,
    required,
    str_,
    url,
    validate,
)


@pytest.mark.asyncio
async def test_required_rule() -> None:
    rule = RequiredRule()
    # Missing field
    assert rule.validate("name", None, {}) is False
    # None value
    assert rule.validate("name", None, {"name": None}) is False
    # Empty string
    assert rule.validate("name", "", {"name": ""}) is False
    # Whitespace only
    assert rule.validate("name", "   ", {"name": "   "}) is False
    # Empty collections
    assert rule.validate("tags", [], {"tags": []}) is False
    assert rule.validate("meta", {}, {"meta": {}}) is False
    # Valid values
    assert rule.validate("name", "Alice", {"name": "Alice"}) is True
    assert rule.validate("age", 0, {"age": 0}) is True
    assert rule.validate("active", False, {"active": False}) is True
    assert rule.validate("tags", ["python"], {"tags": ["python"]}) is True
    assert rule.message("name") == "The name field is required."


@pytest.mark.asyncio
async def test_nullable_rule() -> None:
    assert isinstance(nullable(), NullableRule)
    schema = {"bio": [nullable(), str_(), min_(5)]}

    # Present and None passes immediately (subsequent rules skipped)
    res_none = await validate({"bio": None}, schema)
    assert res_none.is_valid is True
    assert res_none.validated_data == {"bio": None}

    # Present and empty string passes immediately
    res_empty = await validate({"bio": ""}, schema)
    assert res_empty.is_valid is True

    # Present and non-null must satisfy subsequent rules
    res_invalid = await validate({"bio": "hey"}, schema)
    assert res_invalid.is_valid is False
    assert "bio" in res_invalid.errors

    res_valid = await validate({"bio": "Software developer"}, schema)
    assert res_valid.is_valid is True


@pytest.mark.asyncio
async def test_str_rule() -> None:
    rule = StrRule()
    assert rule.validate("name", "hello", {}) is True
    assert rule.validate("name", "", {}) is True
    assert rule.validate("name", 123, {}) is False
    assert rule.validate("name", True, {}) is False
    assert rule.validate("name", ["a"], {}) is False
    assert rule.message("name") == "The name field must be a string."


@pytest.mark.asyncio
async def test_int_rule() -> None:
    rule = IntRule()
    assert rule.validate("age", 25, {}) is True
    assert rule.validate("age", -5, {}) is True
    assert rule.validate("age", "42", {}) is True
    assert rule.validate("age", "-10", {}) is True
    assert rule.validate("age", True, {}) is False
    assert rule.validate("age", False, {}) is False
    assert rule.validate("age", 3.14, {}) is False
    assert rule.validate("age", "not_a_num", {}) is False
    assert rule.message("age") == "The age field must be an integer."


@pytest.mark.asyncio
async def test_float_rule() -> None:
    rule = FloatRule()
    assert rule.validate("price", 19.99, {}) is True
    assert rule.validate("price", 20, {}) is True
    assert rule.validate("price", "3.1415", {}) is True
    assert rule.validate("price", "-0.5", {}) is True
    assert rule.validate("price", True, {}) is False
    assert rule.validate("price", "invalid", {}) is False
    assert rule.message("price") == "The price field must be a float."


@pytest.mark.asyncio
async def test_bool_rule() -> None:
    rule = BoolRule()
    assert rule.validate("is_active", True, {}) is True
    assert rule.validate("is_active", False, {}) is True
    assert rule.validate("is_active", 1, {}) is True
    assert rule.validate("is_active", 0, {}) is True
    assert rule.validate("is_active", "true", {}) is True
    assert rule.validate("is_active", "false", {}) is True
    assert rule.validate("is_active", "1", {}) is True
    assert rule.validate("is_active", "0", {}) is True
    assert rule.validate("is_active", "yes", {}) is False
    assert rule.validate("is_active", 42, {}) is False
    assert rule.message("is_active") == "The is_active field must be a boolean."


@pytest.mark.asyncio
async def test_email_rule() -> None:
    rule = EmailRule()
    assert rule.validate("email", "user@example.com", {}) is True
    assert rule.validate("email", "john.doe+tag@sub.domain.co", {}) is True
    assert rule.validate("email", "plainaddress", {}) is False
    assert rule.validate("email", "@missinguser.com", {}) is False
    assert rule.validate("email", "user@domain..com", {}) is False
    assert rule.validate("email", "user @domain.com", {}) is False
    assert rule.validate("email", 12345, {}) is False
    assert rule.message("email") == "The email field must be a valid email address."


@pytest.mark.asyncio
async def test_url_rule() -> None:
    rule = UrlRule()
    assert rule.validate("site", "https://example.com", {}) is True
    assert rule.validate("site", "http://localhost:8000/api", {}) is True
    assert rule.validate("site", "ftp://ftp.example.com", {}) is False
    assert rule.validate("site", "just_text", {}) is False
    assert rule.validate("site", "http://", {}) is False
    assert rule.validate("site", None, {}) is False
    assert rule.message("site") == "The site field must be a valid URL."


@pytest.mark.asyncio
async def test_min_and_max_rules() -> None:
    min_r = MinRule(5)
    max_r = MaxRule(10)

    # String length
    assert min_r.validate("code", "hello", {}) is True
    assert min_r.validate("code", "hi", {}) is False
    assert max_r.validate("code", "hello", {}) is True
    assert max_r.validate("code", "supercalifragilistic", {}) is False

    # Numeric value
    assert min_r.validate("num", 5, {}) is True
    assert min_r.validate("num", 4, {}) is False
    assert max_r.validate("num", 10, {}) is True
    assert max_r.validate("num", 11, {}) is False

    # Collection size
    assert min_r.validate("items", [1, 2, 3, 4, 5], {}) is True
    assert min_r.validate("items", [1, 2], {}) is False
    assert max_r.validate("items", [1, 2, 3], {}) is True
    assert max_r.validate("items", list(range(15)), {}) is False


@pytest.mark.asyncio
async def test_between_rule() -> None:
    rule = BetweenRule(10, 20)
    # String length
    assert rule.validate("pwd", "12345678901", {}) is True
    assert rule.validate("pwd", "short", {}) is False
    assert rule.validate("pwd", "a" * 25, {}) is False
    # Numeric
    assert rule.validate("age", 15, {}) is True
    assert rule.validate("age", 9, {}) is False
    assert rule.validate("age", 21, {}) is False
    assert rule.message("age") == "The age field must be between 10 and 20."


@pytest.mark.asyncio
async def test_in_and_not_in_rules() -> None:
    in_r = InRule(["admin", "editor", "viewer"])
    not_in_r = NotInRule(["banned", "deleted"])

    assert in_r.validate("role", "admin", {}) is True
    assert in_r.validate("role", "guest", {}) is False
    assert not_in_r.validate("status", "active", {}) is True
    assert not_in_r.validate("status", "banned", {}) is False


@pytest.mark.asyncio
async def test_regex_rule() -> None:
    rule = RegexRule(r"^SKU-[0-9]{4}$")
    assert rule.validate("sku", "SKU-1234", {}) is True
    assert rule.validate("sku", "SKU-ABCD", {}) is False
    assert rule.validate("sku", "sku-1234", {}) is False
    assert rule.message("sku") == "The sku field format is invalid."


@pytest.mark.asyncio
async def test_validator_rule_protocol_and_custom_rules() -> None:
    # 1. Custom synchronous rule satisfying ValidatorRule protocol
    class EvenNumberRule:
        def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
            return isinstance(value, int) and value % 2 == 0

        def message(self, field: str) -> str:
            return f"The {field} field must be an even integer."

    even_rule = EvenNumberRule()
    assert isinstance(even_rule, ValidatorRule)

    # 2. Custom asynchronous rule satisfying ValidatorRule protocol
    class AsyncExternalCheckRule:
        async def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
            await asyncio.sleep(0.01)
            return bool(value == "valid_token")

        def message(self, field: str) -> str:
            return f"The {field} verification failed."

    async_rule = AsyncExternalCheckRule()
    assert isinstance(async_rule, ValidatorRule)

    # Validate with custom rules
    schema = {
        "even_val": [required(), even_rule],
        "token": [required(), async_rule],
    }

    res_valid = await validate({"even_val": 4, "token": "valid_token"}, schema)
    assert res_valid.is_valid is True
    assert res_valid.passes() is True
    assert res_valid.fails() is False

    res_invalid = await validate({"even_val": 5, "token": "bad_token"}, schema)
    assert res_invalid.is_valid is False
    assert (
        "The even_val field must be an even integer." in res_invalid.errors["even_val"]
    )
    assert "The token verification failed." in res_invalid.errors["token"]


@pytest.mark.asyncio
async def test_string_schema_syntax_and_registration() -> None:
    # Register custom rule in registry
    class StartsWithRule:
        def __init__(self, prefix: str) -> None:
            self.prefix = prefix

        def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
            return isinstance(value, str) and value.startswith(self.prefix)

        def message(self, field: str) -> str:
            return f"The {field} field must start with '{self.prefix}'."

    register_rule("starts_with", lambda p: StartsWithRule(p))

    schema = {
        "username": "required|str|min:3|max:15",
        "email": "required|email",
        "age": "required|int|between:18,65",
        "role": "required|in:admin,member",
        "code": "required|starts_with:DEV_",
    }

    valid_payload = {
        "username": "alice",
        "email": "alice@example.com",
        "age": "25",
        "role": "admin",
        "code": "DEV_99",
    }

    result = await validate(valid_payload, schema)
    assert result.is_valid is True
    # Coercion verification
    assert result.validated_data["age"] == 25
    assert result.validated()["username"] == "alice"

    invalid_payload = {
        "username": "al",
        "email": "not-an-email",
        "age": 10,
        "role": "guest",
        "code": "PROD_1",
    }

    bad_result = await validate(invalid_payload, schema)
    assert bad_result.is_valid is False
    assert len(bad_result.errors) == 5
    assert bad_result.first_error("username") is not None
    assert "email" in bad_result.errors_for("email")[0]
    assert len(bad_result.all_errors()) >= 5


@pytest.mark.asyncio
async def test_python_types_in_schema() -> None:
    # User can supply Python types directly: str, int, float, bool
    schema = {
        "title": [required, str],
        "count": [required, int, min_(1)],
        "rate": [required, float],
    }

    res = await validate(
        {"title": "Test Title", "count": 5, "rate": 4.5},
        schema,
    )
    assert res.is_valid is True
    assert bool(res) is True


@pytest.mark.asyncio
async def test_factory_helpers_and_validation_result_methods() -> None:
    schema = {
        "text": [required(), str_(), min_(2), max_(20)],
        "count": [int_(), between(1, 10)],
        "ratio": [float_()],
        "status": [bool_()],
        "contact": [email()],
        "website": [url()],
        "category": [in_(["tech", "art"]), not_in(["spam"])],
        "pattern": [regex(r"^[A-Z]+$")],
        "opt": [nullable()],
    }

    payload = {
        "text": "Valid text",
        "count": 5,
        "ratio": 1.25,
        "status": True,
        "contact": "test@example.com",
        "website": "https://swiftpy.dev",
        "category": "tech",
        "pattern": "HELLO",
        "opt": None,
    }

    result = await validate(payload, schema)
    assert isinstance(result, ValidationResult)
    assert result.passes() is True
    assert result.fails() is False
    assert result.errors == {}
    assert result.first_error() is None
    assert result.all_errors() == []
    assert result.errors_for("text") == []
    assert result.validated()["text"] == "Valid text"


@pytest.mark.asyncio
async def test_required_distinguishes_missing_null_and_empty() -> None:
    schema = {"name": [required()]}
    missing = await validate({}, schema)
    explicit_null = await validate({"name": None}, schema)
    empty = await validate({"name": ""}, schema)
    whitespace = await validate({"name": "   "}, schema)

    assert missing.is_valid is False
    assert explicit_null.is_valid is False
    assert empty.is_valid is False
    assert whitespace.is_valid is False

    for res in (missing, explicit_null, empty, whitespace):
        assert "name" in res.errors
        assert res.first_error("name") == "The name field is required."

    # Optional field that is missing should pass
    optional_schema = {"bio": ["nullable", "str"]}
    opt_missing = await validate({}, optional_schema)
    assert opt_missing.is_valid is True
    assert "bio" not in opt_missing.validated_data


@pytest.mark.asyncio
async def test_nullable_skips_subsequent_rules_with_tracking() -> None:
    calls: list[str] = []

    class TrackingRule:
        def validate(
            self,
            field: str,
            value: Any,
            data: dict[str, Any],
        ) -> bool:
            calls.append(field)
            return False

        def message(self, field: str) -> str:
            return "Tracking rule failed."

    # 1. Null value skips tracking rule
    res_null = await validate(
        {"bio": None},
        {"bio": [nullable(), TrackingRule()]},
    )
    assert res_null.is_valid is True
    assert calls == []

    # 2. Empty string skips tracking rule
    res_empty = await validate(
        {"bio": ""},
        {"bio": [nullable(), TrackingRule()]},
    )
    assert res_empty.is_valid is True
    assert calls == []

    # 3. Non-null value invokes tracking rule
    res_val = await validate(
        {"bio": "developer"},
        {"bio": [nullable(), TrackingRule()]},
    )
    assert res_val.is_valid is False
    assert calls == ["bio"]


@pytest.mark.asyncio
async def test_integer_rule_rejects_ambiguous_and_coerces() -> None:
    schema = {"value": [int_()]}
    # Reject ambiguous types and values
    for val in (
        True,
        False,
        3.0,
        "3.0",
        "abc",
        float("nan"),
        float("inf"),
        None,
        "",
    ):
        res = await validate({"value": val}, schema)
        assert res.is_valid is False, f"Unexpectedly accepted {val!r}"

    # Accept and coerce integer strings
    res_pos = await validate({"value": "42"}, schema)
    assert res_pos.is_valid is True
    assert res_pos.validated_data["value"] == 42
    assert type(res_pos.validated_data["value"]) is int

    res_neg = await validate({"value": "-15"}, schema)
    assert res_neg.is_valid is True
    assert res_neg.validated_data["value"] == -15
    assert type(res_neg.validated_data["value"]) is int


@pytest.mark.asyncio
async def test_float_rule_rejects_non_finite_values() -> None:
    schema = {"price": [float_()]}
    # Rejects booleans and non-finite numbers
    for val in (
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        "nan",
        "inf",
        "-inf",
        "invalid",
    ):
        res = await validate({"price": val}, schema)
        assert res.is_valid is False, f"Unexpectedly accepted {val!r}"

    # Valid floats coerce properly
    res_valid = await validate({"price": "19.95"}, schema)
    assert res_valid.is_valid is True
    assert math.isclose(res_valid.validated_data["price"], 19.95)
    assert type(res_valid.validated_data["price"]) is float


@pytest.mark.asyncio
async def test_bool_rule_complete_tokens() -> None:
    schema = {"flag": [bool_()]}

    # Accepted tokens
    for token in (True, False, 1, 0, "true", "false", "True", "False", "1", "0"):
        res = await validate({"flag": token}, schema)
        assert res.is_valid is True, f"Failed on valid token {token!r}"
        assert type(res.validated_data["flag"]) is bool

    # Rejected tokens
    rejected_tokens: list[Any] = ["yes", "no", 2, -1, 3.14, None, "maybe", [], {}]
    for bad in rejected_tokens:
        res = await validate({"flag": bad}, schema)
        assert res.is_valid is False, f"Unexpectedly accepted {bad!r}"


@pytest.mark.asyncio
async def test_between_rule_guards_and_boundaries() -> None:
    # Inverted range must raise ValueError upfront
    with pytest.raises(ValueError, match="min_val .* cannot be greater than max_val"):
        BetweenRule(20, 10)

    rule = BetweenRule(10, 20)
    # Exact boundary checks
    assert rule.validate("num", 10, {}) is True
    assert rule.validate("num", 20, {}) is True
    assert rule.validate("num", 9.99, {}) is False
    assert rule.validate("num", 20.01, {}) is False

    # Unsupported type returns False
    assert rule.validate("obj", object(), {}) is False


@pytest.mark.asyncio
async def test_custom_rule_duck_typing_and_fallbacks() -> None:
    # 1. Custom rule with passes() only (no validate method)
    class PassesOnlyRule:
        def passes(self, field: str, value: Any, data: dict[str, Any]) -> bool:
            return bool(value == "secret")

        def message(self, field: str) -> str:
            return "Passes-only rule rejected value."

    res_pass = await validate({"code": "secret"}, {"code": [PassesOnlyRule()]})
    assert res_pass.is_valid is True

    res_fail = await validate({"code": "wrong"}, {"code": [PassesOnlyRule()]})
    assert res_fail.is_valid is False
    first_err = res_fail.first_error("code")
    assert first_err is not None and "Passes-only" in first_err

    # 2. Custom rule with string message attribute instead of callable
    class StringMessageRule:
        message = "Constant error message."

        def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
            return False

    res_msg = await validate({"x": 1}, {"x": [StringMessageRule()]})
    assert res_msg.is_valid is False
    assert res_msg.first_error("x") == "Constant error message."

    # 3. Custom rule without any message attribute -> default fallback
    class NoMessageRule:
        def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
            return False

    res_fallback = await validate({"x": 1}, {"x": [NoMessageRule()]})
    assert res_fallback.is_valid is False
    assert res_fallback.first_error("x") == "The x field is invalid."
