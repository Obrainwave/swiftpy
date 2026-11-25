from __future__ import annotations

import math
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

_MISSING = object()


def _validate_numeric_bound(name: str, value: Any) -> float | Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(
            f"Field constraint '{name}' must be int, float, or Decimal, got {type(value).__name__}."
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Field constraint '{name}' must be finite, got {value!r}.")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"Field constraint '{name}' must be finite, got {value!r}.")
    return value


def _validate_length_bound(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"Field constraint '{name}' must be an int, got {type(value).__name__}."
        )
    if value < 0:
        raise ValueError(f"Field constraint '{name}' must be >= 0, got {value}.")
    return value


class SchemaFieldInfo:
    """Declarative field metadata and validation constraints for BaseSchema / Schema."""

    __slots__ = (
        "alias",
        "compiled_pattern",
        "default",
        "default_factory",
        "description",
        "exclude",
        "ge",
        "gt",
        "le",
        "lt",
        "max_length",
        "min_length",
        "multiple_of",
        "pattern",
        "strip_whitespace",
        "to_lower",
        "to_upper",
    )

    def __init__(
        self,
        default: Any = ...,
        *,
        default_factory: Callable[[], Any] | None = None,
        alias: str | None = None,
        description: str | None = None,
        gt: float | Decimal | None = None,
        ge: float | Decimal | None = None,
        lt: float | Decimal | None = None,
        le: float | Decimal | None = None,
        multiple_of: float | Decimal | None = None,
        min_length: int | None = None,
        max_length: int | None = None,
        pattern: str | re.Pattern[str] | None = None,
        strip_whitespace: bool = False,
        to_lower: bool = False,
        to_upper: bool = False,
        exclude: bool = False,
    ) -> None:
        normalized_default = _MISSING if default is Ellipsis else default
        if normalized_default is not _MISSING and default_factory is not None:
            raise ValueError("Cannot specify both 'default' and 'default_factory'.")
        if default_factory is not None and not callable(default_factory):
            raise TypeError("'default_factory' must be callable.")
        if alias is not None and (not isinstance(alias, str) or not alias):
            raise ValueError("'alias' must be a non-empty string.")
        if to_lower and to_upper:
            raise ValueError("Cannot specify both 'to_lower=True' and 'to_upper=True'.")

        self.default = normalized_default
        self.default_factory = default_factory
        self.alias = alias
        self.description = description

        self.gt = _validate_numeric_bound("gt", gt) if gt is not None else None
        self.ge = _validate_numeric_bound("ge", ge) if ge is not None else None
        self.lt = _validate_numeric_bound("lt", lt) if lt is not None else None
        self.le = _validate_numeric_bound("le", le) if le is not None else None
        if self.gt is not None and self.ge is not None:
            raise ValueError("Cannot specify both 'gt' and 'ge'.")
        if self.lt is not None and self.le is not None:
            raise ValueError("Cannot specify both 'lt' and 'le'.")

        if self.gt is not None and self.lt is not None and self.gt >= self.lt:
            raise ValueError("Lower bound 'gt' must be less than upper bound 'lt'.")
        if self.gt is not None and self.le is not None and self.gt >= self.le:
            raise ValueError("Lower bound 'gt' must be less than upper bound 'le'.")
        if self.ge is not None and self.lt is not None and self.ge >= self.lt:
            raise ValueError("Lower bound 'ge' must be less than upper bound 'lt'.")
        if self.ge is not None and self.le is not None and self.ge > self.le:
            raise ValueError("Lower bound 'ge' cannot exceed upper bound 'le'.")

        if multiple_of is not None:
            checked_mult = _validate_numeric_bound("multiple_of", multiple_of)
            if checked_mult <= 0:
                raise ValueError("Field constraint 'multiple_of' must be greater than 0.")
            self.multiple_of: float | Decimal | None = checked_mult
        else:
            self.multiple_of = None

        self.min_length = (
            _validate_length_bound("min_length", min_length)
            if min_length is not None
            else None
        )
        self.max_length = (
            _validate_length_bound("max_length", max_length)
            if max_length is not None
            else None
        )
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("'min_length' cannot exceed 'max_length'.")

        self.pattern: str | None
        self.compiled_pattern: re.Pattern[str] | None
        if pattern is None:
            self.pattern = None
            self.compiled_pattern = None
        elif isinstance(pattern, re.Pattern):
            self.pattern = pattern.pattern
            self.compiled_pattern = pattern
        elif isinstance(pattern, str):
            try:
                self.compiled_pattern = re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"Invalid regex pattern {pattern!r}: {exc}") from exc
            self.pattern = pattern
        else:
            raise TypeError(
                f"Field constraint 'pattern' must be str or re.Pattern, got {type(pattern).__name__}."
            )

        self.strip_whitespace = bool(strip_whitespace)
        self.to_lower = bool(to_lower)
        self.to_upper = bool(to_upper)
        self.exclude = bool(exclude)

    @property
    def is_required(self) -> bool:
        """Return True when the field has no default value or default factory."""
        return self.default is _MISSING and self.default_factory is None


def Field(
    default: Any = ...,
    *,
    default_factory: Callable[[], Any] | None = None,
    alias: str | None = None,
    description: str | None = None,
    gt: float | Decimal | None = None,
    ge: float | Decimal | None = None,
    lt: float | Decimal | None = None,
    le: float | Decimal | None = None,
    multiple_of: float | Decimal | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | re.Pattern[str] | None = None,
    strip_whitespace: bool = False,
    to_lower: bool = False,
    to_upper: bool = False,
    exclude: bool = False,
) -> Any:
    """Declare field defaults, metadata, and validation constraints on a BaseSchema / Schema."""
    return SchemaFieldInfo(
        default=default,
        default_factory=default_factory,
        alias=alias,
        description=description,
        gt=gt,
        ge=ge,
        lt=lt,
        le=le,
        multiple_of=multiple_of,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        strip_whitespace=strip_whitespace,
        to_lower=to_lower,
        to_upper=to_upper,
        exclude=exclude,
    )


SchemaField = Field

__all__ = ["Field", "SchemaField", "SchemaFieldInfo"]
