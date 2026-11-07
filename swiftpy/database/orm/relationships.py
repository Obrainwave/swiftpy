from __future__ import annotations

from collections.abc import Generator, Sequence
from typing import TYPE_CHECKING, Any, Self

from swiftpy.database.orm.registry import _MODEL_REGISTRY
from swiftpy.database.query_builder import Column
from swiftpy.support.strings import snake_case

if TYPE_CHECKING:
    from swiftpy.database.orm.model import Model, ModelQueryBuilder


_MISSING = object()


class RelationNotLoadedError(RuntimeError):
    """Raised when attempting to synchronously access an un-eager-loaded relationship."""


class LoadedList(list[Any]):
    """A list of eager-loaded models that can also be directly awaited."""

    def __await__(self) -> Generator[Any, None, Self]:
        async def _resolved() -> Self:
            return self

        return _resolved().__await__()


class Relation:
    """Base relation definition for Active Record Models."""

    def __init__(
        self,
        related_model: str | type[Model],
        foreign_key: str | None = None,
        local_key: str = "id",
    ) -> None:
        self._related_cls: type[Model] | None = None
        if isinstance(related_model, str):
            self.related_model_name = related_model
        else:
            self.related_model_name = related_model.__name__
            self._related_cls = related_model

        self.foreign_key = foreign_key
        self.local_key = local_key
        self.name: str = ""
        self.owner_cls: type[Model] | None = None

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

    def build_lazy_query(self, instance: Model) -> ModelQueryBuilder[Any]:
        """Construct a query builder scoped to this relationship."""
        raise NotImplementedError


class HasMany(Relation):
    """One-to-many relationship."""

    async def eager_load(self, instances: Sequence[Model]) -> None:
        if not instances:
            return

        target_model = self.get_target_model()
        owner_name = (
            self.owner_cls.__name__
            if self.owner_cls is not None
            else instances[0].__class__.__name__
        )
        fk = self.foreign_key or f"{snake_case(owner_name)}_id"

        local_keys = [
            getattr(inst, self.local_key)
            for inst in instances
            if getattr(inst, self.local_key, None) is not None
        ]
        unique_keys = list(set(local_keys))

        if not unique_keys:
            for inst in instances:
                inst._loaded_relations[self.name] = LoadedList()
            return

        related_instances = await target_model.where(fk, "IN", unique_keys).get()

        grouped: dict[Any, list[Model]] = {}
        for rel in related_instances:
            key = getattr(rel, fk, None)
            grouped.setdefault(key, []).append(rel)

        for inst in instances:
            key = getattr(inst, self.local_key, None)
            inst._loaded_relations[self.name] = LoadedList(grouped.get(key, []))

    def build_lazy_query(self, instance: Model) -> ModelQueryBuilder[Any]:
        target_model = self.get_target_model()
        owner_name = (
            self.owner_cls.__name__
            if self.owner_cls is not None
            else instance.__class__.__name__
        )
        fk = self.foreign_key or f"{snake_case(owner_name)}_id"
        val = getattr(instance, self.local_key, None)
        return target_model.where(fk, val)


class HasOne(Relation):
    """One-to-one relationship."""

    async def eager_load(self, instances: Sequence[Model]) -> None:
        if not instances:
            return

        target_model = self.get_target_model()
        owner_name = (
            self.owner_cls.__name__
            if self.owner_cls is not None
            else instances[0].__class__.__name__
        )
        fk = self.foreign_key or f"{snake_case(owner_name)}_id"

        local_keys = [
            getattr(inst, self.local_key)
            for inst in instances
            if getattr(inst, self.local_key, None) is not None
        ]
        unique_keys = list(set(local_keys))

        if not unique_keys:
            for inst in instances:
                inst._loaded_relations[self.name] = None
            return

        related = await target_model.where(fk, "IN", unique_keys).get()
        mapped: dict[Any, Model] = {}
        for r in related:
            key = getattr(r, fk, None)
            if key not in mapped:
                mapped[key] = r

        for inst in instances:
            key = getattr(inst, self.local_key, None)
            inst._loaded_relations[self.name] = mapped.get(key, None)

    def build_lazy_query(self, instance: Model) -> ModelQueryBuilder[Any]:
        target_model = self.get_target_model()
        owner_name = (
            self.owner_cls.__name__
            if self.owner_cls is not None
            else instance.__class__.__name__
        )
        fk = self.foreign_key or f"{snake_case(owner_name)}_id"
        val = getattr(instance, self.local_key, None)
        return target_model.where(fk, val).limit(1)


class BelongsTo(Relation):
    """Inverse one-to-one or one-to-many relationship."""

    async def eager_load(self, instances: Sequence[Model]) -> None:
        if not instances:
            return

        target_model = self.get_target_model()
        fk = self.foreign_key or f"{snake_case(self.name)}_id"
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

        related_instances = await target_model.where(
            self.local_key, "IN", unique_keys
        ).get()
        mapped = {getattr(rel, self.local_key, None): rel for rel in related_instances}

        for inst in instances:
            key = getattr(inst, fk, None)
            inst._loaded_relations[self.name] = mapped.get(key, None)

    def build_lazy_query(self, instance: Model) -> ModelQueryBuilder[Any]:
        target_model = self.get_target_model()
        fk = self.foreign_key or f"{snake_case(self.name)}_id"
        val = getattr(instance, fk, None)
        return target_model.where(self.local_key, val).limit(1)


class BelongsToMany(Relation):
    """Many-to-many relationship with pivot table."""

    def __init__(
        self,
        related_model: str | type[Model],
        pivot_table: str | None = None,
        foreign_pivot_key: str | None = None,
        related_pivot_key: str | None = None,
        parent_key: str = "id",
        related_key: str = "id",
    ) -> None:
        super().__init__(related_model, local_key=parent_key)
        self.pivot_table = pivot_table
        self.foreign_pivot_key = foreign_pivot_key
        self.related_pivot_key = related_pivot_key
        self.parent_key = parent_key
        self.related_key = related_key

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
        pivot_table = self._resolve_pivot_table(parent_cls)
        fpk = self.foreign_pivot_key or f"{snake_case(parent_cls.__name__)}_id"
        rpk = self.related_pivot_key or f"{snake_case(target.__name__)}_id"

        parent_keys = [
            getattr(i, self.parent_key, None)
            for i in instances
            if getattr(i, self.parent_key, None) is not None
        ]
        unique_keys = list(set(parent_keys))

        if not unique_keys:
            for inst in instances:
                inst._loaded_relations[self.name] = LoadedList()
            return

        builder = target.query().builder
        builder.select(
            f"{target.__table__}.*",
            Column(f"{pivot_table}.{fpk}", alias="_pivot_parent_key"),
        )
        builder.join(
            pivot_table,
            f"{target.__table__}.{self.related_key}",
            "=",
            f"{pivot_table}.{rpk}",
        )
        builder.where(f"{pivot_table}.{fpk}", "IN", unique_keys)

        rows = await builder.get()
        grouped: dict[Any, list[Model]] = {}

        for row in rows:
            pivot_parent_key = row.pop("_pivot_parent_key")
            item = target(**row)
            item._mark_persisted(row.keys())
            grouped.setdefault(pivot_parent_key, []).append(item)

        for inst in instances:
            key = getattr(inst, self.parent_key, None)
            inst._loaded_relations[self.name] = LoadedList(grouped.get(key, []))

    def build_lazy_query(self, instance: Model) -> ModelQueryBuilder[Any]:
        target = self.get_target_model()
        parent_cls = (
            self.owner_cls if self.owner_cls is not None else instance.__class__
        )
        pivot_table = self._resolve_pivot_table(parent_cls)
        fpk = self.foreign_pivot_key or f"{snake_case(parent_cls.__name__)}_id"
        rpk = self.related_pivot_key or f"{snake_case(target.__name__)}_id"

        builder = target.query()
        # Explicitly scope select to target table so pivot table columns do not overwrite target attributes
        builder.select(f"{target.__table__}.*")
        builder.builder.join(
            pivot_table,
            f"{target.__table__}.{self.related_key}",
            "=",
            f"{pivot_table}.{rpk}",
        )
        builder.builder.where(
            f"{pivot_table}.{fpk}", getattr(instance, self.parent_key, None)
        )
        return builder


class RelationshipQueryBuilder[T: Model]:
    """Scoped query builder for an Active Record relationship.

    Lazily awaitable when explicitly queried (e.g. `await user.posts` or
    `await user.posts.where("published", True).get()`).

    Synchronous data access (such as iterating, `len()`, or accessing model attributes)
    raises `RelationNotLoadedError` to prevent silent N+1 query antipatterns.
    """

    def __init__(
        self,
        instance: Model,
        relation: Relation,
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

    def where(self, column: str, operator_or_value: Any, value: Any = _MISSING) -> Self:
        if value is _MISSING:
            self._builder.where(column, operator_or_value)
        else:
            self._builder.where(column, operator_or_value, value)
        return self

    def where_raw(self, sql: str, bindings: list[Any] | None = None) -> Self:
        self._builder.where_raw(sql, bindings)
        return self

    def where_null(self, column: str) -> Self:
        self._builder.where_null(column)
        return self

    def where_not_null(self, column: str) -> Self:
        self._builder.where_not_null(column)
        return self

    def order_by(self, column: str, direction: str = "ASC") -> Self:
        self._builder.order_by(column, direction)
        return self

    def limit(self, value: int) -> Self:
        self._builder.limit(value)
        return self

    def select(self, *columns: str) -> Self:
        self._builder.select(*columns)
        return self

    def with_(self, *relations: str) -> Self:
        self._builder.with_(*relations)
        return self

    async def get(self) -> list[T]:
        """Execute the relationship query and return hydrated models."""
        return await self._builder.get()

    async def first(self) -> T | None:
        """Fetch the first record matching the relationship scope."""
        return await self._builder.first()

    async def count(self, column: str = "*") -> int:
        """Return the count of related records."""
        return await self._builder.count(column)

    async def exists(self) -> bool:
        """Return True if at least one related record exists."""
        return await self._builder.exists()

    async def create(self, *args: dict[str, Any], **kwargs: Any) -> T:
        """Create a new model instance pre-bound to this relationship."""
        if isinstance(self._relation, BelongsToMany):
            raise NotImplementedError(
                "create() would leave the new row unlinked; use attach()."
            )

        target_model = self._relation.get_target_model()
        data: dict[str, Any] = {}
        if args:
            data.update(args[0])
        data.update(kwargs)

        if isinstance(self._relation, (HasMany, HasOne)):
            owner_name = (
                self._relation.owner_cls.__name__
                if self._relation.owner_cls is not None
                else self._instance.__class__.__name__
            )
            fk = self._relation.foreign_key or f"{snake_case(owner_name)}_id"
            data[fk] = getattr(self._instance, self._relation.local_key, None)

        created = await target_model.create(**data)
        return created  # type: ignore[return-value]

    def __await__(self) -> Generator[Any, None, Any]:
        """Allow directly awaiting the relation query builder."""
        if isinstance(self._relation, (HasOne, BelongsTo)):
            return self.first().__await__()
        return self.get().__await__()

    def __iter__(self) -> Any:
        raise self._not_loaded_error()

    def __len__(self) -> int:
        raise self._not_loaded_error()

    def __bool__(self) -> bool:
        raise self._not_loaded_error()

    def __getitem__(self, key: Any) -> Any:
        raise self._not_loaded_error()

    def __getattr__(self, name: str) -> Any:
        if hasattr(self._builder, name):
            return getattr(self._builder, name)
        raise self._not_loaded_error(name)


class RelationDescriptor:
    """Descriptor that intercepts relation property access on Model instances."""

    def __init__(self, name: str, relation: Relation) -> None:
        self.name = name
        self.relation = relation

    def __get__(self, instance: Model | None, owner: type[Model]) -> Any:
        if instance is None:
            return self.relation

        if self.name in instance._loaded_relations:
            return instance._loaded_relations[self.name]

        builder = self.relation.build_lazy_query(instance)
        return RelationshipQueryBuilder(instance, self.relation, builder)

    def __set__(self, instance: Model, value: Any) -> None:
        instance._loaded_relations[self.name] = value


__all__ = [
    "BelongsTo",
    "BelongsToMany",
    "HasMany",
    "HasOne",
    "LoadedList",
    "Relation",
    "RelationDescriptor",
    "RelationNotLoadedError",
    "RelationshipQueryBuilder",
]
