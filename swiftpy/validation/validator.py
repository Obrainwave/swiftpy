from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Self
from weakref import WeakKeyDictionary

from swiftpy.database.transaction import get_active_connection
from swiftpy.http.request import Request
from swiftpy.validation.exceptions import (
    ValidationException,
    _extract_first_error,
    _extract_first_errors,
)
from swiftpy.validation.rules.builtin import (
    BailRule,
    BetweenRule,
    BoolRule,
    EmailRule,
    FloatRule,
    InRule,
    IntRule,
    MaxRule,
    MinRule,
    NotInRule,
    RegexRule,
    StrRule,
    UrlRule,
)
from swiftpy.validation.rules.exists import ExistsRule
from swiftpy.validation.rules.registry import RuleRegistry, default_registry
from swiftpy.validation.rules.required import NullableRule, RequiredRule
from swiftpy.validation.rules.unique import UniqueRule

_RULE_INVOKER_CACHE: WeakKeyDictionary[
    type, dict[tuple[str, Any], tuple[int, bool]]
] = WeakKeyDictionary()

_RULE_ALIAS_MAP: dict[type[Any], tuple[str, ...]] = {
    RequiredRule: ("required",),
    NullableRule: ("nullable",),
    BailRule: ("bail",),
    StrRule: ("str", "string"),
    EmailRule: ("email",),
    IntRule: ("int", "integer"),
    FloatRule: ("float",),
    BoolRule: ("bool", "boolean"),
    UrlRule: ("url",),
    MinRule: ("min",),
    MaxRule: ("max",),
    BetweenRule: ("between",),
    InRule: ("in", "in_"),
    NotInRule: ("not_in",),
    RegexRule: ("regex",),
    UniqueRule: ("unique",),
    ExistsRule: ("exists",),
}


def _get_rule_invoker_info(rule: Any) -> tuple[int, bool]:
    """Precompute and cache rule method signature metadata keyed safely by class and callable."""
    cls = rule.__class__
    method_name = "validate"
    method = getattr(rule, "validate", None)
    if method is None or not callable(method):
        method_name = "passes"
        method = getattr(rule, "passes", None)
    if method is None or not callable(method):
        raise AttributeError(
            f"Rule {cls.__name__} must implement 'validate' or 'passes'"
        )

    func_key: Any = getattr(method, "__func__", method)
    cls_cache: dict[tuple[str, Any], tuple[int, bool]] | None = None
    try:
        cls_cache = _RULE_INVOKER_CACHE.get(cls)
        if cls_cache is not None:
            cached = cls_cache.get((method_name, func_key))
            if cached is not None:
                return cached
    except TypeError:
        cls_cache = None

    sig = inspect.signature(method)
    param_count = len(sig.parameters)
    is_async = inspect.iscoroutinefunction(method)
    info = (param_count, is_async)
    try:
        if cls_cache is None:
            cls_cache = {}
            _RULE_INVOKER_CACHE[cls] = cls_cache
        cls_cache[(method_name, func_key)] = info
    except TypeError:
        pass
    return info


def _rule_sort_key(rule: Any) -> tuple[int, int]:
    """Sort rules canonically so type coercion runs deterministically before constraint evaluation."""
    if isinstance(rule, RequiredRule):
        return (0, 0)
    if isinstance(rule, NullableRule):
        return (0, 1)
    if isinstance(rule, BailRule) or getattr(rule, "is_bail_rule", False):
        return (0, 2)
    if isinstance(rule, StrRule):
        return (1, 0)
    if isinstance(rule, EmailRule):
        return (1, 1)
    if isinstance(rule, IntRule):
        return (1, 2)
    if isinstance(rule, FloatRule):
        return (1, 3)
    if isinstance(rule, BoolRule):
        return (1, 4)
    return (2, 0)


def _to_validated_value(value: Any) -> Any:
    if isinstance(value, dict) and not isinstance(value, ValidatedData):
        return ValidatedData(value)
    if isinstance(value, list):
        return [_to_validated_value(item) for item in value]
    return value


class ValidatedData(dict[str, Any]):
    """Dictionary subclass supporting attribute access (`data.field`) for validated payloads."""

    __slots__ = ()

    def __init__(
        self,
        mapping: Mapping[str, Any] | Iterable[tuple[str, Any]] = (),
        **kwargs: Any,
    ) -> None:
        raw = dict(mapping)
        if kwargs:
            raw.update(kwargs)
        super().__init__({k: _to_validated_value(v) for k, v in raw.items()})

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("_") and name in self:
            return self[name]
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )
        self[name] = _to_validated_value(value)

    def __delattr__(self, name: str) -> None:
        if not name.startswith("_") and name in self:
            del self[name]
            return
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def copy(self) -> ValidatedData:
        return ValidatedData(self)


@dataclass(slots=True)
class ValidationResult:
    """
    Typed result returned by validate(data, schema).
    """

    is_valid: bool
    errors: dict[str, list[str]] = field(default_factory=dict)
    validated_data: dict[str, Any] = field(default_factory=dict)
    _single_error: bool = field(default=False, repr=False)

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
        return _extract_first_error(self.errors, field_name)

    def first_errors(self) -> dict[str, str]:
        """Return a mapping of each failing field to its first error message."""
        return _extract_first_errors(self.errors)

    def errors_for(self, field_name: str) -> list[str]:
        """Return all error messages for a specific field."""
        return list(self.errors.get(field_name, []))

    def all_errors(self) -> list[str]:
        """Return a flattened list of all error messages."""
        flattened: list[str] = []
        for field_errors in self.errors.values():
            flattened.extend(field_errors)
        return flattened

    def validated(self) -> ValidatedData:
        """Return a ValidatedData copy supporting both dict and attribute access."""
        return ValidatedData(self.validated_data)

    def raise_if_failed(self) -> Self:
        """Raise ValidationException if validation failed, or return self for chaining."""
        if self.fails():
            raise ValidationException(
                self.errors,
                single_error=self._single_error,
            )
        return self


_UNSET = object()


def _expand_wildcard_targets(
    data: dict[str, Any],
    pattern: str,
    rules: list[Any],
) -> tuple[
    list[tuple[str, tuple[str | int, ...], list[Any], bool, Any, str]],
    dict[str, list[str]],
]:
    """Expand a dotted wildcard pattern (e.g. 'items.*', 'users.*.email') against payload data."""
    segments = pattern.split(".")
    targets: list[tuple[str, tuple[str | int, ...], list[Any], bool, Any, str]] = []
    structural_errors: dict[str, list[str]] = {}

    def _walk(
        current: Any,
        seg_idx: int,
        field_parts: list[str],
        path_keys: list[str | int],
        present: bool,
    ) -> None:
        if not present:
            return
        if seg_idx >= len(segments):
            concrete_name = ".".join(field_parts)
            targets.append(
                (concrete_name, tuple(path_keys), rules, True, current, pattern)
            )
            return

        seg = segments[seg_idx]
        if seg == "*":
            if current is None:
                return
            if not isinstance(current, (list, tuple)):
                parent_name = ".".join(field_parts) or pattern
                structural_errors.setdefault(parent_name, []).append(
                    f"The {parent_name} field must be a list."
                )
                return
            for idx, item in enumerate(current):
                next_fields = [*field_parts, str(idx)]
                next_keys: list[str | int] = [*path_keys, idx]
                if seg_idx == len(segments) - 1:
                    targets.append(
                        (
                            ".".join(next_fields),
                            tuple(next_keys),
                            rules,
                            True,
                            item,
                            pattern,
                        )
                    )
                else:
                    _walk(item, seg_idx + 1, next_fields, next_keys, True)
            return

        if isinstance(current, Mapping):
            has_key = seg in current
            val = current.get(seg)
            if not has_key:
                if seg_idx == len(segments) - 1 and any(
                    k == "*" for k in segments[:seg_idx]
                ):
                    concrete_name = ".".join([*field_parts, seg])
                    targets.append(
                        (
                            concrete_name,
                            (*path_keys, seg),
                            rules,
                            False,
                            None,
                            pattern,
                        )
                    )
                return
            _walk(val, seg_idx + 1, [*field_parts, seg], [*path_keys, seg], True)
        elif current is not None:
            parent_name = ".".join(field_parts) or pattern
            structural_errors.setdefault(parent_name, []).append(
                f"The {parent_name} field must be an object."
            )

    _walk(data, 0, [], [], True)
    return targets, structural_errors


def _assign_nested_value(
    validated_data: dict[str, Any],
    source_data: dict[str, Any],
    path_keys: tuple[str | int, ...],
    value: Any,
) -> None:
    """Assign a coerced leaf value into validated_data without mutating caller input."""
    if not path_keys:
        return
    root_key = str(path_keys[0])
    if root_key not in validated_data and root_key in source_data:
        validated_data[root_key] = copy.deepcopy(source_data[root_key])
    if len(path_keys) == 1:
        validated_data[root_key] = value
        return
    cur: Any = validated_data.get(root_key)
    for step in path_keys[1:-1]:
        if isinstance(step, int) and isinstance(cur, list) and 0 <= step < len(cur):
            next_cur: Any = cur[step]
            cur = next_cur
        elif isinstance(step, str) and isinstance(cur, dict) and step in cur:
            cur = cur[step]
        else:
            return
    last = path_keys[-1]
    if isinstance(last, int):
        if isinstance(cur, list) and 0 <= last < len(cur):
            cur[last] = value
    elif isinstance(cur, dict):
        cur[last] = value


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
        messages: dict[str, str] | None = None,
        *,
        stop_on_first_error: bool = False,
    ) -> None:
        self.is_payload_dict = isinstance(data, dict)
        self.data: dict[str, Any] = dict(data) if isinstance(data, dict) else {}
        self.schema = schema
        self.registry = registry or default_registry
        self.messages: dict[str, str] = dict(messages) if messages else {}
        self.stop_on_first_error = bool(stop_on_first_error)
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
                _single_error=self.stop_on_first_error,
            )

        direct_fields: list[tuple[str, list[Any]]] = []
        wildcard_fields: list[tuple[str, list[Any]]] = []
        for field_name, rule_def in self.schema.items():
            if isinstance(rule_def, list) and all(
                not isinstance(r, str)
                and (hasattr(r, "validate") or hasattr(r, "passes"))
                for r in rule_def
            ):
                parsed = rule_def
            else:
                parsed = self.registry.parse_rules(rule_def)

            if "*" in field_name.split("."):
                wildcard_fields.append((field_name, parsed))
            else:
                direct_fields.append((field_name, parsed))

        errors: dict[str, list[str]] = {}
        validated_data: dict[str, Any] = {}

        direct_targets: list[
            tuple[str, tuple[str | int, ...], list[Any], bool, Any, str]
        ] = [
            (
                field_name,
                (field_name,),
                rules,
                field_name in self.data,
                self.data.get(field_name),
                field_name,
            )
            for field_name, rules in direct_fields
        ]
        wildcard_targets: list[
            tuple[str, tuple[str | int, ...], list[Any], bool, Any, str]
        ] = []
        for pattern, rules in wildcard_fields:
            expanded, struct_errs = _expand_wildcard_targets(self.data, pattern, rules)
            for k, msgs in struct_errs.items():
                errors.setdefault(k, []).extend(msgs)
            wildcard_targets.extend(expanded)

        all_targets = [*direct_targets, *wildcard_targets]

        if self.stop_on_first_error:
            if errors:
                first_k = next(iter(errors))
                return ValidationResult(
                    is_valid=False,
                    errors={first_k: [errors[first_k][0]]},
                    validated_data={},
                    _single_error=True,
                )
            results: list[tuple[tuple[str | int, ...], tuple[str, list[str], Any]]] = []
            for field_name, path_keys, rules, present, raw_val, template_field in all_targets:
                res = await self._validate_field(
                    field_name,
                    rules,
                    field_present=present,
                    raw_value=raw_val,
                    template_field=template_field,
                )
                if res[1]:
                    return ValidationResult(
                        is_valid=False,
                        errors={res[0]: [res[1][0]]},
                        validated_data={},
                        _single_error=True,
                    )
                results.append((path_keys, res))
        elif get_active_connection() is not None:
            results = [
                (
                    path_keys,
                    await self._validate_field(
                        field_name,
                        rules,
                        field_present=present,
                        raw_value=raw_val,
                        template_field=template_field,
                    ),
                )
                for field_name, path_keys, rules, present, raw_val, template_field in all_targets
            ]
        else:
            coros = [
                self._validate_field(
                    field_name,
                    rules,
                    field_present=present,
                    raw_value=raw_val,
                    template_field=template_field,
                )
                for field_name, _pk, rules, present, raw_val, template_field in all_targets
            ]
            raw_results = list(await asyncio.gather(*coros))
            results = [
                (all_targets[idx][1], res) for idx, res in enumerate(raw_results)
            ]

        for path_keys, (field_name, field_errors, coerced_val) in results:
            if field_errors:
                errors.setdefault(field_name, []).extend(field_errors)
            elif coerced_val is not _UNSET:
                if len(path_keys) == 1:
                    validated_data[field_name] = copy.deepcopy(coerced_val)
                else:
                    _assign_nested_value(validated_data, self.data, path_keys, coerced_val)

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            validated_data=validated_data,
        )

    async def _validate_field(
        self,
        field_name: str,
        rules: list[Any],
        *,
        field_present: bool | None = None,
        raw_value: Any = _UNSET,
        template_field: str | None = None,
    ) -> tuple[str, list[str], Any]:
        field_errors: list[str] = []

        is_nullable = any(
            isinstance(r, NullableRule) or getattr(r, "is_nullable_rule", False)
            for r in rules
        )
        is_required = any(isinstance(r, RequiredRule) for r in rules)
        should_bail = (
            any(
                isinstance(r, BailRule) or getattr(r, "is_bail_rule", False)
                for r in rules
            )
            or self.stop_on_first_error
        )

        if field_present is None:
            field_present = field_name in self.data
        if raw_value is _UNSET:
            raw_value = self.data.get(field_name)

        # 1. Field is absent
        if not field_present:
            if is_required:
                req_rule = next(r for r in rules if isinstance(r, RequiredRule))
                field_errors.append(
                    self._get_rule_message(
                        req_rule, field_name, template_field=template_field
                    )
                )
            return field_name, field_errors, _UNSET

        # 2. Field is present and null / empty string
        if (raw_value is None or raw_value == "") and is_nullable:
            return field_name, [], raw_value

        coerced_value = raw_value

        # Sort rules canonically so type coercion executes consistently before constraint rules
        sorted_rules = sorted(rules, key=_rule_sort_key)

        # 3. Evaluate rules sequentially for this field
        for rule in sorted_rules:
            if isinstance(rule, (NullableRule, BailRule)) or getattr(
                rule, "is_bail_rule", False
            ):
                continue

            if (
                getattr(rule, "requires_db", False)
                and get_active_connection() is not None
            ):
                async with self._db_rule_lock:
                    passed = await self._run_rule(
                        rule, field_name, coerced_value, field_present=field_present
                    )
            else:
                passed = await self._run_rule(
                    rule, field_name, coerced_value, field_present=field_present
                )
            if not passed:
                msg = self._get_rule_message(
                    rule, field_name, template_field=template_field
                )
                field_errors.append(msg)
                # Stop further rules when required fails or bail is enabled
                if isinstance(rule, RequiredRule) or should_bail:
                    break
            else:
                # Apply coercion if rule is type rule
                if isinstance(rule, IntRule) and isinstance(coerced_value, str):
                    try:
                        coerced_value = int(coerced_value.strip())
                    except ValueError:
                        pass
                elif isinstance(rule, FloatRule):
                    if isinstance(coerced_value, int) and not isinstance(
                        coerced_value, bool
                    ):
                        coerced_value = float(coerced_value)
                    elif isinstance(coerced_value, str):
                        try:
                            coerced_value = float(coerced_value.strip())
                        except ValueError:
                            pass
                elif isinstance(rule, BoolRule) and not isinstance(coerced_value, bool):
                    str_val = str(coerced_value).strip().lower()
                    if str_val in ("true", "1"):
                        coerced_value = True
                    elif str_val in ("false", "0"):
                        coerced_value = False
                elif isinstance(rule, EmailRule) and isinstance(coerced_value, str):
                    coerced_value = rule.normalize(coerced_value)

        return field_name, field_errors, coerced_value

    async def _run_rule(
        self,
        rule: Any,
        field_name: str,
        value: Any,
        *,
        field_present: bool = True,
    ) -> bool:
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
            rule_data = (
                {**self.data, field_name: value}
                if field_present and field_name not in self.data
                else self.data
            )
            res = method(field_name, value, rule_data)

        if is_async or inspect.isawaitable(res):
            return bool(await res)
        return bool(res)

    def _resolve_rule_aliases(self, rule: Any) -> tuple[str, ...]:
        explicit = getattr(rule, "rule_name", None)
        if isinstance(explicit, str) and explicit:
            return (explicit.lower(),)
        aliases = _RULE_ALIAS_MAP.get(type(rule))
        if aliases is not None:
            return aliases
        cls_name = type(rule).__name__.lower()
        if cls_name.endswith("rule") and len(cls_name) > 4:
            return (cls_name[:-4],)
        return (cls_name,)

    def _get_rule_message(
        self,
        rule: Any,
        field_name: str,
        *,
        template_field: str | None = None,
    ) -> str:
        """Extract custom override or default error message from the rule."""
        if self.messages:
            aliases = self._resolve_rule_aliases(rule)
            for target_field in (
                (field_name, template_field)
                if template_field and template_field != field_name
                else (field_name,)
            ):
                for alias in aliases:
                    key = f"{target_field}.{alias}"
                    if key in self.messages:
                        return str(self.messages[key])
                if target_field in self.messages:
                    return str(self.messages[target_field])
            for alias in aliases:
                if alias in self.messages:
                    return str(self.messages[alias])

        if hasattr(rule, "message"):
            attr = rule.message
            if callable(attr):
                try:
                    return str(attr(field_name))
                except TypeError:
                    return str(attr())
            return str(attr)
        return f"The {field_name} field is invalid."


async def validate(
    data: dict[str, Any] | Request | Any,
    schema: dict[str, Any],
    registry: RuleRegistry | None = None,
    messages: dict[str, str] | None = None,
    *,
    stop_on_first_error: bool = False,
) -> ValidationResult:
    """
    Validate a mapping or incoming Request against a schema non-blockingly,
    returning a typed ValidationResult.
    """
    payload: Any = data
    if isinstance(data, Request):
        loaded = await data.input()
        payload = None if getattr(data, "_non_dict_payload", False) else loaded

    validator = Validator(
        data=payload,
        schema=schema,
        registry=registry,
        messages=messages,
        stop_on_first_error=stop_on_first_error,
    )
    return await validator.validate()


__all__ = ["ValidatedData", "ValidationResult", "Validator", "validate"]
