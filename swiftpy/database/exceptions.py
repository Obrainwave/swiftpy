from __future__ import annotations

from typing import Any


class DatabaseError(Exception):
    """Base exception for all database subsystem errors."""


class RecordNotFoundError(DatabaseError, LookupError):
    """Raised when a specific raw query record is not found."""

    def __init__(self, message: str = "Record not found.") -> None:
        super().__init__(message)
        self.message = message


class ModelNotFoundError(RecordNotFoundError):
    """Raised when an ORM model query using *_or_fail fails to find a matching instance."""

    def __init__(
        self,
        model: str | type[Any],
        primary_key: Any = None,
        message: str | None = None,
    ) -> None:
        self.model = model if isinstance(model, str) else model.__name__
        self.primary_key = primary_key
        if message is None:
            if primary_key is not None:
                message = f"No query results for model [{self.model}] with primary key [{primary_key}]."
            else:
                message = f"No query results for model [{self.model}]."
        super().__init__(message)


__all__ = [
    "DatabaseError",
    "ModelNotFoundError",
    "RecordNotFoundError",
]

