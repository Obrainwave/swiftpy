from __future__ import annotations

import math
import re
import urllib.parse
from collections.abc import Sequence
from typing import Any

# Standard RFC-compliant email regex pattern
_EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


class StrRule:
    """Validates that a field is a string."""

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        return isinstance(value, str) and not isinstance(value, bool)

    def message(self, field: str) -> str:
        return f"The {field} field must be a string."


class IntRule:
    """Validates that a field is an integer or coercible integer string."""

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, str):
            clean = value.strip()
            if clean.startswith(("-", "+")):
                return clean[1:].isdigit()
            return clean.isdigit()
        return False

    def message(self, field: str) -> str:
        return f"The {field} field must be an integer."


class FloatRule:
    """Validates that a field is a finite float, integer, or numeric float string."""

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return math.isfinite(value)
        if isinstance(value, str):
            try:
                val = float(value)
                return math.isfinite(val)
            except (ValueError, TypeError):
                return False
        return False

    def message(self, field: str) -> str:
        return f"The {field} field must be a float."


class BoolRule:
    """Validates that a field is a boolean value or boolean-like token."""

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if isinstance(value, bool):
            return True
        if isinstance(value, int) and value in (0, 1):
            return True
        return isinstance(value, str) and value.lower() in (
            "true",
            "false",
            "1",
            "0",
        )

    def message(self, field: str) -> str:
        return f"The {field} field must be a boolean."


class EmailRule:
    """Validates that a field is a well-formed email address."""

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if not isinstance(value, str) or isinstance(value, bool):
            return False
        val = value.strip()
        if len(val) > 254 or ".." in val:
            return False
        return bool(_EMAIL_REGEX.match(val))

    def message(self, field: str) -> str:
        return f"The {field} field must be a valid email address."


class UrlRule:
    """Validates that a field is a valid HTTP or HTTPS URL."""

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if not isinstance(value, str) or isinstance(value, bool):
            return False
        try:
            parsed = urllib.parse.urlparse(value.strip())
            return bool(parsed.scheme in ("http", "https") and parsed.netloc)
        except (ValueError, TypeError):
            return False

    def message(self, field: str) -> str:
        return f"The {field} field must be a valid URL."


class MinRule:
    """Validates minimum length for strings/collections or minimum numeric value."""

    def __init__(self, min_val: float) -> None:
        self.min_val = min_val

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return value >= self.min_val
        if isinstance(value, (str, list, dict, set, tuple)):
            return len(value) >= self.min_val
        return False

    def message(self, field: str) -> str:
        return f"The {field} field must be at least {self.min_val}."


class MaxRule:
    """Validates maximum length for strings/collections or maximum numeric value."""

    def __init__(self, max_val: float) -> None:
        self.max_val = max_val

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return value <= self.max_val
        if isinstance(value, (str, list, dict, set, tuple)):
            return len(value) <= self.max_val
        return False

    def message(self, field: str) -> str:
        return f"The {field} field must not be greater than {self.max_val}."


class BetweenRule:
    """Validates that a value or length is within an inclusive numeric range."""

    def __init__(self, min_val: float, max_val: float) -> None:
        if min_val > max_val:
            raise ValueError(
                f"min_val ({min_val}) cannot be greater than max_val ({max_val})"
            )
        self.min_val = min_val
        self.max_val = max_val

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return self.min_val <= value <= self.max_val
        if isinstance(value, (str, list, dict, set, tuple)):
            return self.min_val <= len(value) <= self.max_val
        return False

    def message(self, field: str) -> str:
        return f"The {field} field must be between {self.min_val} and {self.max_val}."


class InRule:
    """Validates that a field value exists within an allowed list of values."""

    def __init__(self, values: Sequence[Any] | set[Any]) -> None:
        self.values = list(values)
        self._str_values = {str(v) for v in self.values}

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if value in self.values:
            return True
        return str(value) in self._str_values

    def message(self, field: str) -> str:
        return f"The selected {field} is invalid."


class NotInRule:
    """Validates that a field value does not exist within a forbidden list of values."""

    def __init__(self, values: Sequence[Any] | set[Any]) -> None:
        self.values = list(values)
        self._str_values = {str(v) for v in self.values}

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        return value not in self.values and str(value) not in self._str_values

    def message(self, field: str) -> str:
        return f"The selected {field} is invalid."


class RegexRule:
    """Validates that a field value matches a specific regular expression."""

    def __init__(self, pattern: str | re.Pattern[str]) -> None:
        self.pattern = (
            pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
        )

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            return False
        return bool(self.pattern.search(str(value)))

    def message(self, field: str) -> str:
        return f"The {field} field format is invalid."


# Rule factories
def str_() -> StrRule:
    return StrRule()


def string() -> StrRule:
    return StrRule()


def int_() -> IntRule:
    return IntRule()


def integer() -> IntRule:
    return IntRule()


def float_() -> FloatRule:
    return FloatRule()


def bool_() -> BoolRule:
    return BoolRule()


def boolean() -> BoolRule:
    return BoolRule()


def email() -> EmailRule:
    return EmailRule()


def url() -> UrlRule:
    return UrlRule()


def min_(val: float) -> MinRule:
    return MinRule(val)


def max_(val: float) -> MaxRule:
    return MaxRule(val)


def between(min_val: float, max_val: float) -> BetweenRule:
    return BetweenRule(min_val, max_val)


def in_(values: Sequence[Any] | set[Any]) -> InRule:
    return InRule(values)


def not_in(values: Sequence[Any] | set[Any]) -> NotInRule:
    return NotInRule(values)


def regex(pattern: str | re.Pattern[str]) -> RegexRule:
    return RegexRule(pattern)


__all__ = [
    "BetweenRule",
    "BoolRule",
    "EmailRule",
    "FloatRule",
    "InRule",
    "IntRule",
    "MaxRule",
    "MinRule",
    "NotInRule",
    "RegexRule",
    "StrRule",
    "UrlRule",
    "between",
    "bool_",
    "boolean",
    "email",
    "float_",
    "in_",
    "int_",
    "integer",
    "max_",
    "min_",
    "not_in",
    "regex",
    "str_",
    "string",
    "url",
]
