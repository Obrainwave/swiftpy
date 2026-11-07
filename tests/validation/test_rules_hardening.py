from __future__ import annotations

import uuid

import pytest

from swiftpy.core.config import ConfigurationError
from swiftpy.database.pool import DatabasePool
from swiftpy.validation.rules.builtin import (
    BetweenRule,
    EmailRule,
    FloatRule,
    InRule,
    IntRule,
    NotInRule,
    RegexRule,
    UrlRule,
)
from swiftpy.validation.rules.exists import ExistsRule
from swiftpy.validation.rules.registry import RuleRegistry, default_registry
from swiftpy.validation.rules.unique import UniqueRule
from swiftpy.validation.validator import validate


def test_int_rule_rejects_superscripts_and_unicode_digits() -> None:
    rule = IntRule()
    # Superscript digits must be rejected (ASCII-only regex)
    assert not rule.validate("field", "²", {})
    assert not rule.validate("field", "³", {})
    assert not rule.validate("field", "¹", {})
    assert not rule.validate("field", "½", {})

    # Booleans rejected
    assert not rule.validate("field", True, {})
    assert not rule.validate("field", False, {})

    # Floats rejected
    assert not rule.validate("field", 3.14, {})
    assert not rule.validate("field", "3.14", {})

    # Valid integers and signed integer strings
    assert rule.validate("field", 42, {})
    assert rule.validate("field", 0, {})
    assert rule.validate("field", -17, {})
    assert rule.validate("field", "42", {})
    assert rule.validate("field", "  42  ", {})
    assert rule.validate("field", "+12", {})
    assert rule.validate("field", "-99", {})


def test_float_rule_rejects_nan_and_infinities() -> None:
    rule = FloatRule()
    # NaN and infinities must be rejected
    assert not rule.validate("field", float("nan"), {})
    assert not rule.validate("field", float("inf"), {})
    assert not rule.validate("field", float("-inf"), {})
    assert not rule.validate("field", "NaN", {})
    assert not rule.validate("field", "nan", {})
    assert not rule.validate("field", "Infinity", {})
    assert not rule.validate("field", "-Infinity", {})
    assert not rule.validate("field", "inf", {})
    assert not rule.validate("field", "-inf", {})

    # Booleans rejected
    assert not rule.validate("field", True, {})
    assert not rule.validate("field", False, {})

    # Superscripts rejected
    assert not rule.validate("field", "²", {})

    # Valid finite numbers
    assert rule.validate("field", 3.14, {})
    assert rule.validate("field", 0.0, {})
    assert rule.validate("field", -42.5, {})
    assert rule.validate("field", 100, {})
    assert rule.validate("field", "3.14", {})
    assert rule.validate("field", "-0.01", {})
    assert rule.validate("field", "+1e-3", {})
    assert rule.validate("field", ".5", {})


def test_email_rule_normalization_and_strict_rfc() -> None:
    rule = EmailRule()

    # Normalization: domain lowercased, whitespace stripped, local part case preserved
    raw = "  Alice.Smith@EXAMPLE.COM  "
    assert rule.validate("email", raw, {})
    normalized = rule.normalize(raw)
    assert normalized == "Alice.Smith@example.com"

    # Optional lowercase option
    lc_rule = EmailRule(lowercase=True)
    assert lc_rule.normalize(raw) == "alice.smith@example.com"

    # Consecutive dots rejected
    assert not rule.validate("email", "alice..smith@example.com", {})

    # Leading/trailing dots in local part rejected
    assert not rule.validate("email", ".alice@example.com", {})
    assert not rule.validate("email", "alice.@example.com", {})

    # Local part <= 64 chars
    valid_local = "a" * 64 + "@example.com"
    invalid_local = "a" * 65 + "@example.com"
    assert rule.validate("email", valid_local, {})
    assert not rule.validate("email", invalid_local, {})

    # Total <= 254 chars
    domain_part = "b" * 60 + "." + "c" * 60 + ".com"
    long_email = "a" * 64 + "@" + domain_part
    assert len(long_email) < 254
    assert rule.validate("email", long_email, {})
    way_too_long = "a" * 64 + "@" + ("b" * 60 + ".") * 4 + "com"
    assert len(way_too_long) > 254
    assert not rule.validate("email", way_too_long, {})

    # No domain dot rejected
    assert not rule.validate("email", "user@localhost", {})


def test_url_rule_max_length_2048() -> None:
    rule = UrlRule()
    valid_url = "https://example.com/api?query=1"
    assert rule.validate("url", valid_url, {})

    # 2048 chars valid
    long_path = "a" * (2048 - len("https://example.com/"))
    assert rule.validate("url", f"https://example.com/{long_path}", {})

    # Over 2048 chars rejected
    too_long = f"https://example.com/{long_path}x"
    assert len(too_long) == 2049
    assert not rule.validate("url", too_long, {})

    # Non-http/https schemes rejected
    assert not rule.validate("url", "ftp://example.com", {})
    assert not rule.validate("url", "javascript:alert(1)", {})
    assert not rule.validate("url", "http://", {})


def test_in_and_not_in_strict_type_comparison() -> None:
    # Testing True vs 1 vs "True"
    rule_ints = InRule([1, 2, 3])
    # In Python, isinstance(True, int) and True == 1, but InRule must strictly reject True
    assert not rule_ints.validate("field", True, {})
    assert not rule_ints.validate("field", False, {})
    assert rule_ints.validate("field", 1, {})
    assert rule_ints.validate("field", "1", {})
    assert not rule_ints.validate("field", "True", {})

    rule_bools = InRule([True])
    assert rule_bools.validate("field", True, {})
    assert not rule_bools.validate("field", 1, {})
    assert not rule_bools.validate("field", "1", {})
    assert not rule_bools.validate("field", "True", {})

    rule_str = InRule(["True", "yes"])
    assert not rule_str.validate("field", True, {})
    assert rule_str.validate("field", "True", {})

    not_in_ints = NotInRule([1, 2])
    assert not_in_ints.validate("field", True, {})
    assert not not_in_ints.validate("field", 1, {})


def test_regex_rule_escaped_pipe_and_re_error() -> None:
    registry = RuleRegistry()

    # Escaped pipe in rule string
    rules = registry.parse_rules(r"required|regex:^(a\|b)+$")
    assert len(rules) == 2
    regex_rule = rules[1]
    assert isinstance(regex_rule, RegexRule)
    assert regex_rule.validate("field", "a", {})
    assert regex_rule.validate("field", "b", {})
    assert regex_rule.validate("field", "ab", {})
    assert not regex_rule.validate("field", "c", {})

    # Escaped colon in rule string
    rules_colon = registry.parse_rules(r"regex:^(a\:b)+$")
    assert len(rules_colon) == 1
    assert rules_colon[0].validate("field", "a:b", {})

    # List of rules without escaping
    rules_list = registry.parse_rules(["required", "regex:^(a|b)+$"])
    assert len(rules_list) == 2
    assert rules_list[1].validate("field", "a", {})
    assert rules_list[1].validate("field", "b", {})
    assert not rules_list[1].validate("field", "c", {})

    # Invalid regex pattern does not crash with 500, returns False
    invalid_regex = RegexRule("[unclosed")
    assert not invalid_regex.validate("field", "anything", {})


def test_between_rule_missing_arg_raises_configuration_error() -> None:
    registry = RuleRegistry()
    with pytest.raises(ConfigurationError, match="between rule requires min and max"):
        registry.parse_rules("between:1")

    # Valid between parses fine
    rules = registry.parse_rules("between:1,10")
    assert len(rules) == 1
    assert isinstance(rules[0], BetweenRule)
    assert rules[0].validate("num", 5, {})


@pytest.mark.asyncio
async def test_email_coercion_in_validator() -> None:
    result = await validate(
        {"email": "  USER@DOMAIN.COM  "},
        {"email": "required|email"},
    )
    assert result.passes()
    # Normalized email stored in validated_data
    assert result.validated_data["email"] == "USER@domain.com"


@pytest.mark.asyncio
async def test_unique_case_insensitive(ensure_db_pool: DatabasePool) -> None:
    table_name = f"test_users_{uuid.uuid4().hex[:8]}"
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            DROP TABLE IF EXISTS "{table_name}" CASCADE;
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL
            );
            INSERT INTO "{table_name}" (email) VALUES ('existing@example.com');
            """
        )

    try:
        # Standard case-sensitive UniqueRule: 'EXISTING@example.com' would pass
        rule_cs = UniqueRule(table=table_name, column="email", pool=ensure_db_pool, case_insensitive=False)
        assert await rule_cs.validate("email", "EXISTING@example.com", {})

        # Case-insensitive UniqueRule: 'EXISTING@example.com' must fail
        rule_ci = UniqueRule(table=table_name, column="email", pool=ensure_db_pool, case_insensitive=True)
        assert not await rule_ci.validate("email", "EXISTING@example.com", {})
        assert await rule_ci.validate("email", "other@example.com", {})

        # String syntax parsing: unique:table,email,ci
        parsed_rule = default_registry.parse(f"unique:{table_name},email,ci")
        assert isinstance(parsed_rule, UniqueRule)
        assert parsed_rule.case_insensitive is True
        assert not await parsed_rule.validate("email", "EXISTING@example.com", {})
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{table_name}" CASCADE;')


@pytest.mark.asyncio
async def test_data_error_in_unique_and_exists_results_in_validation_failure(
    ensure_db_pool: DatabasePool,
) -> None:
    table_name = f"test_int_items_{uuid.uuid4().hex[:8]}"
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            DROP TABLE IF EXISTS "{table_name}" CASCADE;
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                counter INT NOT NULL
            );
            """
        )

    try:
        # Passing a non-numeric string 'abc' into an integer column triggers asyncpg.DataError
        unique_rule = UniqueRule(table=table_name, column="counter", pool=ensure_db_pool)
        exists_rule = ExistsRule(table=table_name, column="counter", pool=ensure_db_pool)

        # Must catch DataError and return False (normal validation failure) rather than 500 crash
        assert not await unique_rule.validate("counter", "abc", {})
        assert not await exists_rule.validate("counter", "abc", {})
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{table_name}" CASCADE;')
