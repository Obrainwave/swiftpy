from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from swiftpy.validation.protocols import ValidatorRule
from swiftpy.validation.rules.builtin import (
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


def _parse_num(val: str) -> int | float:
    """Parse string into int or float if numeric."""
    clean = val.strip()
    try:
        if "." in clean:
            return float(clean)
        return int(clean)
    except ValueError:
        return 0


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
        self.register("str", StrRule)
        self.register("string", StrRule)
        self.register("int", IntRule)
        self.register("integer", IntRule)
        self.register("float", FloatRule)
        self.register("bool", BoolRule)
        self.register("boolean", BoolRule)
        self.register("email", EmailRule)
        self.register("url", UrlRule)
        self.register("min", lambda v: MinRule(_parse_num(v)))
        self.register("max", lambda v: MaxRule(_parse_num(v)))
        self.register(
            "between",
            lambda v: BetweenRule(
                _parse_num(v.split(",")[0]),
                _parse_num(v.split(",")[1]),
            ),
        )
        self.register(
            "in",
            lambda v: InRule([x.strip() for x in v.split(",") if x.strip()]),
        )
        self.register(
            "in_",
            lambda v: InRule([x.strip() for x in v.split(",") if x.strip()]),
        )
        self.register(
            "not_in",
            lambda v: NotInRule([x.strip() for x in v.split(",") if x.strip()]),
        )
        self.register("regex", lambda v: RegexRule(v.strip()))
        self.register(
            "unique",
            lambda v: UniqueRule(
                table=v.split(",")[0].strip(),
                column=v.split(",")[1].strip(),
                ignore_id=(v.split(",")[2].strip() if len(v.split(",")) > 2 else None),
                id_column=(v.split(",")[3].strip() if len(v.split(",")) > 3 else "id"),
            ),
        )
        self.register(
            "exists",
            lambda v: ExistsRule(
                table=v.split(",")[0].strip(),
                column=v.split(",")[1].strip(),
            ),
        )

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

            parts = rule_str.split(":", 1)
            name = parts[0].strip().lower()
            arg = parts[1].strip() if len(parts) > 1 else None

            factory = self.get(name)
            if factory is None:
                raise ValueError(f"Unknown validation rule '{name}'")

            if arg is not None:
                return factory(arg)
            else:
                return factory() if isinstance(factory, type) else factory

        raise TypeError(f"Unsupported validation rule type: {type(rule_item)}")

    def parse_rules(self, rules_input: str | Sequence[Any] | set[Any]) -> list[Any]:
        """Parse a pipe-separated string or sequence of rules into a list of rule instances."""
        if isinstance(rules_input, str):
            raw_list = [r.strip() for r in rules_input.split("|") if r.strip()]
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
