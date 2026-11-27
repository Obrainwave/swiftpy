from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from swiftpy.database.orm.fields import Field, FieldInfo, Vector, VectorFieldInfo
from swiftpy.database.orm.model import Model, ModelQueryBuilder
from swiftpy.database.orm.relationships import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
    LazyLoadingDisabledError,
    Relation,
    RelationDescriptor,
    RelationNotLoadedError,
    RelationshipQueryBuilder,
)

if TYPE_CHECKING:
    from swiftpy.database.factories import Factory, FactoryBatch, Fake, Sequence

_LAZY_FACTORY_EXPORTS: frozenset[str] = frozenset(
    {"Factory", "FactoryBatch", "Fake", "Sequence"}
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_FACTORY_EXPORTS:
        module = importlib.import_module("swiftpy.database.factories")
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "BelongsTo",
    "BelongsToMany",
    "Factory",
    "FactoryBatch",
    "Fake",
    "Field",
    "FieldInfo",
    "HasMany",
    "HasOne",
    "LazyLoadingDisabledError",
    "Model",
    "ModelQueryBuilder",
    "Relation",
    "RelationDescriptor",
    "RelationNotLoadedError",
    "RelationshipQueryBuilder",
    "Sequence",
    "Vector",
    "VectorFieldInfo",
]
