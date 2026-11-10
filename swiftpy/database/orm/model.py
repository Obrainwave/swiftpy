from __future__ import annotations

import copy
import inspect
import json
import logging
import math
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
from swiftpy.database.orm.fields import Field, FieldInfo, VectorFieldInfo
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
from swiftpy.database.pagination import Paginator, SimplePaginator
from swiftpy.database.query_builder import Query
from swiftpy.database.transaction import get_active_connection
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.support.strings import pluralize, snake_case

T = TypeVar("T", bound="Model")
_MISSING = object()
logger = logging.getLogger(__name__)


class PrimaryKeyError(RuntimeError, ValueError):
    """Raised when a primary key is invalid, composite, or missing on a persisted model."""


class MassAssignmentError(ValueError):
    """Raised when an attribute cannot be mass-assigned."""


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

    def offset(self, value: int) -> Self:
        self.builder.offset(value)
        return self

    def take(self, value: int) -> Self:
        self.builder.take(value)
        return self

    def skip(self, value: int) -> Self:
        self.builder.skip(value)
        return self

    def group_by(self, *columns: str | Sequence[str]) -> Self:
        self.builder.group_by(*columns)
        return self

    def having(
        self,
        column: str,
        operator_or_value: Any,
        value: Any = _MISSING,
        boolean: str = "AND",
    ) -> Self:
        self.builder.having(column, operator_or_value, value, boolean=boolean)
        return self

    def or_having(
        self, column: str, operator_or_value: Any, value: Any = _MISSING
    ) -> Self:
        self.builder.or_having(column, operator_or_value, value)
        return self

    def having_raw(
        self,
        sql: str,
        bindings: list[Any] | None = None,
        boolean: str = "AND",
    ) -> Self:
        self.builder.having_raw(sql, bindings, boolean=boolean)
        return self

    def or_having_raw(
        self, sql: str, bindings: list[Any] | None = None
    ) -> Self:
        self.builder.or_having_raw(sql, bindings)
        return self

    def select_raw(
        self, sql: str, bindings: list[Any] | None = None
    ) -> Self:
        self.builder.select_raw(sql, bindings)
        return self

    def select_count(
        self, column: str = "*", alias: str | None = None
    ) -> Self:
        self.builder.select_count(column, alias)
        return self

    def select_sum(
        self, column: str, alias: str | None = None
    ) -> Self:
        self.builder.select_sum(column, alias)
        return self

    def select_avg(
        self, column: str, alias: str | None = None
    ) -> Self:
        self.builder.select_avg(column, alias)
        return self

    def select_min(
        self, column: str, alias: str | None = None
    ) -> Self:
        self.builder.select_min(column, alias)
        return self

    def select_max(
        self, column: str, alias: str | None = None
    ) -> Self:
        self.builder.select_max(column, alias)
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
            inst = self.model_cls._hydrate(row)
            instances.append(inst)

        if self._eager_loads and instances:
            await self._load_relations(instances)

        return instances

    async def first(self) -> T | None:
        res = await self.clone().limit(1).get()
        return res[0] if res else None

    async def paginate(
        self, per_page: int = 15, page: int = 1
    ) -> Paginator[T]:
        """
        Execute the model query with length-aware pagination, counting total records/groups.
        """
        if isinstance(page, bool) or not isinstance(page, int):
            raise TypeError("page must be an integer.")
        if page < 1:
            raise ValueError("page must be >= 1.")
        if isinstance(per_page, bool) or not isinstance(per_page, int):
            raise TypeError("per_page must be an integer.")
        if per_page < 1:
            raise ValueError("per_page must be >= 1.")

        offset = (page - 1) * per_page

        count_builder = self.clone()
        count_builder.builder._limit_value = None
        count_builder.builder._offset_value = None
        count_builder.builder._order_column = None

        if (
            count_builder.builder._groups
            or count_builder.builder._havings
            or count_builder.builder._raw_havings
        ):
            if (
                count_builder.builder._columns == ["*"]
                and count_builder.builder._groups
            ):
                count_builder.builder._columns = list(count_builder.builder._groups)

            inner_sql, inner_bindings = count_builder.builder.to_sql(1)
            count_sql = f"SELECT COUNT(*) AS total FROM ({inner_sql}) AS grouped_results"
            conn = await count_builder.builder._connection()
            try:
                val = await conn.fetchval(count_sql, *inner_bindings)
                total = int(val or 0)
            finally:
                await count_builder.builder._release(conn)
        else:
            total = await count_builder.count()

        data_builder = self.clone()
        data_builder.limit(per_page).offset(offset)
        items = await data_builder.get()

        last_page = max(1, math.ceil(total / per_page))
        return Paginator(
            items=items,
            total=total,
            per_page=per_page,
            current_page=page,
            last_page=last_page,
        )

    async def simple_paginate(
        self, per_page: int = 15, page: int = 1
    ) -> SimplePaginator[T]:
        """
        Execute the model query with lightweight pagination without counting total records.
        """
        if isinstance(page, bool) or not isinstance(page, int):
            raise TypeError("page must be an integer.")
        if page < 1:
            raise ValueError("page must be >= 1.")
        if isinstance(per_page, bool) or not isinstance(per_page, int):
            raise TypeError("per_page must be an integer.")
        if per_page < 1:
            raise ValueError("per_page must be >= 1.")

        offset = (page - 1) * per_page

        data_builder = self.clone()
        data_builder.limit(per_page + 1).offset(offset)
        raw_items = await data_builder.get()

        has_more = len(raw_items) > per_page
        items = raw_items[:per_page] if has_more else raw_items

        return SimplePaginator(
            items=items,
            per_page=per_page,
            current_page=page,
            has_more=has_more,
        )

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
        if not instances or not self._eager_loads:
            return

        # Group eager loads by top-level relation name
        tree: dict[str, list[str]] = {}
        for path in self._eager_loads:
            parts = path.split(".", 1)
            top = parts[0]
            sub = parts[1] if len(parts) > 1 else None
            tree.setdefault(top, [])
            if sub:
                tree[top].append(sub)

        for top_name, sub_paths in tree.items():
            relation = self.model_cls.__relations_config__.get(top_name)
            if not relation:
                raise ValueError(
                    f"Relation '{top_name}' not found on model {self.model_cls.__name__}"
                )
            await relation.eager_load(instances)

            if sub_paths:
                related_items: list[Model] = []
                for inst in instances:
                    val = inst._loaded_relations.get(top_name)
                    if isinstance(val, list):
                        related_items.extend(
                            item for item in val if isinstance(item, Model)
                        )
                    elif isinstance(val, Model):
                        related_items.append(val)

                if related_items:
                    target_model = relation.get_target_model()
                    sub_builder = target_model.query()
                    sub_builder._eager_loads = sub_paths
                    await sub_builder._load_relations(related_items)


def _validate_model_vector(field_name: str, value: Any, dimensions: int) -> list[float]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"Field '{field_name}' must be a list of floats, got {type(value).__name__}")
    if len(value) != dimensions:
        raise ValueError(
            f"Field '{field_name}' vector dimension {len(value)} does not match expected {dimensions}"
        )
    parsed: list[float] = []
    for idx, x in enumerate(value):
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            raise TypeError(f"Field '{field_name}' element at index {idx} must be a number, got {type(x).__name__}")
        fx = float(x)
        if math.isnan(fx) or math.isinf(fx):
            raise ValueError(f"Field '{field_name}' element at index {idx} is NaN or Infinite.")
        parsed.append(fx)
    return parsed


class Model:
    """Active Record base model with typed attributes, dirty tracking, and relationship support."""

    __table__: ClassVar[str]
    __primary_key__: ClassVar[str] = "id"
    __fillable__: ClassVar[set[str] | list[str] | tuple[str, ...] | None] = None
    __guarded__: ClassVar[set[str] | list[str] | tuple[str, ...] | None] = None
    __hidden__: ClassVar[set[str] | list[str] | tuple[str, ...]] = ()
    strict_events: ClassVar[bool] = False
    prevent_lazy_loading: ClassVar[bool] = False

    __fields__: ClassVar[dict[str, Any]]
    __field_defaults__: ClassVar[dict[str, Any]]
    __field_factories__: ClassVar[dict[str, Any]]
    __vector_fields__: ClassVar[dict[str, VectorFieldInfo]]
    __relations_config__: ClassVar[dict[str, Relation]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        # Validate primary key declaration (composite keys are explicitly unsupported)
        raw_pk = getattr(cls, "__primary_key__", "id")
        if not isinstance(raw_pk, str) or "," in raw_pk or not raw_pk.strip():
            raise PrimaryKeyError(
                f"Composite or non-string primary keys are not supported on {cls.__name__}; "
                "__primary_key__ must be a single valid column identifier."
            )
        cls.__primary_key__ = validate_identifier(raw_pk, param_name="__primary_key__")

        # Determine table name if not explicitly set (overridable via __table__)
        if "__table__" not in cls.__dict__:
            cls.__table__ = pluralize(snake_case(cls.__name__))

        # Inherit fields from parent Model classes
        fields: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        factories: dict[str, Any] = {}

        for base in reversed(cls.__mro__[1:]):
            fields.update(getattr(base, "__fields__", {}))
            defaults.update(getattr(base, "__field_defaults__", {}))
            factories.update(getattr(base, "__field_factories__", {}))

        # Resolve typed class attributes using typing.get_type_hints
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
                val = cls.__dict__[name]
                if isinstance(val, VectorFieldInfo):
                    pass
                elif isinstance(val, FieldInfo):
                    if val.primary_key:
                        cls.__primary_key__ = name
                    if val.default_factory is not None:
                        factories[name] = val.default_factory
                    elif val.default is not _MISSING:
                        defaults[name] = val.default
                elif callable(val) and not isinstance(val, (classmethod, staticmethod, property)):
                    factories[name] = val
                else:
                    defaults[name] = val

        for name, val in cls.__dict__.items():
            if name.startswith("__") or name in fields:
                continue
            if isinstance(val, VectorFieldInfo):
                fields[name] = list[float]
            elif isinstance(val, FieldInfo):
                fields[name] = Any
                if val.primary_key:
                    cls.__primary_key__ = name
                if val.default_factory is not None:
                    factories[name] = val.default_factory
                elif val.default is not _MISSING:
                    defaults[name] = val.default

        # Guarantee primary key field presence
        if cls.__primary_key__ not in fields:
            fields[cls.__primary_key__] = int

        cls.__fields__ = fields
        cls.__field_defaults__ = defaults
        cls.__field_factories__ = factories

        # Discover Vector fields
        vectors: dict[str, VectorFieldInfo] = {}
        for base in reversed(cls.__mro__[1:]):
            vectors.update(getattr(base, "__vector_fields__", {}))
        for name, val in cls.__dict__.items():
            if isinstance(val, VectorFieldInfo):
                vectors[name] = val
        cls.__vector_fields__ = vectors

        # Inherit relations from ancestors and discover newly declared relations
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

        # Register model globally under bare name and fully qualified name
        qualname = f"{cls.__module__}.{cls.__qualname__}"
        _MODEL_REGISTRY.setdefault(cls.__name__, {})[qualname] = cls

    def fill(self, **attributes: Any) -> Self:
        """Mass-assign attributes to the model instance after validating mass assignment rules."""
        guarded = getattr(self, "__guarded__", None)
        fillable = getattr(self, "__fillable__", None)

        if guarded is None and fillable is None:
            raise MassAssignmentError(
                f"Model '{self.__class__.__name__}' has no __fillable__ or __guarded__ declared; "
                "mass assignment is denied by default (Decision D-4). "
                "Declare __fillable__ or set __guarded__ = []."
            )

        for key, val in attributes.items():
            if key.startswith("_"):
                raise MassAssignmentError(
                    f"Cannot mass-assign private attribute '{key}'."
                )
            if key not in self.__fields__ and key not in getattr(self, "__relations_config__", {}):
                raise MassAssignmentError(
                    f"Unknown attribute '{key}' for model '{self.__class__.__name__}'."
                )

            if guarded is not None:
                if key in guarded:
                    raise MassAssignmentError(
                        f"Attribute '{key}' is guarded on model '{self.__class__.__name__}'."
                    )
                setattr(self, key, val)
                self._init_keys.add(key)
            elif fillable is not None:
                if key in fillable or key == self.__primary_key__:
                    setattr(self, key, val)
                    self._init_keys.add(key)
                # Unlisted fields in fillable are omitted/ignored
        return self

    def force_fill(self, **attributes: Any) -> Self:
        """Mass-assign attributes bypassing fillable/guarded rules (still checks private/unknown)."""
        for key, val in attributes.items():
            if key.startswith("_"):
                raise MassAssignmentError(
                    f"Cannot mass-assign private attribute '{key}'."
                )
            if key not in self.__fields__ and key not in getattr(self, "__relations_config__", {}):
                raise MassAssignmentError(
                    f"Unknown attribute '{key}' for model '{self.__class__.__name__}'."
                )
            setattr(self, key, val)
            self._init_keys.add(key)
        return self

    @classmethod
    def _hydrate(cls, row: dict[str, Any]) -> Self:
        """Hydrate a model instance from raw database columns without mass-assignment checks."""
        inst = cls.__new__(cls)
        inst._loaded_relations = {}
        inst._exists = True
        inst._original = {}
        inst._init_keys = set(row.keys())
        inst._modified_keys = set()
        inst._temp_visible = set()
        inst._temp_hidden = set()

        for name in cls.__fields__:
            if name in row:
                setattr(inst, name, cls._coerce_db_value(name, row[name]))
            elif name in cls.__field_factories__:
                setattr(inst, name, cls.__field_factories__[name]())
            elif name in cls.__field_defaults__:
                setattr(inst, name, copy.deepcopy(cls.__field_defaults__[name]))
            else:
                setattr(inst, name, None)

        for name, val in row.items():
            if name not in cls.__fields__:
                setattr(inst, name, val)

        inst._mark_persisted(row.keys())
        return inst

    def __init__(self, **kwargs: Any) -> None:
        self._loaded_relations: dict[str, Any] = {}
        self._exists: bool = False
        self._original: dict[str, Any] = {}
        self._init_keys: set[str] = set()
        self._modified_keys: set[str] = set()
        self._temp_visible: set[str] = set()
        self._temp_hidden: set[str] = set()

        for name in self.__fields__:
            if name in self.__field_factories__:
                setattr(self, name, self.__field_factories__[name]())
            elif name in self.__field_defaults__:
                setattr(self, name, copy.deepcopy(self.__field_defaults__[name]))
            else:
                setattr(self, name, None)

        if kwargs:
            self.fill(**kwargs)

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(self, "__vector_fields__") and name in self.__vector_fields__ and value is not None:
            v_info = self.__vector_fields__[name]
            value = _validate_model_vector(name, value, v_info.dimensions)
        if (
            hasattr(self, "_exists")
            and self._exists
            and name in getattr(self, "__fields__", {})
        ):
            orig = getattr(self, "_original", None)
            if orig is not None and name not in orig:
                if not hasattr(self, "_modified_keys"):
                    self._modified_keys = set()
                self._modified_keys.add(name)
        super().__setattr__(name, value)

    def _mark_persisted(self, keys: Iterable[str]) -> None:
        """Mark the instance as persisted and record deep-copied original attribute values for dirty checking."""
        self._exists = True
        self._original = {
            k: copy.deepcopy(getattr(self, k))
            for k in keys
            if k in self.__fields__ and hasattr(self, k)
        }
        if hasattr(self, "_modified_keys"):
            self._modified_keys.clear()

    def is_dirty(self, *attributes: str) -> bool:
        """Check if the model or specified attributes have changed since being persisted."""
        if not self._exists:
            return bool(self._init_keys or attributes)
        dirty = self.get_dirty()
        if not attributes:
            return bool(dirty)
        return any(attr in dirty for attr in attributes)

    def get_dirty(self) -> dict[str, Any]:
        """Return a mapping of attributes that have changed since being persisted to their current values."""
        if not self._exists:
            return {
                k: getattr(self, k)
                for k in self.__fields__
                if hasattr(self, k) and getattr(self, k) is not None
            }

        dirty: dict[str, Any] = {}
        for name, original in self._original.items():
            if name == self.__primary_key__:
                continue
            if not hasattr(self, name):
                continue
            current_val = getattr(self, name)
            if current_val != original:
                dirty[name] = current_val

        for name in getattr(self, "_modified_keys", ()):
            if name == self.__primary_key__:
                continue
            if hasattr(self, name):
                dirty[name] = getattr(self, name)

        return dirty

    @classmethod
    def _coerce_db_value(cls, field_name: str, value: Any) -> Any:
        """Coerce database values such as JSON strings back to typed Python structures if needed."""
        if field_name in getattr(cls, "__vector_fields__", {}):
            if value is None:
                return None
            if isinstance(value, str):
                clean_s = value.strip("[] \t\n\r")
                if not clean_s:
                    return []
                try:
                    return [float(x.strip()) for x in clean_s.split(",") if x.strip()]
                except (ValueError, TypeError):
                    return value
            if isinstance(value, (list, tuple)):
                return [float(x) for x in value]
            return value

        if isinstance(value, str) and field_name in cls.__fields__:
            hint = cls.__fields__[field_name]
            origin = get_origin(hint) or hint
            is_json_type = (
                origin in (dict, list)
                or (
                    isinstance(hint, str)
                    and hint.startswith(("dict", "list"))
                )
            )
            if is_json_type:
                try:
                    return json.loads(value)
                except (ValueError, TypeError):
                    return value
        return value

    @staticmethod
    def _persistable(value: Any) -> Any:
        if isinstance(value, Model):
            return value.get_primary_key()
        if (
            isinstance(value, list)
            and value
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)
        ):
            return f"[{','.join(str(float(x)) for x in value)}]"
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return value

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
    def limit(cls, value: int) -> ModelQueryBuilder[Self]:
        """Set query result limit."""
        return cls.query().limit(value)

    @classmethod
    def offset(cls, value: int) -> ModelQueryBuilder[Self]:
        """Set query result offset."""
        return cls.query().offset(value)

    @classmethod
    def take(cls, value: int) -> ModelQueryBuilder[Self]:
        """Alias for limit()."""
        return cls.query().take(value)

    @classmethod
    def skip(cls, value: int) -> ModelQueryBuilder[Self]:
        """Alias for offset()."""
        return cls.query().skip(value)

    @classmethod
    def order_by(
        cls, column: str, direction: str = "ASC"
    ) -> ModelQueryBuilder[Self]:
        """Order the model query by a column."""
        return cls.query().order_by(column, direction)

    @classmethod
    def group_by(cls, *columns: str | Sequence[str]) -> ModelQueryBuilder[Self]:
        """Add GROUP BY columns to the query."""
        return cls.query().group_by(*columns)

    @classmethod
    def having(
        cls,
        column: str,
        operator_or_value: Any,
        value: Any = _MISSING,
        boolean: str = "AND",
    ) -> ModelQueryBuilder[Self]:
        """Add a HAVING condition to the model query."""
        return cls.query().having(column, operator_or_value, value, boolean=boolean)

    @classmethod
    def or_having(
        cls, column: str, operator_or_value: Any, value: Any = _MISSING
    ) -> ModelQueryBuilder[Self]:
        """Add an OR HAVING condition to the model query."""
        return cls.query().or_having(column, operator_or_value, value)

    @classmethod
    def having_raw(
        cls,
        sql: str,
        bindings: list[Any] | None = None,
        boolean: str = "AND",
    ) -> ModelQueryBuilder[Self]:
        """Add a raw HAVING condition to the model query."""
        return cls.query().having_raw(sql, bindings, boolean=boolean)

    @classmethod
    def or_having_raw(
        cls, sql: str, bindings: list[Any] | None = None
    ) -> ModelQueryBuilder[Self]:
        """Add an OR raw HAVING condition to the model query."""
        return cls.query().or_having_raw(sql, bindings)

    @classmethod
    async def paginate(
        cls, per_page: int = 15, page: int = 1
    ) -> Paginator[Self]:
        """Execute length-aware pagination on the model."""
        return await cls.query().paginate(per_page=per_page, page=page)

    @classmethod
    async def simple_paginate(
        cls, per_page: int = 15, page: int = 1
    ) -> SimplePaginator[Self]:
        """Execute lightweight pagination on the model without total count."""
        return await cls.query().simple_paginate(per_page=per_page, page=page)

    @classmethod
    async def _dispatch_lifecycle_event(
        cls,
        event_suffix: str,
        instance: Model,
        *,
        is_after_event: bool = False,
    ) -> None:
        """
        Dispatch a model lifecycle event.

        - Before events (is_after_event=False) execute inline immediately. If any listener raises,
          the operation is aborted and the exception propagates.
        - After events (is_after_event=True) are deferred to transaction commit via on_commit if
          inside an active transaction. By default (strict_events=False), listener exceptions do
          not fail an already committed database write.
        """
        event_name = f"{snake_case(cls.__name__)}.{event_suffix}"

        async def _do_dispatch() -> None:
            try:
                await EventDispatcher.get_instance().dispatch(event_name, instance)
            except Exception:
                if is_after_event and not getattr(cls, "strict_events", False):
                    logger.exception(
                        "Error executing lifecycle event listener for %s",
                        event_name,
                    )
                else:
                    raise

        if is_after_event:
            active_conn = get_active_connection()
            if active_conn is not None and hasattr(active_conn, "on_commit"):
                active_conn.on_commit(_do_dispatch)
                return

        await _do_dispatch()

    @classmethod
    async def create(cls, *args: dict[str, Any], **kwargs: Any) -> Self:
        """Create and persist a new model instance, validating mass assignment rules."""
        data: dict[str, Any] = {}
        if args:
            data.update(args[0])
        data.update(kwargs)

        instance = cls()
        instance.fill(**data)

        await cls._dispatch_lifecycle_event("creating", instance, is_after_event=False)

        insert_data = {
            k: instance._persistable(getattr(instance, k))
            for k in instance._init_keys
            if k in cls.__fields__
        }
        row = await cls.query().insert_returning(insert_data)
        for col, val in row.items():
            if col in cls.__fields__:
                setattr(instance, col, cls._coerce_db_value(col, val))
            elif col == cls.__primary_key__:
                setattr(instance, col, val)
        instance._mark_persisted([*insert_data.keys(), *row.keys()])

        await cls._dispatch_lifecycle_event("created", instance, is_after_event=True)
        return instance

    @classmethod
    async def force_create(cls, *args: dict[str, Any], **kwargs: Any) -> Self:
        """Create and persist a new model instance, bypassing fillable/guarded checks for trusted code."""
        data: dict[str, Any] = {}
        if args:
            data.update(args[0])
        data.update(kwargs)

        instance = cls()
        instance.force_fill(**data)

        await cls._dispatch_lifecycle_event("creating", instance, is_after_event=False)

        insert_data = {
            k: instance._persistable(getattr(instance, k))
            for k in instance._init_keys
            if k in cls.__fields__
        }
        row = await cls.query().insert_returning(insert_data)
        for col, val in row.items():
            if col in cls.__fields__:
                setattr(instance, col, cls._coerce_db_value(col, val))
            elif col == cls.__primary_key__:
                setattr(instance, col, val)
        instance._mark_persisted([*insert_data.keys(), *row.keys()])

        await cls._dispatch_lifecycle_event("created", instance, is_after_event=True)
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
                name: self._persistable(val)
                for name, val in self.get_dirty().items()
                if name != pk_name
            }
            if not changed:
                return

            await self.__class__._dispatch_lifecycle_event(
                "updating", self, is_after_event=False
            )

            if "updated_at" in self.__fields__:
                now = datetime.now(UTC)
                changed["updated_at"] = now
                self.updated_at = now

            await self.__class__.query().where(pk_name, pk).update(changed)
            for name in changed:
                self._original[name] = copy.deepcopy(getattr(self, name))
            if hasattr(self, "_modified_keys"):
                self._modified_keys.clear()
            await self.__class__._dispatch_lifecycle_event(
                "updated", self, is_after_event=True
            )
            return

        await self.__class__._dispatch_lifecycle_event(
            "creating", self, is_after_event=False
        )

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
        await self.__class__._dispatch_lifecycle_event(
            "created", self, is_after_event=True
        )

    async def delete(self) -> None:
        """Delete this model instance from the database."""
        pk_name = self.__primary_key__
        pk = getattr(self, pk_name, None)

        if not self._exists or pk is None:
            raise RuntimeError(f"Cannot delete unsaved {self.__class__.__name__}.")

        await self.__class__._dispatch_lifecycle_event(
            "deleting", self, is_after_event=False
        )
        await self.__class__.query().where(pk_name, pk).delete()
        self._exists = False
        await self.__class__._dispatch_lifecycle_event(
            "deleted", self, is_after_event=True
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

    def make_visible(self, *fields: str) -> Self:
        """Temporarily make specified fields visible in to_dict() serialization."""
        self._temp_visible.update(fields)
        self._temp_hidden.difference_update(fields)
        return self

    def make_hidden(self, *fields: str) -> Self:
        """Temporarily hide specified fields from to_dict() serialization."""
        self._temp_hidden.update(fields)
        self._temp_visible.difference_update(fields)
        return self

    def to_dict(self, include_hidden: bool = False) -> dict[str, Any]:
        """Convert the model and its loaded relations to a raw dictionary, omitting __hidden__ fields."""
        if include_hidden:
            hidden: set[str] = set()
        else:
            base_hidden = set(getattr(self, "__hidden__", ()) or ())
            hidden = (base_hidden | self._temp_hidden) - self._temp_visible

        data = {
            k: v for k, v in self.get_attributes().items() if k not in hidden
        }
        for rel_name, rel_value in self._loaded_relations.items():
            if rel_name in hidden:
                continue
            if isinstance(rel_value, list):
                data[rel_name] = [
                    (
                        item.to_dict(include_hidden=include_hidden)
                        if isinstance(item, Model)
                        else item
                    )
                    for item in rel_value
                ]
            elif isinstance(rel_value, Model):
                data[rel_name] = rel_value.to_dict(include_hidden=include_hidden)
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
        pk = self.get_primary_key()
        return f"<{self.__class__.__name__} {self.__primary_key__}={pk!r}>"

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return False
        pk = self.get_primary_key()
        other_pk = other.get_primary_key()
        if pk is not None and other_pk is not None:
            return bool(pk == other_pk)
        return self is other

    def __hash__(self) -> int:
        pk = self.get_primary_key()
        if pk is not None:
            return hash((self.__class__, pk))
        return hash(id(self))


__all__ = [
    "_MODEL_REGISTRY",
    "Field",
    "FieldInfo",
    "MassAssignmentError",
    "Model",
    "ModelQueryBuilder",
    "PrimaryKeyError",
    "restore_model_registry",
    "snapshot_model_registry",
]
