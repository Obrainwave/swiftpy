from __future__ import annotations

from typing import Any


class Field:
    """Represents column metadata and configuration on an Active Record Model."""

    def __init__(
        self,
        default: Any = None,
        *,
        primary_key: bool = False,
        nullable: bool = True,
    ) -> None:
        self.default = default
        self.primary_key = primary_key
        self.nullable = nullable


__all__ = ["Field"]
