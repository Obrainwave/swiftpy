from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any

from swiftpy.database.transaction import get_active_connection
from swiftpy.validation.rules.builtin import (
    BoolRule,
    EmailRule,
    FloatRule,
    IntRule,
    StrRule,
)
from swiftpy.validation.rules.registry import RuleRegistry, default_registry
from swiftpy.validation.rules.required import NullableRule, RequiredRule

_RULE_INVOKER_CACHE: dict[type, tuple[int, bool]] = {}


def _get_rule_invoker_info(rule: Any) -> tuple[int, bool]:
    """Precompute and cache rule method signature metadata."""
    cls = rule.__class__
    if cls in _RULE_INVOKER_CACHE:
        return _RULE_INVOKER_CACHE[cls]
    method = getattr(rule, "validate", None)
    if method is None or not callable(method):
        method = getattr(rule, "passes", None)
    if method is None or not callable(method):
        raise AttributeError(
            f"Rule {cls.__name__} must implement 'validate' or 'passes'"
        )
    sig = inspect.signature(method)
    param_count = len(sig.parameters)
    is_async = inspect.iscoroutinefunction(method)
    info = (param_count, is_async)
    _RULE_INVOKER_CACHE[cls] = info
    return info


def _rule_sort_key(rule: Any) -> int:
    """Sort rules so type coercion executes before constraint evaluation."""
    if isinstance(rule, (RequiredRule, NullableRule)):
        return 0
    if isinstance(rule, (IntRule, FloatRule, BoolRule, StrRule, EmailRule)):
        return 1
    return 2


@dataclass(slots=True)
class ValidationResult:
    """
    Typed result returned by validate(data, schema).
    """

    is_valid: bool
    errors: dict[str, list[str]] = field(default_factory=dict)
    validated_data: dict[str, Any] = field(default_factory=dict)

    def passes(self) -> bool:
        """Return True if all validation rules passed."""
        return self.is_valid

    def fails(self) -> bool:
        """Return True if any validation rule failed."""
        return not self.is_valid

    def __bool__(self) -> bool:
        """Evaluate result truthiness based on validity."""
        return self.is_valid

    def first_error(self, field_name: str | None = None) -> str | None:
        """
        Return the first error message for a specific field or across all fields.
        """
        if field_name is not None:
            field_errors = self.errors.get(field_name)
            return field_errors[0] if field_errors else None

        for field_errors in self.errors.values():
            if field_errors:
                return field_errors[0]
        return None

    def errors_for(self, field_name: str) -> list[str]:
        """Return all error messages for a specific field."""
        return list(self.errors.get(field_name, []))

    def all_errors(self) -> list[str]:
        """Return a flattened list of all error messages."""
        flattened: list[str] = []
        for field_errors in self.errors.values():
            flattened.extend(field_errors)
        return flattened

    def validated(self) -> dict[str, Any]:
        """Return a copy of the cleaned/coerced validated data."""
        return dict(self.validated_data)


class Validator:
    """
    Core validation engine. Parses schemas, coordinates rule execution,
    supports sync/async custom rules, and returns a typed ValidationResult.
    """

    def __init__(
        self,
        data: dict[str, Any] | Any,
        schema: dict[str, Any],
        registry: RuleRegistry | None = None,
    ) -> None:
        self.is_payload_dict = isinstance(data, dict)
        self.data: dict[str, Any] = dict(data) if isinstance(data, dict) else {}
        self.schema = schema
        self.registry = registry or default_registry
        self._db_rule_lock = asyncio.Lock()

    async def validate(self) -> ValidationResult:
        """
        Execute validation across all schema rules non-blockingly.
        Fields are validated concurrently via asyncio.gather outside transactions,
        or sequentially when an active transaction is present (P3-D4).
        """
        if not self.is_payload_dict:
            return ValidationResult(
                is_valid=False,
                errors={"body": ["The request body must be a JSON object."]},
                validated_data={},
            )

        parsed_schema: dict[str, list[Any]] = {}
        for field_name, rule_def in self.schema.items():
            if isinstance(rule_def, list) and all(
                not isinstance(r, str)
                and (hasattr(r, "validate") or hasattr(r, "passes"))
                for r in rule_def
            ):
                parsed_schema[field_name] = rule_def
            else:
                parsed_schema[field_name] = self.registry.parse_rules(rule_def)

        errors: dict[str, list[str]] = {}
        validated_data: dict[str, Any] = {}

        if get_active_connection() is not None:
            results = [
                await self._validate_field(field_name, rules)
                for field_name, rules in parsed_schema.items()
            ]
        else:
            tasks = [
                self._validate_field(field_name, rules)
                for field_name, rules in parsed_schema.items()
            ]
            results = list(await asyncio.gather(*tasks))

        for field_name, field_errors, coerced_val in results:
            if field_errors:
                errors[field_name] = field_errors
            elif coerced_val is not _UNSET:
                validated_data[field_name] = coerced_val

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            validated_data=validated_data,
        )

    async def _validate_field(
        self, field_name: str, rules: list[Any]
    ) -> tuple[str, list[str], Any]:
        field_errors: list[str] = []

        is_nullable = any(
            isinstance(r, NullableRule) or getattr(r, "is_nullable_rule", False)
            for r in rules
        )
        is_required = any(isinstance(r, RequiredRule) for r in rules)

        field_present = field_name in self.data
        raw_value = self.data.get(field_name)

        # 1. Field is absent
        if not field_present:
            if is_required:
                req_rule = next(r for r in rules if isinstance(r, RequiredRule))
                field_errors.append(req_rule.message(field_name))
            return field_name, field_errors, _UNSET

        # 2. Field is present and null / empty string
        if (raw_value is None or raw_value == "") and is_nullable:
            return field_name, [], raw_value

        coerced_value = raw_value

        # Sort rules so type coercion executes before magnitude/constraint rules
        sorted_rules = sorted(rules, key=_rule_sort_key)

        # 3. Evaluate rules sequentially for this field
        for rule in sorted_rules:
            if isinstance(rule, NullableRule):
                continue

            if (
                getattr(rule, "requires_db", False)
                and get_active_connection() is not None
            ):
                async with self._db_rule_lock:
                    passed = await self._run_rule(rule, field_name, coerced_value)
            else:
                passed = await self._run_rule(rule, field_name, coerced_value)
            if not passed:
                msg = self._get_rule_message(rule, field_name)
                field_errors.append(msg)
                # If required rule failed, stop further rules on this field
                if isinstance(rule, RequiredRule):
                    break
            else:
                # Apply coercion if rule is type rule
                if isinstance(rule, IntRule) and isinstance(coerced_value, str):
                    try:
                        coerced_value = int(coerced_value.strip())
                    except ValueError:
                        pass
                elif isinstance(rule, FloatRule) and isinstance(coerced_value, str):
                    try:
                        coerced_value = float(coerced_value.strip())
                    except ValueError:
                        pass
                elif isinstance(rule, BoolRule) and not isinstance(coerced_value, bool):
                    str_val = str(coerced_value).lower()
                    if str_val in ("true", "1"):
                        coerced_value = True
                    elif str_val in ("false", "0"):
                        coerced_value = False
                elif isinstance(rule, EmailRule) and isinstance(coerced_value, str):
                    coerced_value = rule.normalize(coerced_value)

        return field_name, field_errors, coerced_value

    async def _run_rule(self, rule: Any, field_name: str, value: Any) -> bool:
        """Invoke rule's validate or passes method safely handling sync and async."""
        method = getattr(rule, "validate", None)
        if method is None or not callable(method):
            method = getattr(rule, "passes", None)

        if method is None or not callable(method):
            raise AttributeError(
                f"Rule {rule.__class__.__name__} must implement 'validate' or 'passes'"
            )

        param_count, is_async = _get_rule_invoker_info(rule)

        if param_count <= 2:
            res = method(field_name, value)
        else:
            res = method(field_name, value, self.data)

        if is_async or inspect.isawaitable(res):
            return bool(await res)
        return bool(res)

        if inspect.isawaitable(res):
            return bool(await res)
        return bool(res)

    def _get_rule_message(self, rule: Any, field_name: str) -> str:
        """Extract the error message from the rule."""
        if hasattr(rule, "message"):
            attr = rule.message
            if callable(attr):
                try:
                    return str(attr(field_name))
                except TypeError:
                    return str(attr())
            return str(attr)
        return f"The {field_name} field is invalid."


_UNSET = object()


async def validate(
    data: dict[str, Any] | Any,
    schema: dict[str, Any],
    registry: RuleRegistry | None = None,
) -> ValidationResult:
    """
    Validate data against a schema non-blockingly, returning a typed ValidationResult.
    """
    validator = Validator(data=data, schema=schema, registry=registry)
    return await validator.validate()


__all__ = ["ValidationResult", "Validator", "validate"]
