from __future__ import annotations

from collections.abc import Callable
from typing import Any

_MISSING = object()


class FieldInfo:
    """Represents column metadata and configuration on an Active Record Model."""

    def __init__(
        self,
        default: Any = _MISSING,
        *,
        default_factory: Callable[[], Any] | None = None,
        primary_key: bool = False,
        nullable: bool = True,
    ) -> None:
        self.default = default
        self.default_factory = default_factory
        self.primary_key = primary_key
        self.nullable = nullable


def Field(
    default: Any = _MISSING,
    *,
    default_factory: Callable[[], Any] | None = None,
    primary_key: bool = False,
    nullable: bool = True,
) -> Any:
    """Declare column metadata, default value, or default_factory on an Active Record Model."""
    return FieldInfo(
        default=default,
        default_factory=default_factory,
        primary_key=primary_key,
        nullable=nullable,
    )


__all__ = ["Field", "FieldInfo"]
