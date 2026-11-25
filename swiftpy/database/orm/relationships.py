from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, NoReturn, Self, cast, overload

from swiftpy.database.orm.registry import _MODEL_REGISTRY
from swiftpy.database.query_builder import Column, Query, QueryError
from swiftpy.database.transaction import db
from swiftpy.support.strings import snake_case

if TYPE_CHECKING:
    from swiftpy.database.orm.model import Model, ModelQueryBuilder
    from swiftpy.database.pagination import Paginator, SimplePaginator
    from swiftpy.database.query_builder import QueryBuilder
    from swiftpy.schemas.base import BaseSchema


_MISSING = object()
_CHUNK_SIZE = 1000


def _chunk_list[T](items: list[T], chunk_size: int = _CHUNK_SIZE) -> list[list[T]]:
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


class RelationNotLoadedError(RuntimeError):
    """Raised when attempting to synchronously access an un-eager-loaded relationship."""


class LazyLoadingDisabledError(RuntimeError):
    """Raised when attempting to access an unloaded relationship while prevent_lazy_loading is enabled."""


class LoadedList[T: Model](list[T]):
    """A list of eager-loaded models that can also be directly awaited."""

    def __await__(self) -> Generator[Any, None, Self]:
        async def _resolved() -> Self:
            return self

        return _resolved().__await__()


class Relation[T: Model]:
    """Base relation definition for Active Record Models."""

    def __init__(
        self,
        related_model: str | type[T],
        foreign_key: str | None = None,
        local_key: str | None = None,
    ) -> None:
        self._related_cls: type[T] | None = None
        if isinstance(related_model, str):
            self.related_model_name = related_model
        else:
            self.related_model_name = related_model.__name__
            self._related_cls = related_model

        self.foreign_key = foreign_key
        self.local_key = local_key
        self.name: str = ""
        self.owner_cls: type[Model] | None = None

    def _resolve_local_key(self, instance_or_cls: Model | type[Model] | None = None) -> str:
        """Resolve local key dynamically from explicit override or owner model primary key."""
        if self.local_key is not None:
            return self.local_key
        if isinstance(instance_or_cls, type):
            return getattr(instance_or_cls, "__primary_key__", "id")
        if instance_or_cls is not None:
            return getattr(instance_or_cls.__class__, "__primary_key__", "id")
        if self.owner_cls is not None:
            return getattr(self.owner_cls, "__primary_key__", "id")
        return "id"

    def _get_target_model_typed(self) -> type[T]:
        """Resolve the typed related Model class for strongly typed query construction."""
        if self._related_cls is not None:
            return self._related_cls
        return cast(type[T], self.get_target_model())

    def get_target_model(self) -> type[Model]:
        """Resolve the related Model class, preferring direct class references and module scope."""
        if self._related_cls is not None:
            return self._related_cls

        candidates = _MODEL_REGISTRY.get(self.related_model_name, {})
        if not candidates:
            raise RuntimeError(
                f"Model '{self.related_model_name}' has not been registered."
            )

        if len(candidates) == 1:
            return next(iter(candidates.values()))

        owner_module = self.owner_cls.__module__ if self.owner_cls else None
        same_module = [c for c in candidates.values() if c.__module__ == owner_module]
        if len(same_module) == 1:
            return same_module[0]

        raise RuntimeError(
            f"Model name '{self.related_model_name}' is ambiguous: {sorted(candidates)}. "
            "Pass the class itself instead of a string."
        )

    async def eager_load(self, instances: Sequence[Model]) -> None:
        """Batch load related models for instances in a single query."""
        raise NotImplementedError

    def build_lazy_query(self, instance: Model) -> ModelQueryBuilder[T]:
        """Construct a query builder scoped to this relationship."""
        raise NotImplementedError

    def _build_query_builder(self, instance: Model) -> _BaseRelationshipQueryBuilder[T]:
        """Build the appropriate relationship query builder for an unloaded relation."""
        builder = self.build_lazy_query(instance)
        return RelationshipQueryBuilder(instance, self, builder)

    @staticmethod
    def _ensure_lazy_allowed(
        instance: Model, owner: type[Model] | None, relation_name: str
    ) -> None:
        if getattr(instance, "prevent_lazy_loading", False) or getattr(
            owner, "prevent_lazy_loading", False
        ):
            raise LazyLoadingDisabledError(
                f"Lazy loading is disabled on {instance.__class__.__name__}. "
                f"Relation '{relation_name}' must be eager-loaded using with_()."
            )


class ManyRelation[T: Model](Relation[T]):
    """Base descriptor for to-many Active Record relationships."""

    def _build_query_builder(self, instance: Model) -> RelationshipQueryBuilder[T]:
        builder = self.build_lazy_query(instance)
        return RelationshipQueryBuilder(instance, self, builder)

    @overload
    def __get__(self, instance: None, owner: type[Model] | None = None) -> Self: ...

    @overload
    def __get__(
        self, instance: Model, owner: type[Model] | None = None
    ) -> RelationshipQueryBuilder[T]: ...

    def __get__(
        self, instance: Model | None, owner: type[Model] | None = None
    ) -> Self | RelationshipQueryBuilder[T] | Sequence[Model] | Model | None:
        if instance is None:
            return self

        if hasattr(instance, "_loaded_relations") and self.name in instance._loaded_relations:
            return instance._loaded_relations[self.name]

        self._ensure_lazy_allowed(instance, owner, self.name)
        return self._build_query_builder(instance)


class SingleRelation[T: Model](Relation[T]):
    """Base descriptor for to-one Active Record relationships."""

    def _build_query_builder(
        self, instance: Model
    ) -> SingleRelationshipQueryBuilder[T]:
        builder = self.build_lazy_query(instance)
        return SingleRelationshipQueryBuilder(instance, self, builder)

    @overload
    def __get__(self, instance: None, owner: type[Model] | None = None) -> Self: ...

    @overload
    def __get__(
        self, instance: Model, owner: type[Model] | None = None
    ) -> SingleRelationshipQueryBuilder[T]: ...

    def __get__(
        self, instance: Model | None, owner: type[Model] | None = None
    ) -> Self | SingleRelationshipQueryBuilder[T] | Sequence[Model] | Model | None:
        if instance is None:
            return self

        if hasattr(instance, "_loaded_relations") and self.name in instance._loaded_relations:
            return instance._loaded_relations[self.name]

        self._ensure_lazy_allowed(instance, owner, self.name)
        return self._build_query_builder(instance)


class HasMany[T: Model](ManyRelation[T]):
    """One-to-many relationship."""

    @overload
    def __init__(
        self,
        related_model: type[T],
        foreign_key: str | None = None,
        local_key: str | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self: HasMany[Any],
        related_model: str,
        foreign_key: str | None = None,
        local_key: str | None = None,
    ) -> None: ...

    def __init__(
        self,
        related_model: str | type[T],
        foreign_key: str | None = None,
        local_key: str | None = None,
    ) -> None:
        super().__init__(related_model, foreign_key=foreign_key, local_key=local_key)

    async def eager_load(self, instances: Sequence[Model]) -> None:
        if not instances:
            return

        target_model = self.get_target_model()
        owner_cls = (
            self.owner_cls
            if self.owner_cls is not None
            else instances[0].__class__
        )
        fk = self.foreign_key or f"{snake_case(owner_cls.__name__)}_id"
        local_key = self._resolve_local_key(owner_cls)

        local_keys = [
            getattr(inst, local_key)
            for inst in instances
            if getattr(inst, local_key, None) is not None
        ]
        unique_keys = list(set(local_keys))

        if not unique_keys:
            for inst in instances:
                inst._loaded_relations[self.name] = LoadedList()
            return

        related_instances: list[Model] = []
        for chunk in _chunk_list(unique_keys):
            rows = await target_model.where(fk, "IN", chunk).get()
            related_instances.extend(rows)

        grouped: dict[Any, list[Model]] = {}
        for rel in related_instances:
            key = getattr(rel, fk, None)
            grouped.setdefault(key, []).append(rel)

        for inst in instances:
            key = getattr(inst, local_key, None)
            inst._loaded_relations[self.name] = LoadedList(grouped.get(key, []))

    def build_lazy_query(self, instance: Model) -> ModelQueryBuilder[T]:
        target_model = self._get_target_model_typed()
        owner_name = (
            self.owner_cls.__name__
            if self.owner_cls is not None
            else instance.__class__.__name__
        )
        fk = self.foreign_key or f"{snake_case(owner_name)}_id"
        local_key = self._resolve_local_key(instance)
        val = getattr(instance, local_key, None)
        return target_model.where(fk, val)


class HasOne[T: Model](SingleRelation[T]):
    """One-to-one relationship."""

    @overload
    def __init__(
        self,
        related_model: type[T],
        foreign_key: str | None = None,
        local_key: str | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self: HasOne[Any],
        related_model: str,
        foreign_key: str | None = None,
        local_key: str | None = None,
    ) -> None: ...

    def __init__(
        self,
        related_model: str | type[T],
        foreign_key: str | None = None,
        local_key: str | None = None,
    ) -> None:
        super().__init__(related_model, foreign_key=foreign_key, local_key=local_key)

    async def eager_load(self, instances: Sequence[Model]) -> None:
        if not instances:
            return

        target_model = self.get_target_model()
        owner_cls = (
            self.owner_cls
            if self.owner_cls is not None
            else instances[0].__class__
        )
        fk = self.foreign_key or f"{snake_case(owner_cls.__name__)}_id"
        local_key = self._resolve_local_key(owner_cls)

        local_keys = [
            getattr(inst, local_key)
            for inst in instances
            if getattr(inst, local_key, None) is not None
        ]
        unique_keys = list(set(local_keys))

        if not unique_keys:
            for inst in instances:
                inst._loaded_relations[self.name] = None
            return

        related: list[Model] = []
        for chunk in _chunk_list(unique_keys):
            rows = await target_model.where(fk, "IN", chunk).get()
            related.extend(rows)

        mapped: dict[Any, Model] = {}
        for r in related:
            key = getattr(r, fk, None)
            if key not in mapped:
                mapped[key] = r

        for inst in instances:
            key = getattr(inst, local_key, None)
            inst._loaded_relations[self.name] = mapped.get(key, None)

    def build_lazy_query(self, instance: Model) -> ModelQueryBuilder[T]:
        target_model = self._get_target_model_typed()
        owner_name = (
            self.owner_cls.__name__
            if self.owner_cls is not None
            else instance.__class__.__name__
        )
        fk = self.foreign_key or f"{snake_case(owner_name)}_id"
        local_key = self._resolve_local_key(instance)
        val = getattr(instance, local_key, None)
        return target_model.where(fk, val).limit(1)


class BelongsTo[T: Model](SingleRelation[T]):
    """Inverse one-to-one or one-to-many relationship."""

    @overload
    def __init__(
        self,
        related_model: type[T],
        foreign_key: str | None = None,
        local_key: str | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self: BelongsTo[Any],
        related_model: str,
        foreign_key: str | None = None,
        local_key: str | None = None,
    ) -> None: ...

    def __init__(
        self,
        related_model: str | type[T],
        foreign_key: str | None = None,
        local_key: str | None = None,
    ) -> None:
        super().__init__(related_model, foreign_key=foreign_key, local_key=local_key)

    def _resolve_owner_key(self, target_model: type[Model]) -> str:
        """Resolve referenced primary/owner key on the target parent model."""
        if self.local_key is not None:
            return self.local_key
        return getattr(target_model, "__primary_key__", "id")

    async def eager_load(self, instances: Sequence[Model]) -> None:
        if not instances:
            return

        target_model = self.get_target_model()
        fk = self.foreign_key or f"{snake_case(self.name)}_id"
        owner_key = self._resolve_owner_key(target_model)
        foreign_keys = [
            getattr(inst, fk)
            for inst in instances
            if getattr(inst, fk, None) is not None
        ]
        unique_keys = list(set(foreign_keys))

        if not unique_keys:
            for inst in instances:
                inst._loaded_relations[self.name] = None
            return

        related_instances: list[Model] = []
        for chunk in _chunk_list(unique_keys):
            rows = await target_model.where(owner_key, "IN", chunk).get()
            related_instances.extend(rows)

        mapped = {getattr(rel, owner_key, None): rel for rel in related_instances}

        for inst in instances:
            key = getattr(inst, fk, None)
            inst._loaded_relations[self.name] = mapped.get(key, None)

    def build_lazy_query(self, instance: Model) -> ModelQueryBuilder[T]:
        target_model = self._get_target_model_typed()
        fk = self.foreign_key or f"{snake_case(self.name)}_id"
        owner_key = self._resolve_owner_key(target_model)
        val = getattr(instance, fk, None)
        return target_model.where(owner_key, val).limit(1)


class BelongsToMany[T: Model](ManyRelation[T]):
    """Many-to-many relationship with pivot table."""

    @overload
    def __init__(
        self,
        related_model: type[T],
        pivot_table: str | None = None,
        foreign_pivot_key: str | None = None,
        related_pivot_key: str | None = None,
        parent_key: str | None = None,
        related_key: str | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self: BelongsToMany[Any],
        related_model: str,
        pivot_table: str | None = None,
        foreign_pivot_key: str | None = None,
        related_pivot_key: str | None = None,
        parent_key: str | None = None,
        related_key: str | None = None,
    ) -> None: ...

    def __init__(
        self,
        related_model: str | type[T],
        pivot_table: str | None = None,
        foreign_pivot_key: str | None = None,
        related_pivot_key: str | None = None,
        parent_key: str | None = None,
        related_key: str | None = None,
    ) -> None:
        super().__init__(related_model, local_key=parent_key)
        self.pivot_table = pivot_table
        self.foreign_pivot_key = foreign_pivot_key
        self.related_pivot_key = related_pivot_key
        self.parent_key = parent_key
        self.related_key = related_key

    def _resolve_parent_key(self, parent_cls: type[Model]) -> str:
        """Resolve parent key column name using model __primary_key__ unless overridden."""
        if self.parent_key is not None:
            return self.parent_key
        return getattr(parent_cls, "__primary_key__", "id")

    def _resolve_related_key(self, target_cls: type[Model]) -> str:
        """Resolve related key column name using target __primary_key__ unless overridden."""
        if self.related_key is not None:
            return self.related_key
        return getattr(target_cls, "__primary_key__", "id")

    def _resolve_pivot_table(self, parent_cls: type[Model]) -> str:
        if self.pivot_table:
            return self.pivot_table
        names = sorted(
            [snake_case(parent_cls.__name__), snake_case(self.related_model_name)]
        )
        return f"{names[0]}_{names[1]}"

    async def eager_load(self, instances: Sequence[Model]) -> None:
        if not instances:
            return

        target = self.get_target_model()
        parent_cls = (
            self.owner_cls if self.owner_cls is not None else instances[0].__class__
        )
        parent_key = self._resolve_parent_key(parent_cls)
        related_key = self._resolve_related_key(target)
        pivot_table = self._resolve_pivot_table(parent_cls)
        fpk = self.foreign_pivot_key or f"{snake_case(parent_cls.__name__)}_id"
        rpk = self.related_pivot_key or f"{snake_case(target.__name__)}_id"

        parent_keys = [
            getattr(i, parent_key, None)
            for i in instances
            if getattr(i, parent_key, None) is not None
        ]
        unique_keys = list(set(parent_keys))

        if not unique_keys:
            for inst in instances:
                inst._loaded_relations[self.name] = LoadedList()
            return

        rows: list[dict[str, Any]] = []
        for chunk in _chunk_list(unique_keys):
            b = target.query().builder
            b.select(
                f"{target.__table__}.*",
                Column(f"{pivot_table}.{fpk}", alias="_pivot_parent_key"),
            )
            b.join(
                pivot_table,
                f"{target.__table__}.{related_key}",
                "=",
                f"{pivot_table}.{rpk}",
            )
            b.where(f"{pivot_table}.{fpk}", "IN", chunk)
            chunk_rows = await b.get()
            rows.extend(chunk_rows)

        grouped: dict[Any, list[Model]] = {}

        for row in rows:
            pivot_parent_key = row.pop("_pivot_parent_key")
            item = target._hydrate(row)
            grouped.setdefault(pivot_parent_key, []).append(item)

        for inst in instances:
            key = getattr(inst, parent_key, None)
            inst._loaded_relations[self.name] = LoadedList(grouped.get(key, []))

    def build_lazy_query(self, instance: Model) -> ModelQueryBuilder[T]:
        target = self._get_target_model_typed()
        parent_cls = (
            self.owner_cls if self.owner_cls is not None else instance.__class__
        )
        parent_key = self._resolve_parent_key(parent_cls)
        related_key = self._resolve_related_key(target)
        pivot_table = self._resolve_pivot_table(parent_cls)
        fpk = self.foreign_pivot_key or f"{snake_case(parent_cls.__name__)}_id"
        rpk = self.related_pivot_key or f"{snake_case(target.__name__)}_id"

        builder = target.query()
        # Explicitly scope select to target table so pivot table columns do not overwrite target attributes
        builder.select(f"{target.__table__}.*")
        builder.builder.join(
            pivot_table,
            f"{target.__table__}.{related_key}",
            "=",
            f"{pivot_table}.{rpk}",
        )
        builder.builder.where(
            f"{pivot_table}.{fpk}", getattr(instance, parent_key, None)
        )
        return builder


class _BaseRelationshipQueryBuilder[T: Model]:
    """Shared typed query builder and guard logic for Active Record relationships."""

    def __init__(
        self,
        instance: Model,
        relation: Relation[T],
        builder: ModelQueryBuilder[T],
    ) -> None:
        self._instance = instance
        self._relation = relation
        self._builder = builder

    def _not_loaded_error(self, attr: str | None = None) -> RelationNotLoadedError:
        rel_name = self._relation.name
        model_name = self._instance.__class__.__name__
        if attr:
            msg = (
                f"Cannot access attribute '{attr}' because relation '{rel_name}' "
                f"on {model_name} was not eager-loaded. "
                f"Use .with_('{rel_name}') or await the relation query first."
            )
        else:
            msg = (
                f"Relation '{rel_name}' on {model_name} was not eager-loaded. "
                f"Use .with_('{rel_name}') or explicitly query the relationship "
                f"(e.g., 'await {model_name.lower()}.{rel_name}.get()')."
            )
        return RelationNotLoadedError(msg)

    def clone(self) -> Self:
        """Create an independent copy of this scoped relationship query builder."""
        return self.__class__(self._instance, self._relation, self._builder.clone())

    def to_sql(self, start_idx: int = 1) -> tuple[str, list[Any]]:
        return self._builder.to_sql(start_idx)

    def to_raw_sql(self) -> str:
        return self._builder.to_raw_sql()

    def dump(self) -> Self:
        self._builder.dump()
        return self

    def dump_raw_sql(self) -> Self:
        self._builder.dump_raw_sql()
        return self

    def dd(self) -> NoReturn:
        self._builder.dd()

    def dd_raw_sql(self) -> NoReturn:
        self._builder.dd_raw_sql()

    def allow_all(self) -> Self:
        """Explicitly permit mass UPDATE or DELETE queries without extra filters."""
        self._builder.allow_all()
        return self

    def where(self, column: str, operator_or_value: Any, value: Any = _MISSING) -> Self:
        if value is _MISSING:
            self._builder.where(column, operator_or_value)
        else:
            self._builder.where(column, operator_or_value, value)
        return self

    def or_where(
        self, column: str, operator_or_value: Any, value: Any = _MISSING
    ) -> Self:
        if value is _MISSING:
            self._builder.or_where(column, operator_or_value)
        else:
            self._builder.or_where(column, operator_or_value, value)
        return self

    def where_column(
        self, first: str, operator: str, second: str, *, boolean: str = "AND"
    ) -> Self:
        self._builder.where_column(first, operator, second, boolean=boolean)
        return self

    def or_where_column(self, first: str, operator: str, second: str) -> Self:
        self._builder.or_where_column(first, operator, second)
        return self

    def where_exists(
        self,
        query: ModelQueryBuilder[Any] | QueryBuilder,
        *,
        boolean: str = "AND",
    ) -> Self:
        self._builder.where_exists(query, boolean=boolean)
        return self

    def where_not_exists(
        self,
        query: ModelQueryBuilder[Any] | QueryBuilder,
        *,
        boolean: str = "AND",
    ) -> Self:
        self._builder.where_not_exists(query, boolean=boolean)
        return self

    def or_where_exists(self, query: ModelQueryBuilder[Any] | QueryBuilder) -> Self:
        self._builder.or_where_exists(query)
        return self

    def or_where_not_exists(self, query: ModelQueryBuilder[Any] | QueryBuilder) -> Self:
        self._builder.or_where_not_exists(query)
        return self

    def where_any(
        self,
        columns: Sequence[str],
        operator_or_value: Any,
        value: Any = _MISSING,
        *,
        boolean: str = "AND",
    ) -> Self:
        if value is _MISSING:
            self._builder.where_any(columns, operator_or_value, boolean=boolean)
        else:
            self._builder.where_any(columns, operator_or_value, value, boolean=boolean)
        return self

    def where_all(
        self,
        columns: Sequence[str],
        operator_or_value: Any,
        value: Any = _MISSING,
        *,
        boolean: str = "AND",
    ) -> Self:
        if value is _MISSING:
            self._builder.where_all(columns, operator_or_value, boolean=boolean)
        else:
            self._builder.where_all(columns, operator_or_value, value, boolean=boolean)
        return self

    def where_none(
        self,
        columns: Sequence[str],
        operator_or_value: Any,
        value: Any = _MISSING,
        *,
        boolean: str = "AND",
    ) -> Self:
        if value is _MISSING:
            self._builder.where_none(columns, operator_or_value, boolean=boolean)
        else:
            self._builder.where_none(columns, operator_or_value, value, boolean=boolean)
        return self

    def or_where_any(
        self, columns: Sequence[str], operator_or_value: Any, value: Any = _MISSING
    ) -> Self:
        if value is _MISSING:
            self._builder.or_where_any(columns, operator_or_value)
        else:
            self._builder.or_where_any(columns, operator_or_value, value)
        return self

    def or_where_all(
        self, columns: Sequence[str], operator_or_value: Any, value: Any = _MISSING
    ) -> Self:
        if value is _MISSING:
            self._builder.or_where_all(columns, operator_or_value)
        else:
            self._builder.or_where_all(columns, operator_or_value, value)
        return self

    def or_where_none(
        self, columns: Sequence[str], operator_or_value: Any, value: Any = _MISSING
    ) -> Self:
        if value is _MISSING:
            self._builder.or_where_none(columns, operator_or_value)
        else:
            self._builder.or_where_none(columns, operator_or_value, value)
        return self

    def where_past(self, column: str, *, boolean: str = "AND") -> Self:
        self._builder.where_past(column, boolean=boolean)
        return self

    def where_future(self, column: str, *, boolean: str = "AND") -> Self:
        self._builder.where_future(column, boolean=boolean)
        return self

    def or_where_past(self, column: str) -> Self:
        self._builder.or_where_past(column)
        return self

    def or_where_future(self, column: str) -> Self:
        self._builder.or_where_future(column)
        return self

    def where_json_contains(
        self, column: str, value: Any, *, boolean: str = "AND"
    ) -> Self:
        self._builder.where_json_contains(column, value, boolean=boolean)
        return self

    def where_json_doesnt_contain(
        self, column: str, value: Any, *, boolean: str = "AND"
    ) -> Self:
        self._builder.where_json_doesnt_contain(column, value, boolean=boolean)
        return self

    def where_json_contains_key(self, column: str, *, boolean: str = "AND") -> Self:
        self._builder.where_json_contains_key(column, boolean=boolean)
        return self

    def where_json_doesnt_contain_key(
        self, column: str, *, boolean: str = "AND"
    ) -> Self:
        self._builder.where_json_doesnt_contain_key(column, boolean=boolean)
        return self

    def where_json_length(
        self,
        column: str,
        operator_or_value: Any,
        value: Any = _MISSING,
        *,
        boolean: str = "AND",
    ) -> Self:
        if value is _MISSING:
            self._builder.where_json_length(column, operator_or_value, boolean=boolean)
        else:
            self._builder.where_json_length(
                column, operator_or_value, value, boolean=boolean
            )
        return self

    def or_where_json_contains(self, column: str, value: Any) -> Self:
        self._builder.or_where_json_contains(column, value)
        return self

    def or_where_json_doesnt_contain(self, column: str, value: Any) -> Self:
        self._builder.or_where_json_doesnt_contain(column, value)
        return self

    def or_where_json_contains_key(self, column: str) -> Self:
        self._builder.or_where_json_contains_key(column)
        return self

    def or_where_json_doesnt_contain_key(self, column: str) -> Self:
        self._builder.or_where_json_doesnt_contain_key(column)
        return self

    def or_where_json_length(
        self, column: str, operator_or_value: Any, value: Any = _MISSING
    ) -> Self:
        if value is _MISSING:
            self._builder.or_where_json_length(column, operator_or_value)
        else:
            self._builder.or_where_json_length(column, operator_or_value, value)
        return self

    def where_group(
        self,
        callback: Callable[[ModelQueryBuilder[T]], Any],
        *,
        boolean: str = "AND",
    ) -> Self:
        self._builder.where_group(callback, boolean=boolean)
        return self

    def or_where_group(self, callback: Callable[[ModelQueryBuilder[T]], Any]) -> Self:
        self._builder.or_where_group(callback)
        return self

    def where_in(
        self,
        column: str,
        values: Sequence[Any] | QueryBuilder | ModelQueryBuilder[Any],
    ) -> Self:
        self._builder.where_in(column, values)
        return self

    def or_where_in(
        self,
        column: str,
        values: Sequence[Any] | QueryBuilder | ModelQueryBuilder[Any],
    ) -> Self:
        self._builder.or_where_in(column, values)
        return self

    def where_not_in(
        self,
        column: str,
        values: Sequence[Any] | QueryBuilder | ModelQueryBuilder[Any],
    ) -> Self:
        self._builder.where_not_in(column, values)
        return self

    def or_where_not_in(
        self,
        column: str,
        values: Sequence[Any] | QueryBuilder | ModelQueryBuilder[Any],
    ) -> Self:
        self._builder.or_where_not_in(column, values)
        return self

    def where_like(
        self,
        column: str,
        value: str,
        *,
        case_insensitive: bool = False,
        match: str = "contains",
        boolean: str = "AND",
    ) -> Self:
        self._builder.where_like(
            column,
            value,
            case_insensitive=case_insensitive,
            match=match,
            boolean=boolean,
        )
        return self

    def or_where_like(
        self,
        column: str,
        value: str,
        *,
        case_insensitive: bool = False,
        match: str = "contains",
    ) -> Self:
        self._builder.or_where_like(
            column, value, case_insensitive=case_insensitive, match=match
        )
        return self

    def where_ilike(
        self,
        column: str,
        value: str,
        *,
        match: str = "contains",
        boolean: str = "AND",
    ) -> Self:
        self._builder.where_ilike(column, value, match=match, boolean=boolean)
        return self

    def or_where_ilike(
        self,
        column: str,
        value: str,
        *,
        match: str = "contains",
    ) -> Self:
        self._builder.or_where_ilike(column, value, match=match)
        return self

    def where_between(
        self, column: str, values: Sequence[Any], *, boolean: str = "AND"
    ) -> Self:
        self._builder.where_between(column, values, boolean=boolean)
        return self

    def where_not_between(
        self, column: str, values: Sequence[Any], *, boolean: str = "AND"
    ) -> Self:
        self._builder.where_not_between(column, values, boolean=boolean)
        return self

    def or_where_between(self, column: str, values: Sequence[Any]) -> Self:
        self._builder.or_where_between(column, values)
        return self

    def or_where_not_between(self, column: str, values: Sequence[Any]) -> Self:
        self._builder.or_where_not_between(column, values)
        return self

    def where_raw(self, sql: str, bindings: Sequence[Any] | None = None) -> Self:
        self._builder.where_raw(sql, bindings)
        return self

    def or_where_raw(self, sql: str, bindings: Sequence[Any] | None = None) -> Self:
        self._builder.or_where_raw(sql, bindings)
        return self

    def where_null(self, column: str) -> Self:
        self._builder.where_null(column)
        return self

    def or_where_null(self, column: str) -> Self:
        self._builder.or_where_null(column)
        return self

    def where_not_null(self, column: str) -> Self:
        self._builder.where_not_null(column)
        return self

    def or_where_not_null(self, column: str) -> Self:
        self._builder.or_where_not_null(column)
        return self

    def join(
        self,
        table: str,
        first: str,
        operator: str,
        second: str,
        type_: str = "INNER",
    ) -> Self:
        self._builder.join(table, first, operator, second, type_=type_)
        return self

    def left_join(self, table: str, first: str, operator: str, second: str) -> Self:
        self._builder.left_join(table, first, operator, second)
        return self

    def right_join(self, table: str, first: str, operator: str, second: str) -> Self:
        self._builder.right_join(table, first, operator, second)
        return self

    def with_(self, *relations: str) -> Self:
        self._builder.with_(*relations)
        return self

    def order_by(self, column: str, direction: str = "ASC") -> Self:
        self._builder.order_by(column, direction)
        return self

    def latest(self, column: str = "created_at") -> Self:
        self._builder.latest(column)
        return self

    def oldest(self, column: str = "created_at") -> Self:
        self._builder.oldest(column)
        return self

    def in_random_order(self) -> Self:
        self._builder.in_random_order()
        return self

    def limit(self, value: int) -> Self:
        self._builder.limit(value)
        return self

    def offset(self, value: int) -> Self:
        self._builder.offset(value)
        return self

    def take(self, value: int) -> Self:
        self._builder.take(value)
        return self

    def skip(self, value: int) -> Self:
        self._builder.skip(value)
        return self

    def group_by(self, *columns: str | Sequence[str]) -> Self:
        self._builder.group_by(*columns)
        return self

    def having(
        self,
        column: str,
        operator_or_value: Any,
        value: Any = _MISSING,
        boolean: str = "AND",
    ) -> Self:
        self._builder.having(column, operator_or_value, value, boolean=boolean)
        return self

    def or_having(
        self, column: str, operator_or_value: Any, value: Any = _MISSING
    ) -> Self:
        self._builder.or_having(column, operator_or_value, value)
        return self

    def having_raw(
        self,
        sql: str,
        bindings: Sequence[Any] | None = None,
        boolean: str = "AND",
    ) -> Self:
        self._builder.having_raw(sql, bindings, boolean=boolean)
        return self

    def or_having_raw(self, sql: str, bindings: Sequence[Any] | None = None) -> Self:
        self._builder.or_having_raw(sql, bindings)
        return self

    def select(self, *columns: str) -> Self:
        self._builder.select(*columns)
        return self

    def select_raw(self, sql: str, bindings: Sequence[Any] | None = None) -> Self:
        self._builder.select_raw(sql, bindings)
        return self

    def select_count(self, column: str = "*", alias: str | None = None) -> Self:
        self._builder.select_count(column, alias)
        return self

    def select_sum(self, column: str, alias: str | None = None) -> Self:
        self._builder.select_sum(column, alias)
        return self

    def select_avg(self, column: str, alias: str | None = None) -> Self:
        self._builder.select_avg(column, alias)
        return self

    def select_min(self, column: str, alias: str | None = None) -> Self:
        self._builder.select_min(column, alias)
        return self

    def select_max(self, column: str, alias: str | None = None) -> Self:
        self._builder.select_max(column, alias)
        return self

    def distinct(self, *columns: str) -> Self:
        self._builder.distinct(*columns)
        return self

    def shared_lock(self) -> Self:
        self._builder.shared_lock()
        return self

    def lock_for_update(self) -> Self:
        self._builder.lock_for_update()
        return self

    def when(
        self,
        condition: Any,
        callback: Callable[[Self, Any], Any],
        default: Callable[[Self, Any], Any] | None = None,
    ) -> Self:
        """Apply callback if condition is truthy, otherwise apply default callback if provided."""
        if condition:
            res = callback(self, condition)
            return res if isinstance(res, self.__class__) else self
        if default is not None:
            res = default(self, condition)
            return res if isinstance(res, self.__class__) else self
        return self

    def unless(
        self,
        condition: Any,
        callback: Callable[[Self, Any], Any],
        default: Callable[[Self, Any], Any] | None = None,
    ) -> Self:
        """Apply callback if condition is falsy, otherwise apply default callback if provided."""
        return self.when(not condition, callback, default)

    def has(
        self,
        relation: str,
        operator: str = ">=",
        count: int = 1,
        *,
        boolean: str = "AND",
        callback: Callable[[ModelQueryBuilder[Any]], Any] | None = None,
    ) -> Self:
        self._builder.has(relation, operator, count, boolean=boolean, callback=callback)
        return self

    def or_has(
        self,
        relation: str,
        operator: str = ">=",
        count: int = 1,
        callback: Callable[[ModelQueryBuilder[Any]], Any] | None = None,
    ) -> Self:
        self._builder.or_has(relation, operator, count, callback=callback)
        return self

    def doesnt_have(
        self,
        relation: str,
        *,
        boolean: str = "AND",
        callback: Callable[[ModelQueryBuilder[Any]], Any] | None = None,
    ) -> Self:
        self._builder.doesnt_have(relation, boolean=boolean, callback=callback)
        return self

    def or_doesnt_have(
        self,
        relation: str,
        callback: Callable[[ModelQueryBuilder[Any]], Any] | None = None,
    ) -> Self:
        self._builder.or_doesnt_have(relation, callback=callback)
        return self

    def where_has(
        self,
        relation: str,
        callback: Callable[[ModelQueryBuilder[Any]], Any] | None = None,
        operator: str = ">=",
        count: int = 1,
        *,
        boolean: str = "AND",
    ) -> Self:
        self._builder.where_has(
            relation, callback, operator, count, boolean=boolean
        )
        return self

    def or_where_has(
        self,
        relation: str,
        callback: Callable[[ModelQueryBuilder[Any]], Any] | None = None,
        operator: str = ">=",
        count: int = 1,
    ) -> Self:
        self._builder.or_where_has(relation, callback, operator, count)
        return self

    def where_doesnt_have(
        self,
        relation: str,
        callback: Callable[[ModelQueryBuilder[Any]], Any] | None = None,
        *,
        boolean: str = "AND",
    ) -> Self:
        self._builder.where_doesnt_have(relation, callback, boolean=boolean)
        return self

    def or_where_doesnt_have(
        self,
        relation: str,
        callback: Callable[[ModelQueryBuilder[Any]], Any] | None = None,
    ) -> Self:
        self._builder.or_where_doesnt_have(relation, callback)
        return self

    async def get(self) -> list[T]:
        """Execute the relationship query and return hydrated models."""
        return await self._builder.get()

    async def first(self) -> T | None:
        """Fetch the first record matching the relationship scope."""
        return await self._builder.first()

    async def first_or_fail(self) -> T:
        """Fetch the first related record or raise ModelNotFoundError."""
        return await self._builder.first_or_fail()

    async def count(self, column: str = "*") -> int:
        """Return the count of related records."""
        return await self._builder.count(column)

    async def exists(self) -> bool:
        """Return True if at least one related record exists."""
        return await self._builder.exists()

    async def sum(self, column: str) -> Any:
        return await self._builder.sum(column)

    async def min(self, column: str) -> Any:
        return await self._builder.min(column)

    async def max(self, column: str) -> Any:
        return await self._builder.max(column)

    async def avg(self, column: str) -> Any:
        return await self._builder.avg(column)

    async def value(self, column: str) -> Any | None:
        return await self._builder.value(column)

    @overload
    async def pluck(self, column: str, key: None = None) -> list[Any]: ...

    @overload
    async def pluck(self, column: str, key: str) -> dict[Any, Any]: ...

    async def pluck(
        self, column: str, key: str | None = None
    ) -> list[Any] | dict[Any, Any]:
        if key is None:
            return await self._builder.pluck(column)
        return await self._builder.pluck(column, key)

    async def increment(
        self, column: str, amount: float = 1, extra: Mapping[str, Any] | None = None
    ) -> str:
        return await self._builder.increment(column, amount, extra=extra)

    async def decrement(
        self, column: str, amount: float = 1, extra: Mapping[str, Any] | None = None
    ) -> str:
        return await self._builder.decrement(column, amount, extra=extra)

    async def chunk(
        self,
        count: int,
        callback: Callable[[list[T]], Awaitable[bool | None] | bool | None],
    ) -> bool:
        return await self._builder.chunk(count, callback)

    async def chunk_by_id(
        self,
        count: int,
        callback: Callable[[list[T]], Awaitable[bool | None] | bool | None],
        column: str | None = None,
    ) -> bool:
        return await self._builder.chunk_by_id(count, callback, column=column)

    async def upsert(
        self,
        values: Sequence[Mapping[str, Any]],
        unique_by: Sequence[str] | str,
        update_columns: Sequence[str] | None = None,
    ) -> int:
        return await self._builder.upsert(values, unique_by, update_columns=update_columns)

    async def paginate(
        self, per_page: int = 15, page: int = 1
    ) -> Paginator[T]:
        return await self._builder.paginate(per_page=per_page, page=page)

    async def simple_paginate(
        self, per_page: int = 15, page: int = 1
    ) -> SimplePaginator[T]:
        return await self._builder.simple_paginate(per_page=per_page, page=page)

    async def update(self, data: Mapping[str, Any] | BaseSchema) -> str:
        return await self._builder.update(data)

    async def delete(self) -> str:
        return await self._builder.delete()

    async def attach(self, model_or_id: Any, **pivot_extra: Any) -> None:
        """Attach a related model to this parent model in the pivot table."""
        if not isinstance(self._relation, BelongsToMany):
            raise TypeError("attach() is only supported on BelongsToMany relationships.")
        target_model = self._relation.get_target_model()
        target_id = (
            model_or_id.get_primary_key()
            if hasattr(model_or_id, "get_primary_key")
            else model_or_id
        )
        parent_cls = self._relation.owner_cls or self._instance.__class__
        parent_key = self._relation._resolve_parent_key(parent_cls)
        parent_id = getattr(self._instance, parent_key, None)
        if parent_id is None:
            raise QueryError("Cannot attach relation on an unsaved model without a primary key.")
        if target_id is None:
            raise QueryError("Cannot attach a related model without a primary key.")
        pivot_table = self._relation._resolve_pivot_table(parent_cls)
        fpk = self._relation.foreign_pivot_key or f"{snake_case(parent_cls.__name__)}_id"
        rpk = self._relation.related_pivot_key or f"{snake_case(target_model.__name__)}_id"

        data = {fpk: parent_id, rpk: target_id, **pivot_extra}
        await Query.table(pivot_table).insert(data)

    async def detach(self, model_or_id: Any = None) -> int:
        """Detach related model(s) from this parent model in the pivot table."""
        if not isinstance(self._relation, BelongsToMany):
            raise TypeError("detach() is only supported on BelongsToMany relationships.")
        target_model = self._relation.get_target_model()
        parent_cls = self._relation.owner_cls or self._instance.__class__
        parent_key = self._relation._resolve_parent_key(parent_cls)
        parent_id = getattr(self._instance, parent_key, None)
        if parent_id is None:
            raise QueryError("Cannot detach relation on an unsaved model without a primary key.")
        pivot_table = self._relation._resolve_pivot_table(parent_cls)
        fpk = self._relation.foreign_pivot_key or f"{snake_case(parent_cls.__name__)}_id"
        rpk = self._relation.related_pivot_key or f"{snake_case(target_model.__name__)}_id"

        q = Query.table(pivot_table).where(fpk, parent_id)
        if model_or_id is not None:
            target_id = (
                model_or_id.get_primary_key()
                if hasattr(model_or_id, "get_primary_key")
                else model_or_id
            )
            if target_id is None:
                raise QueryError("Cannot detach a related model without a primary key.")
            q = q.where(rpk, target_id)
        res = await q.delete()
        if isinstance(res, str) and res.startswith("DELETE"):
            try:
                return int(res.split()[-1])
            except ValueError:
                return 1
        return 1 if res else 0

    async def sync(self, ids_or_models: Sequence[Any]) -> None:
        """Sync pivot table connections to match the provided list of models or ids."""
        async with db.transaction():
            await self.detach()
            for item in ids_or_models:
                await self.attach(item)

    async def create(
        self, *args: Mapping[str, Any] | BaseSchema, **kwargs: Any
    ) -> T:
        """Create a new model instance pre-bound to this relationship."""
        data = self._builder.model_cls._extract_create_mapping(args, kwargs)

        if isinstance(self._relation, (HasMany, HasOne)):
            owner_name = (
                self._relation.owner_cls.__name__
                if self._relation.owner_cls is not None
                else self._instance.__class__.__name__
            )
            fk = self._relation.foreign_key or f"{snake_case(owner_name)}_id"
            local_key = self._relation._resolve_local_key(self._instance)
            data[fk] = getattr(self._instance, local_key, None)
            return await self._builder.model_cls.create(data)

        if isinstance(self._relation, BelongsToMany):
            raise NotImplementedError(
                "create() would leave the new row unlinked; use attach()."
            )

        return await self._builder.model_cls.create(data)

    def __iter__(self) -> Iterator[T]:
        raise self._not_loaded_error()

    def __len__(self) -> int:
        raise self._not_loaded_error()

    def __bool__(self) -> bool:
        raise self._not_loaded_error()

    @overload
    def __getitem__(self, key: int) -> T: ...

    @overload
    def __getitem__(self, key: slice) -> list[T]: ...

    def __getitem__(self, key: int | slice) -> T | list[T]:
        raise self._not_loaded_error()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(
                f"'{self.__class__.__name__}' object has no attribute '{name}'"
            )
        if hasattr(self._builder, name):
            attr = getattr(self._builder, name)
            if callable(attr):
                def _delegated(*args: Any, **kwargs: Any) -> Any:
                    res = attr(*args, **kwargs)
                    return self if res is self._builder else res

                return _delegated
            return attr
        raise self._not_loaded_error(name)


class RelationshipQueryBuilder[T: Model](_BaseRelationshipQueryBuilder[T]):
    """Scoped query builder for an Active Record relationship.

    Lazily awaitable when explicitly queried (e.g. `await user.posts` or
    `await user.posts.where("published", True).get()`).

    Synchronous data access (such as iterating, `len()`, or accessing model attributes)
    raises `RelationNotLoadedError` to prevent silent N+1 query antipatterns.
    """

    def __await__(self) -> Generator[Any, None, list[T]]:
        """Allow directly awaiting the relation query builder."""
        return self.get().__await__()


class SingleRelationshipQueryBuilder[T: Model](_BaseRelationshipQueryBuilder[T]):
    """Scoped query builder for singular Active Record relationships (HasOne / BelongsTo)."""

    def __await__(self) -> Generator[Any, None, T | None]:
        """Allow directly awaiting the single-record relationship query builder."""
        return self.first().__await__()


class RelationDescriptor[TRel: Relation[Any]]:
    """Descriptor that intercepts relation property access on Model instances."""

    def __init__(self, name: str, relation: TRel) -> None:
        self.name = name
        self._relation: TRel = relation

    @property
    def relation(self) -> TRel:
        """Return the underlying Relation configuration object."""
        return self._relation

    @overload
    def __get__(self, instance: None, owner: type[Model] | None = None) -> TRel: ...

    @overload
    def __get__[T: Model](
        self: RelationDescriptor[SingleRelation[T]],
        instance: Model,
        owner: type[Model] | None = None,
    ) -> SingleRelationshipQueryBuilder[T]: ...

    @overload
    def __get__[T: Model](
        self: RelationDescriptor[ManyRelation[T]],
        instance: Model,
        owner: type[Model] | None = None,
    ) -> RelationshipQueryBuilder[T]: ...

    @overload
    def __get__(
        self, instance: Model, owner: type[Model] | None = None
    ) -> RelationshipQueryBuilder[Any]: ...

    def __get__(
        self, instance: Model | None, owner: type[Model] | None = None
    ) -> TRel | _BaseRelationshipQueryBuilder[Any] | Sequence[Model] | Model | None:
        if instance is None:
            return self._relation

        if hasattr(instance, "_loaded_relations") and self.name in instance._loaded_relations:
            return instance._loaded_relations[self.name]

        Relation._ensure_lazy_allowed(instance, owner, self.name)
        return self._relation._build_query_builder(instance)

    def __set__(self, instance: Model, value: Any) -> None:
        instance._loaded_relations[self.name] = value


__all__ = [
    "BelongsTo",
    "BelongsToMany",
    "HasMany",
    "HasOne",
    "LazyLoadingDisabledError",
    "LoadedList",
    "ManyRelation",
    "Relation",
    "RelationDescriptor",
    "RelationNotLoadedError",
    "RelationshipQueryBuilder",
    "SingleRelation",
    "SingleRelationshipQueryBuilder",
]
