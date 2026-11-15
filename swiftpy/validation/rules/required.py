from __future__ import annotations

from typing import Any


class RequiredRule:
    """
    Validates that a field is present, not None, not an empty string,
    not whitespace-only, and not an empty collection.
    """

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if field not in data:
            return False
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        return not (isinstance(value, (list, dict, set, tuple)) and len(value) == 0)

    def message(self, field: str) -> str:
        return f"The {field} field is required."


class NullableRule:
    """
    Allows a field to be null or empty. When present and the field value
    is None or empty string, subsequent validation rules for that field are skipped.
    """

    is_nullable_rule: bool = True

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        return True

    def message(self, field: str) -> str:
        return f"The {field} field may be null."


def required() -> RequiredRule:
    """Helper to instantiate RequiredRule."""
    return RequiredRule()


def nullable() -> NullableRule:
    """Helper to instantiate NullableRule."""
    return NullableRule()


__all__ = [
    "NullableRule",
    "RequiredRule",
    "nullable",
    "required",
]
