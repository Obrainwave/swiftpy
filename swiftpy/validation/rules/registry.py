from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from swiftpy.core.config import ConfigurationError
from swiftpy.validation.protocols import ValidatorRule
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
from swiftpy.validation.rules.required import NullableRule, RequiredRule
from swiftpy.validation.rules.unique import UniqueRule


def _split_unescaped(text: str, delimiter: str) -> list[str]:
    """Split text by delimiter, ignoring escaped delimiters (e.g. \\| or \\:) while preserving backslash pairs."""
    tokens: list[str] = []
    current: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            if i + 1 < n and text[i + 1] == delimiter:
                current.append(delimiter)
                i += 2
                continue
            if i + 1 < n and text[i + 1] == "\\":
                current.append("\\\\")
                i += 2
                continue
            current.append("\\")
            i += 1
            continue
        if char == delimiter:
            tokens.append("".join(current))
            current = []
            i += 1
            continue
        current.append(char)
        i += 1
    tokens.append("".join(current))
    return tokens


def _parse_num(val: str) -> int | float:
    """Parse string into finite int or float, raising ConfigurationError on invalid input."""
    clean = val.strip()
    if not clean:
        raise ConfigurationError("Numeric validation rule argument cannot be empty")
    try:
        if any(ch in clean for ch in (".", "e", "E")):
            num = float(clean)
            if not math.isfinite(num):
                raise ConfigurationError(
                    f"Validation rule numeric argument must be finite, got '{val}'"
                )
            return num
        return int(clean)
    except ValueError as exc:
        raise ConfigurationError(
            f"Expected numeric validation rule argument, got '{val}'"
        ) from exc


def _parse_between(v: str) -> BetweenRule:
    parts = [p.strip() for p in v.split(",")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ConfigurationError(
            f"between rule requires min and max arguments, e.g. 'between:1,10' (got '{v}')"
        )
    min_val = _parse_num(parts[0])
    max_val = _parse_num(parts[1])
    if min_val > max_val:
        raise ConfigurationError(
            f"between rule min ({min_val}) cannot be greater than max ({max_val})"
        )
    return BetweenRule(min_val, max_val)


def _parse_unique(v: str) -> UniqueRule:
    parts = [p.strip() for p in v.split(",")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ConfigurationError(
            "unique rule requires table and column arguments, e.g. 'unique:users,email'"
        )
    table = parts[0]
    column = parts[1]
    ignore_id: Any = None
    id_column: str = "id"
    case_insensitive = False
    if len(parts) > 2:
        for extra in parts[2:]:
            if extra.lower() == "ci":
                case_insensitive = True
            elif ignore_id is None:
                ignore_id = extra if extra != "" else None
            elif id_column == "id":
                id_column = extra
    return UniqueRule(
        table=table,
        column=column,
        ignore_id=ignore_id,
        id_column=id_column,
        case_insensitive=case_insensitive,
    )


def _parse_exists(v: str) -> ExistsRule:
    parts = [p.strip() for p in v.split(",")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ConfigurationError(
            f"exists rule requires table and column arguments, e.g. 'exists:users,id' (got '{v}')"
        )
    return ExistsRule(table=parts[0], column=parts[1])


def _parse_in_values(v: str) -> list[str]:
    values = [x.strip() for x in v.split(",") if x.strip()]
    if not values:
        raise ConfigurationError(
            f"in/not_in rule requires at least one allowed/disallowed value (got '{v}')"
        )
    return values


def _parse_regex(v: str) -> RegexRule:
    pattern = v.strip()
    if not pattern:
        raise ConfigurationError("regex rule requires a non-empty pattern argument")
    return RegexRule(pattern)


def _parse_email(v: str = "") -> EmailRule:
    if v:
        return EmailRule(lowercase=(v.strip().lower() in ("lowercase", "ci", "lc")))
    return EmailRule()


_REQUIRED_ARG_RULES: frozenset[str] = frozenset(
    {"min", "max", "between", "in", "in_", "not_in", "regex", "unique", "exists"}
)


class RuleRegistry:
    """
    Registry for resolving validation rules from names, string expressions,
    or factory functions.
    """

    def __init__(self) -> None:
        self._rules: dict[str, Any] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        self.register("required", RequiredRule)
        self.register("nullable", NullableRule)
        self.register("bail", BailRule)
        self.register("str", StrRule)
        self.register("string", StrRule)
        self.register("int", IntRule)
        self.register("integer", IntRule)
        self.register("float", FloatRule)
        self.register("bool", BoolRule)
        self.register("boolean", BoolRule)
        self.register("email", _parse_email)
        self.register("url", UrlRule)
        self.register("min", lambda v: MinRule(_parse_num(v)))
        self.register("max", lambda v: MaxRule(_parse_num(v)))
        self.register("between", _parse_between)
        self.register("in", lambda v: InRule(_parse_in_values(v)))
        self.register("in_", lambda v: InRule(_parse_in_values(v)))
        self.register("not_in", lambda v: NotInRule(_parse_in_values(v)))
        self.register("regex", _parse_regex)
        self.register("unique", _parse_unique)
        self.register("exists", _parse_exists)

    def register(self, name: str, rule_target: Any) -> None:
        """Register a custom rule by name."""
        self._rules[name.lower()] = rule_target

    def get(self, name: str) -> Any:
        return self._rules.get(name.lower())

    def parse(self, rule_item: Any) -> Any:
        """Parse a single rule definition into a rule instance."""
        # Built-in Python types mapping
        if rule_item is str:
            return StrRule()
        if rule_item is int:
            return IntRule()
        if rule_item is float:
            return FloatRule()
        if rule_item is bool:
            return BoolRule()

        # Already an instance of a rule
        if isinstance(rule_item, (ValidatorRule, RequiredRule, NullableRule)) or (
            hasattr(rule_item, "validate") or hasattr(rule_item, "passes")
        ):
            if isinstance(rule_item, type):
                return rule_item()
            return rule_item

        # Callable factory with no args
        if callable(rule_item) and not isinstance(rule_item, type):
            try:
                res = rule_item()
                if hasattr(res, "validate") or hasattr(res, "passes"):
                    return res
            except TypeError:
                pass

        if isinstance(rule_item, str):
            rule_str = rule_item.strip()
            if not rule_str:
                return None

            parts = _split_unescaped(rule_str, ":")
            name = parts[0].strip().lower()
            arg = ":".join(parts[1:]).strip() if len(parts) > 1 else None

            factory = self.get(name)
            if factory is None:
                raise ValueError(f"Unknown validation rule '{name}'")

            if arg is not None:
                if name in _REQUIRED_ARG_RULES and not arg:
                    raise ConfigurationError(
                        f"Validation rule '{name}' requires a non-empty argument"
                    )
                return factory(arg)
            else:
                if name in _REQUIRED_ARG_RULES:
                    raise ConfigurationError(
                        f"Validation rule '{name}' requires an argument"
                    )
                try:
                    return factory() if callable(factory) else factory
                except TypeError as exc:
                    raise ConfigurationError(
                        f"Validation rule '{name}' requires valid arguments: {exc}"
                    ) from exc

        raise TypeError(f"Unsupported validation rule type: {type(rule_item)}")

    def parse_rules(self, rules_input: str | Sequence[Any]) -> list[Any]:
        """Parse a pipe-separated string or ordered sequence of rules into a list of rule instances."""
        if isinstance(rules_input, (set, frozenset)):
            raise TypeError(
                "Validation rules cannot be an unordered set; use a pipe-delimited string or list/tuple."
            )
        if isinstance(rules_input, str):
            raw_list = [
                r.strip() for r in _split_unescaped(rules_input, "|") if r.strip()
            ]
        else:
            raw_list = list(rules_input)

        parsed: list[Any] = []
        for item in raw_list:
            rule = self.parse(item)
            if rule is not None:
                parsed.append(rule)
        return parsed


default_registry = RuleRegistry()


def register_rule(name: str, rule_target: Any) -> None:
    """Public helper to register a custom rule with default registry."""
    default_registry.register(name, rule_target)


__all__ = ["RuleRegistry", "default_registry", "register_rule"]
