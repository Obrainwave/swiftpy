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


class VectorFieldInfo(FieldInfo):
    """Represents a vector column for semantic search and embeddings on an Active Record Model."""

    def __init__(
        self,
        dimensions: int,
        default: Any = _MISSING,
        *,
        metric: str = "cosine",
        default_factory: Callable[[], Any] | None = None,
        primary_key: bool = False,
        nullable: bool = True,
    ) -> None:
        if not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError(f"Vector dimensions must be a positive integer, got {dimensions}")
        allowed_metrics = {"cosine", "l2", "inner_product"}
        if metric not in allowed_metrics:
            raise ValueError(
                f"Unsupported vector distance metric '{metric}'. Expected one of {allowed_metrics}."
            )
        super().__init__(
            default=default,
            default_factory=default_factory,
            primary_key=primary_key,
            nullable=nullable,
        )
        self.dimensions = dimensions
        self.metric = metric


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


def Vector(
    dimensions: int,
    default: Any = _MISSING,
    *,
    metric: str = "cosine",
    default_factory: Callable[[], Any] | None = None,
    primary_key: bool = False,
    nullable: bool = True,
) -> Any:
    """Declare a vector embedding column with fixed dimensionality on an Active Record Model."""
    return VectorFieldInfo(
        dimensions=dimensions,
        default=default,
        metric=metric,
        default_factory=default_factory,
        primary_key=primary_key,
        nullable=nullable,
    )


__all__ = ["Field", "FieldInfo", "Vector", "VectorFieldInfo"]
