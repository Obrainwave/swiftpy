from __future__ import annotations

import inspect
from collections.abc import Generator, Iterable, Sequence
from datetime import UTC, datetime
from typing import (
    Any,
    ClassVar,
    Self,
    TypeVar,
    get_origin,
    get_type_hints,
    overload,
)

from swiftpy.database.identifiers import validate_identifier
from swiftpy.database.orm.registry import (
    _MODEL_REGISTRY,
    restore_model_registry,
    snapshot_model_registry,
)
from swiftpy.database.orm.relationships import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
    Relation,
    RelationDescriptor,
)
from swiftpy.database.query_builder import Query
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.support.strings import pluralize, snake_case

T = TypeVar("T", bound="Model")
_MISSING = object()


class PrimaryKeyError(RuntimeError, ValueError):
    """Raised when a primary key is invalid, composite, or missing on a persisted model."""


class ModelQueryBuilder[T: Model]:
    """Hydrates database records into strongly typed Active Record Model instances."""

    def __init__(self, model_cls: type[T]) -> None:
        self.model_cls = model_cls
        self.builder = Query.table(model_cls.__table__)
        self._eager_loads: list[str] = []

    def clone(self) -> Self:
        """Create an independent copy of this ModelQueryBuilder."""
        cloned = self.__class__(self.model_cls)
        cloned.builder = self.builder.clone()
        cloned._eager_loads = list(self._eager_loads)
        return cloned

    def allow_all(self) -> Self:
        """Explicitly permit mass UPDATE or DELETE queries without a WHERE clause (Decision D-10)."""
        self.builder.allow_all()
        return self

    def where(self, column: str, operator_or_value: Any, value: Any = _MISSING) -> Self:
        if value is _MISSING:
            self.builder.where(column, operator_or_value)
        else:
            self.builder.where(column, operator_or_value, value)
        return self

    def where_in(self, column: str, values: list[Any]) -> Self:
        self.builder.where_in(column, values)
        return self

    def where_not_in(self, column: str, values: list[Any]) -> Self:
        self.builder.where_not_in(column, values)
        return self

    def where_like(
        self,
        column: str,
        value: str,
        *,
        case_insensitive: bool = False,
        match: str = "contains",
    ) -> Self:
        self.builder.where_like(
            column, value, case_insensitive=case_insensitive, match=match
        )
        return self

    def where_ilike(
        self,
        column: str,
        value: str,
        *,
        match: str = "contains",
    ) -> Self:
        self.builder.where_ilike(column, value, match=match)
        return self

    def where_null(self, column: str) -> Self:
        self.builder.where_null(column)
        return self

    def where_not_null(self, column: str) -> Self:
        self.builder.where_not_null(column)
        return self

    def where_raw(self, sql: str, bindings: list[Any] | None = None) -> Self:
        self.builder.where_raw(sql, bindings)
        return self

    def with_(self, *relations: str) -> Self:
        self._eager_loads.extend(relations)
        return self

    def limit(self, value: int) -> Self:
        self.builder.limit(value)
        return self

    def order_by(self, column: str, direction: str = "ASC") -> Self:
        self.builder.order_by(column, direction)
        return self

    def select(self, *columns: str) -> Self:
        self.builder.select(*columns)
        return self

    async def count(self, column: str = "*") -> int:
        return await self.builder.count(column)

    async def exists(self) -> bool:
        return await self.builder.exists()

    async def sum(self, column: str) -> Any:
        return await self.builder.sum(column)

    async def min(self, column: str) -> Any:
        return await self.builder.min(column)

    async def max(self, column: str) -> Any:
        return await self.builder.max(column)

    async def avg(self, column: str) -> Any:
        return await self.builder.avg(column)

    async def get(self) -> list[T]:
        rows = await self.builder.get()
        instances: list[T] = []

        for row in rows:
            inst = self.model_cls(**row)
            inst._mark_persisted(row.keys())
            instances.append(inst)

        if self._eager_loads and instances:
            await self._load_relations(instances)

        return instances

    async def first(self) -> T | None:
        res = await self.clone().limit(1).get()
        return res[0] if res else None

    async def insert_returning(
        self,
        data: dict[str, Any],
        *,
        returning: Sequence[str] | str | None = None,
    ) -> dict[str, Any]:
        return await self.builder.insert_returning(data, returning=returning)

    @overload
    async def insert(self, data: dict[str, Any], *, returning: None = None) -> int: ...

    @overload
    async def insert(
        self, data: dict[str, Any], *, returning: Sequence[str] | str
    ) -> Any: ...

    async def insert(
        self,
        data: dict[str, Any],
        *,
        returning: Sequence[str] | str | None = None,
    ) -> Any:
        return await self.builder.insert(data, returning=returning)

    async def update(self, data: dict[str, Any]) -> str:
        return await self.builder.update(data)

    async def delete(self) -> str:
        return await self.builder.delete()

    def __await__(self) -> Generator[Any, None, list[T]]:
        return self.get().__await__()

    async def _load_relations(self, instances: list[T]) -> None:
        for rel_name in self._eager_loads:
            relation = self.model_cls.__relations_config__.get(rel_name)
            if not relation:
                raise ValueError(
                    f"Relation '{rel_name}' not found on model {self.model_cls.__name__}"
                )
            await relation.eager_load(instances)


class Model:
    """Active Record base model with typed attributes, dirty tracking, and relationship support."""

    __table__: ClassVar[str]
    __primary_key__: ClassVar[str] = "id"
    __fillable__: ClassVar[set[str] | list[str] | tuple[str, ...] | None] = None
    __hidden__: ClassVar[set[str] | list[str] | tuple[str, ...]] = ()

    __fields__: ClassVar[dict[str, Any]]
    __field_defaults__: ClassVar[dict[str, Any]]
    __relations_config__: ClassVar[dict[str, Relation]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        # 0. Validate primary key declaration (composite keys are explicitly unsupported)
        raw_pk = getattr(cls, "__primary_key__", "id")
        if not isinstance(raw_pk, str) or "," in raw_pk or not raw_pk.strip():
            raise PrimaryKeyError(
                f"Composite or non-string primary keys are not supported on {cls.__name__}; "
                "__primary_key__ must be a single valid column identifier."
            )
        cls.__primary_key__ = validate_identifier(raw_pk, param_name="__primary_key__")

        # 1. Determine table name if not explicitly set (overridable via __table__)
        if "__table__" not in cls.__dict__:
            cls.__table__ = pluralize(snake_case(cls.__name__))

        # 2. Inherit fields from parent Model classes
        fields: dict[str, Any] = {}
        defaults: dict[str, Any] = {}

        for base in reversed(cls.__mro__[1:]):
            fields.update(getattr(base, "__fields__", {}))
            defaults.update(getattr(base, "__field_defaults__", {}))

        # 3. Resolve typed class attributes using typing.get_type_hints
        globalns = getattr(inspect.getmodule(cls), "__dict__", {})
        localns: dict[str, Any] = {
            name: next(reversed(variants.values()))
            for name, variants in _MODEL_REGISTRY.items()
        }
        localns[cls.__name__] = cls

        try:
            resolved_hints = get_type_hints(cls, globalns=globalns, localns=localns)
        except (NameError, TypeError):
            resolved_hints = inspect.get_annotations(cls, eval_str=False)

        for name, hint in resolved_hints.items():
            if name.startswith("__"):
                continue
            if get_origin(hint) is ClassVar:
                continue

            fields[name] = hint
            if name in cls.__dict__:
                defaults[name] = cls.__dict__[name]

        # 4. Guarantee primary key field presence
        if cls.__primary_key__ not in fields:
            fields[cls.__primary_key__] = int

        cls.__fields__ = fields
        cls.__field_defaults__ = defaults

        # 5. Inherit relations from ancestors and discover newly declared relations
        relations: dict[str, Relation] = {}
        for base in reversed(cls.__mro__[1:]):
            relations.update(getattr(base, "__relations_config__", {}))

        for name, value in list(cls.__dict__.items()):
            if isinstance(value, Relation):
                value.name = name
                value.owner_cls = cls
                relations[name] = value
                setattr(cls, name, RelationDescriptor(name, value))

        cls.__relations_config__ = relations

        # 6. Register model globally under bare name and fully qualified name
        qualname = f"{cls.__module__}.{cls.__qualname__}"
        _MODEL_REGISTRY.setdefault(cls.__name__, {})[qualname] = cls

    def __init__(self, **kwargs: Any) -> None:
        self._loaded_relations: dict[str, Any] = {}
        self._exists: bool = False
        self._original: dict[str, Any] = {}
        self._init_keys: set[str] = set(kwargs)

        for name in self.__fields__:
            if name in kwargs:
                setattr(self, name, kwargs[name])
            elif name in self.__field_defaults__:
                setattr(self, name, self.__field_defaults__[name])
            else:
                setattr(self, name, None)

        for name, val in kwargs.items():
            if name not in self.__fields__:
                setattr(self, name, val)

    def _mark_persisted(self, keys: Iterable[str]) -> None:
        """Mark the instance as persisted and record original attribute values for dirty checking."""
        self._exists = True
        self._original = {
            k: getattr(self, k)
            for k in keys
            if k in self.__fields__ and hasattr(self, k)
        }

    @staticmethod
    def _persistable(value: Any) -> Any:
        return value.get_primary_key() if isinstance(value, Model) else value

    @classmethod
    def query(cls) -> ModelQueryBuilder[Self]:
        """Return a new query builder for this model."""
        return ModelQueryBuilder(cls)

    @classmethod
    async def find(cls, id: Any) -> Self | None:
        """Find a model instance by its primary key."""
        return await cls.where(cls.__primary_key__, id).first()

    @classmethod
    def where(
        cls, column: str, operator_or_value: Any, value: Any = _MISSING
    ) -> ModelQueryBuilder[Self]:
        """Add a basic where clause to the query."""
        return cls.query().where(column, operator_or_value, value)

    @classmethod
    def where_null(cls, column: str) -> ModelQueryBuilder[Self]:
        """Add a where null clause to the query."""
        return cls.query().where_null(column)

    @classmethod
    def where_not_null(cls, column: str) -> ModelQueryBuilder[Self]:
        """Add a where not null clause to the query."""
        return cls.query().where_not_null(column)

    @classmethod
    def where_raw(
        cls, sql: str, bindings: list[Any] | None = None
    ) -> ModelQueryBuilder[Self]:
        """Add a raw SQL where clause."""
        return cls.query().where_raw(sql, bindings)

    @classmethod
    def with_(cls, *relations: str) -> ModelQueryBuilder[Self]:
        """Eager-load specified relationships."""
        return cls.query().with_(*relations)

    @classmethod
    async def all(cls) -> list[Self]:
        """Retrieve all model instances."""
        return await cls.query().get()

    @classmethod
    async def create(cls, *args: dict[str, Any], **kwargs: Any) -> Self:
        """Create and persist a new model instance, honoring __fillable__ if defined."""
        data: dict[str, Any] = {}
        if args:
            data.update(args[0])
        data.update(kwargs)

        if cls.__fillable__ is not None:
            allowed = set(cls.__fillable__) | {cls.__primary_key__}
            data = {k: v for k, v in data.items() if k in allowed}

        for key, val in list(data.items()):
            if isinstance(val, Model):
                data[key] = val.get_primary_key()

        row = await cls.query().insert_returning(data)
        merged = {**data, **row}
        instance = cls(**merged)
        instance._mark_persisted(merged.keys())

        await EventDispatcher.get_instance().dispatch(
            f"{snake_case(cls.__name__)}.created", instance
        )
        return instance

    async def save(self) -> None:
        """Save the current model instance, writing only changed attributes on update."""
        pk_name = self.__primary_key__
        pk = getattr(self, pk_name, None)

        if self._exists:
            if pk is None:
                raise PrimaryKeyError(
                    f"Cannot save existing {self.__class__.__name__} without primary key '{pk_name}'."
                )

            changed = {
                name: self._persistable(getattr(self, name))
                for name, original in self._original.items()
                if name != pk_name and getattr(self, name, None) != original
            }
            if changed:
                if "updated_at" in self.__fields__:
                    now = datetime.now(UTC)
                    changed["updated_at"] = now
                    self.updated_at = now

                await self.__class__.query().where(pk_name, pk).update(changed)
                for name in changed:
                    self._original[name] = getattr(self, name)
            await EventDispatcher.get_instance().dispatch(
                f"{snake_case(self.__class__.__name__)}.updated", self
            )
            return

        data = {
            name: self._persistable(getattr(self, name))
            for name in self.__fields__
            if (
                (
                    name != pk_name
                    and (
                        name in self._init_keys or getattr(self, name, None) is not None
                    )
                )
                or (name == pk_name and getattr(self, name, None) is not None)
            )
        }
        row = await self.__class__.query().insert_returning(data)
        for col, val in row.items():
            if col in self.__fields__ or col == pk_name:
                setattr(self, col, val)
        self._mark_persisted([*data, *row])
        await EventDispatcher.get_instance().dispatch(
            f"{snake_case(self.__class__.__name__)}.created", self
        )

    async def delete(self) -> None:
        """Delete this model instance from the database."""
        pk_name = self.__primary_key__
        pk = getattr(self, pk_name, None)

        if not self._exists or pk is None:
            raise RuntimeError(f"Cannot delete unsaved {self.__class__.__name__}.")

        await self.__class__.query().where(pk_name, pk).delete()
        self._exists = False
        await EventDispatcher.get_instance().dispatch(
            f"{snake_case(self.__class__.__name__)}.deleted", self
        )

    async def refresh(self) -> Self:
        """Reload the model attributes from the database."""
        pk = self.get_primary_key()
        if not self._exists or pk is None:
            raise RuntimeError(f"Cannot refresh unsaved {self.__class__.__name__}.")

        refreshed = await self.__class__.find(pk)
        if refreshed is None:
            raise RuntimeError(
                f"Record for {self.__class__.__name__} with "
                f"{self.__primary_key__}={pk} no longer exists."
            )

        for k, v in refreshed.get_attributes().items():
            setattr(self, k, v)

        self._original = dict(refreshed._original)
        self._init_keys = set(refreshed._init_keys)
        self._loaded_relations.clear()
        return self

    def get_primary_key(self) -> Any:
        """Return the value of the primary key field."""
        return getattr(self, self.__primary_key__, None)

    def get_attributes(self) -> dict[str, Any]:
        """Return a dictionary of model attributes."""
        attrs: dict[str, Any] = {}
        for f in self.__fields__:
            if hasattr(self, f):
                attrs[f] = getattr(self, f)
        return attrs

    def to_dict(self) -> dict[str, Any]:
        """Convert the model and its loaded relations to a raw dictionary, omitting __hidden__ fields."""
        hidden = set(getattr(self, "__hidden__", ()) or ())
        data = {
            k: v for k, v in self.get_attributes().items() if k not in hidden
        }
        for rel_name, rel_value in self._loaded_relations.items():
            if rel_name in hidden:
                continue
            if isinstance(rel_value, list):
                data[rel_name] = [
                    item.to_dict() if isinstance(item, Model) else item
                    for item in rel_value
                ]
            elif isinstance(rel_value, Model):
                data[rel_name] = rel_value.to_dict()
            else:
                data[rel_name] = rel_value
        return data

    def has_many(
        self,
        related: str | type[Model],
        foreign_key: str | None = None,
        local_key: str = "id",
    ) -> HasMany:
        """Construct a HasMany relationship instance."""
        rel = HasMany(related, foreign_key, local_key)
        rel.owner_cls = self.__class__
        return rel

    def belongs_to(
        self,
        related: str | type[Model],
        foreign_key: str | None = None,
        local_key: str = "id",
    ) -> BelongsTo:
        """Construct a BelongsTo relationship instance."""
        rel = BelongsTo(related, foreign_key, local_key)
        rel.owner_cls = self.__class__
        return rel

    def has_one(
        self,
        related: str | type[Model],
        foreign_key: str | None = None,
        local_key: str = "id",
    ) -> HasOne:
        """Construct a HasOne relationship instance."""
        rel = HasOne(related, foreign_key, local_key)
        rel.owner_cls = self.__class__
        return rel

    def belongs_to_many(
        self,
        related: str | type[Model],
        pivot_table: str | None = None,
        foreign_pivot_key: str | None = None,
        related_pivot_key: str | None = None,
        parent_key: str = "id",
        related_key: str = "id",
    ) -> BelongsToMany:
        """Construct a BelongsToMany relationship instance."""
        rel = BelongsToMany(
            related,
            pivot_table,
            foreign_pivot_key,
            related_pivot_key,
            parent_key,
            related_key,
        )
        rel.owner_cls = self.__class__
        return rel

    def __repr__(self) -> str:
        attrs = ", ".join(f"{k}={v!r}" for k, v in self.get_attributes().items())
        return f"{self.__class__.__name__}({attrs})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, self.__class__):
            return False
        pk = self.get_primary_key()
        other_pk = other.get_primary_key()
        if pk is not None and other_pk is not None:
            return pk == other_pk  # type: ignore[no-any-return]
        return self is other

    def __hash__(self) -> int:
        pk = self.get_primary_key()
        if pk is not None:
            return hash((self.__class__, pk))
        return hash(id(self))


__all__ = [
    "_MODEL_REGISTRY",
    "Model",
    "ModelQueryBuilder",
    "PrimaryKeyError",
    "restore_model_registry",
    "snapshot_model_registry",
]
