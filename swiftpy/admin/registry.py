from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast, get_args, get_origin

from swiftpy.database.orm.model import Model
from swiftpy.database.orm.relationships import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
    Relation,
)
from swiftpy.support.strings import pluralize, snake_case

if TYPE_CHECKING:
    from swiftpy.core.application import Application
    from swiftpy.database.orm.model import ModelQueryBuilder
    from swiftpy.http.router import Router

T = TypeVar("T", bound=Model)
A = TypeVar("A", bound="ModelAdmin")


def _humanize_name(name: str) -> str:
    """Convert snake_case column or identifier to title case label."""
    if name.lower() == "id":
        return "ID"
    clean = name.removesuffix("_id") if name.endswith("_id") and len(name) > 3 else name
    return clean.replace("_", " ").title()


def _infer_type_name(hint: Any) -> str:
    """Infer primitive string type name from a Python type annotation."""
    if hint is None or hint is type(None):
        return "str"

    if isinstance(hint, str):
        norm = hint.strip().lower()
        if "bool" in norm:
            return "bool"
        if "int" in norm:
            return "int"
        if "float" in norm or "decimal" in norm:
            return "float"
        if "datetime" in norm or "date" in norm:
            return "datetime"
        if "dict" in norm or "mapping" in norm:
            return "dict"
        if "list" in norm or "sequence" in norm or "set" in norm or "tuple" in norm:
            return "list"
        return "str"

    origin = get_origin(hint)
    if origin is not None:
        args = get_args(hint)
        # Handle Optional[X] or Union[X, None]
        non_none_args = [a for a in args if a is not type(None)]
        if non_none_args:
            return _infer_type_name(non_none_args[0])
        return "str"

    if isinstance(hint, type):
        name = hint.__name__.lower()
        if name in ("int", "integer"):
            return "int"
        if name in ("float", "decimal"):
            return "float"
        if name in ("bool", "boolean"):
            return "bool"
        if name in ("datetime", "date", "time"):
            return "datetime"
        if name in ("dict", "mapping"):
            return "dict"
        if name in ("list", "sequence", "set", "tuple"):
            return "list"
        return "str"

    return "str"


class FieldMetadata:
    """Metadata describing a declared field on an Active Record Model."""

    def __init__(
        self,
        name: str,
        type_hint: Any,
        nullable: bool = True,
        default: Any = None,
        is_primary_key: bool = False,
        is_foreign_key: bool = False,
        foreign_relation_name: str | None = None,
        foreign_model_name: str | None = None,
    ) -> None:
        self.name = name
        self.type_hint = type_hint
        self.type_name = _infer_type_name(type_hint)
        self.nullable = nullable
        self.default = default
        self.is_primary_key = is_primary_key
        self.is_foreign_key = is_foreign_key
        self.foreign_relation_name = foreign_relation_name
        self.foreign_model_name = foreign_model_name
        self.label = _humanize_name(name)

    @property
    def input_type(self) -> str:
        if self.is_foreign_key:
            return "select"
        if self.type_name == "int":
            return "number"
        if self.type_name == "float":
            return "number"
        if self.type_name == "bool":
            return "checkbox"
        if self.type_name == "datetime":
            return "datetime-local"
        if self.name in ("content", "description", "body", "bio", "notes", "text"):
            return "textarea"
        return "text"

    def coerce_value(self, raw_value: Any) -> Any:
        """Coerce raw HTTP form/JSON input to the declared field type."""
        if self.type_name == "bool":
            if isinstance(raw_value, bool):
                return raw_value
            if raw_value is None or raw_value == "":
                return False
            norm = str(raw_value).strip().lower()
            return norm in ("true", "1", "yes", "on")

        if raw_value is None or raw_value == "":
            if self.default is not None:
                return self.default
            if self.nullable:
                return None
            if self.type_name == "str":
                return ""
            return None

        if self.type_name == "int":
            try:
                return int(raw_value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Field '{self.name}' must be a valid integer.") from exc

        if self.type_name == "float":
            try:
                return float(raw_value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Field '{self.name}' must be a valid number.") from exc

        return str(raw_value)


class RelationshipMetadata:
    """Metadata describing a declared relationship on an Active Record Model."""

    def __init__(
        self,
        name: str,
        relation_obj: Relation,
        owner_cls: type[Model],
    ) -> None:
        self.name = name
        self.relation_obj = relation_obj
        self.owner_cls = owner_cls
        self.foreign_key = relation_obj.foreign_key or f"{snake_case(owner_cls.__name__)}_id"
        self.local_key = relation_obj.local_key

        if isinstance(relation_obj, BelongsTo):
            self.relation_type = "BelongsTo"
        elif isinstance(relation_obj, HasMany):
            self.relation_type = "HasMany"
        elif isinstance(relation_obj, HasOne):
            self.relation_type = "HasOne"
        elif isinstance(relation_obj, BelongsToMany):
            self.relation_type = "BelongsToMany"
        else:
            self.relation_type = "Relation"

        if isinstance(relation_obj, BelongsToMany):
            self.pivot_table = getattr(relation_obj, "pivot_table", None)
        else:
            self.pivot_table = None

        self.target_model_name = relation_obj.related_model_name
        self.label = _humanize_name(name)

    def get_target_model(self) -> type[Model] | None:
        """Resolve the target Model class, or return None if unresolved."""
        try:
            return self.relation_obj.get_target_model()
        except (KeyError, ValueError, AttributeError, LookupError, RuntimeError):
            return None


class ModelAdmin:
    """
    Administrative configuration for a registered Model.
    Controls list display columns, search fields, ordering, and CRUD form behaviors.
    """

    model: type[Model]
    list_display: ClassVar[list[str] | tuple[str, ...] | None] = None
    search_fields: ClassVar[list[str]] = []
    ordering: ClassVar[str | None] = None
    readonly_fields: ClassVar[list[str]] = []
    exclude: ClassVar[list[str]] = []
    per_page: ClassVar[int] = 25

    def __init__(self, model: type[Model], site: AdminSite | None = None) -> None:
        self.model = model
        self.site = site
        self._fields_cache: list[FieldMetadata] | None = None
        self._relations_cache: dict[str, RelationshipMetadata] | None = None

    def __call__(self, admin_cls: type[ModelAdmin]) -> type[ModelAdmin]:
        """Support decorator usage: @admin.register(Model) class MyAdmin(ModelAdmin): ..."""
        if (
            isinstance(admin_cls, type)
            and issubclass(admin_cls, ModelAdmin)
            and self.site is not None
        ):
            self.site.unregister(self.model)
            instance = admin_cls(self.model, site=self.site)
            self.site._register_model_admin(self.model, instance)
        return admin_cls

    @property
    def primary_key(self) -> str:
        """Declared primary key column name on the underlying model."""
        return str(getattr(self.model, "__primary_key__", "id"))

    @property
    def slug(self) -> str:
        """URL slug for this model in the admin panel."""
        raw_table = getattr(self.model, "__table__", None)
        if raw_table and isinstance(raw_table, str):
            return raw_table.lower()
        return pluralize(snake_case(self.model.__name__)).lower()

    @property
    def title(self) -> str:
        """Singular humanized display name."""
        return _humanize_name(snake_case(self.model.__name__))

    @property
    def plural_title(self) -> str:
        """Plural humanized display name."""
        return pluralize(self.title)

    def get_relationships(self) -> dict[str, RelationshipMetadata]:
        """Inspect and return declared relationships on the model."""
        if self._relations_cache is not None:
            return self._relations_cache

        relations_config = getattr(self.model, "__relations_config__", {})
        result: dict[str, RelationshipMetadata] = {}
        for rel_name, rel_obj in relations_config.items():
            result[rel_name] = RelationshipMetadata(
                name=rel_name,
                relation_obj=rel_obj,
                owner_cls=self.model,
            )
        self._relations_cache = result
        return result

    def get_fields(self) -> list[FieldMetadata]:
        """Inspect and return declared fields on the model with relationship mapping."""
        if self._fields_cache is not None:
            return self._fields_cache

        declared_fields = getattr(self.model, "__fields__", {})
        defaults = getattr(self.model, "__field_defaults__", {})
        pk = getattr(self.model, "__primary_key__", "id")
        relationships = self.get_relationships()

        # Build reverse lookup of foreign_key -> (relation_name, target_model_name)
        fk_map: dict[str, tuple[str, str]] = {}
        for rel_name, rel_meta in relationships.items():
            if rel_meta.relation_type == "BelongsTo" and rel_meta.foreign_key:
                fk_map[rel_meta.foreign_key] = (rel_name, rel_meta.target_model_name)

        fields: list[FieldMetadata] = []
        for field_name, type_hint in declared_fields.items():
            is_pk = field_name == pk
            is_fk = field_name in fk_map
            rel_info = fk_map.get(field_name)
            foreign_rel_name: str | None = rel_info[0] if rel_info else None
            foreign_target_name: str | None = rel_info[1] if rel_info else None
            def_val = defaults.get(field_name)

            fields.append(
                FieldMetadata(
                    name=field_name,
                    type_hint=type_hint,
                    nullable=is_pk or field_name in defaults,
                    default=def_val,
                    is_primary_key=is_pk,
                    is_foreign_key=is_fk,
                    foreign_relation_name=foreign_rel_name,
                    foreign_model_name=foreign_target_name,
                )
            )

        self._fields_cache = fields
        return fields

    def get_field(self, name: str) -> FieldMetadata | None:
        for f in self.get_fields():
            if f.name == name:
                return f
        return None

    def get_list_display(self) -> list[str]:
        """Columns to display in the list view table."""
        if self.list_display:
            return list(self.list_display)
        all_field_names = [f.name for f in self.get_fields()]
        # Show primary key and first few fields
        pk = getattr(self.model, "__primary_key__", "id")
        cols = [pk] if pk in all_field_names else []
        for f in all_field_names:
            if f != pk and f not in cols:
                cols.append(f)
            if len(cols) >= 6:
                break
        return cols

    def get_form_fields(self) -> list[FieldMetadata]:
        """Fields to include in Create and Edit forms."""
        pk = getattr(self.model, "__primary_key__", "id")
        excluded = set(self.exclude)
        return [
            f
            for f in self.get_fields()
            if not f.is_primary_key and f.name != pk and f.name not in excluded
        ]

    def get_queryset(self) -> ModelQueryBuilder[Any]:
        """
        Produce a base ModelQueryBuilder for list views.
        Automatically eager-loads BelongsTo relationships to eliminate N+1 queries.
        """
        builder = self.model.query()
        relationships = self.get_relationships()
        eager_rels = [
            name
            for name, meta in relationships.items()
            if meta.relation_type == "BelongsTo"
        ]
        if eager_rels:
            builder.with_(*eager_rels)
        if self.ordering:
            builder.order_by(self.ordering)
        return builder


_ROUTE_COMPILER: Callable[[AdminSite, Any, str], None] | None = None
_ROUTER_FACTORY: Callable[[AdminSite, str], Any] | None = None


def set_admin_route_compiler(
    compiler: Callable[[AdminSite, Any, str], None],
    factory: Callable[[AdminSite, str], Any],
) -> None:
    """Register views compiler callbacks to avoid circular or inline imports."""
    global _ROUTE_COMPILER, _ROUTER_FACTORY
    _ROUTE_COMPILER = compiler
    _ROUTER_FACTORY = factory


class AdminSite:
    """
    Central administrative registry managing registered Models and generating CRUD screens.
    """

    def __init__(self, title: str = "SwiftPY Admin") -> None:
        self.title = title
        self._registry: dict[type[Model], ModelAdmin] = {}
        self._slug_registry: dict[str, tuple[type[Model], ModelAdmin]] = {}

    def register(
        self,
        model_or_admin: type[Model | ModelAdmin] | None = None,
        admin_class: type[ModelAdmin] | None = None,
    ) -> Any:
        """
        Register a Model with the AdminSite.
        Supports:
          1. Direct invocation: admin.register(Model, ModelAdmin)
          2. Direct invocation: admin.register(Model)
          3. Decorator on Model: @admin.register class MyModel(Model): ...
          4. Decorator on ModelAdmin: @admin.register(MyModel) class MyAdmin(ModelAdmin): ...
        """
        # Check non-Model parameter
        if model_or_admin is not None and not (isinstance(model_or_admin, type) and issubclass(model_or_admin, Model)):
            raise TypeError(f"AdminSite.register() expects a Model subclass, got {model_or_admin!r}.")

        # Case 1: admin.register(MyModel, MyAdmin)
        if (
            isinstance(model_or_admin, type)
            and issubclass(model_or_admin, Model)
            and admin_class is not None
        ):
            instance = admin_class(model_or_admin, site=self)
            self._register_model_admin(model_or_admin, instance)
            return instance

        # Case 2: @admin.register(MyModel) or admin.register(MyModel)
        if (
            isinstance(model_or_admin, type)
            and issubclass(model_or_admin, Model)
            and admin_class is None
        ):
            instance = ModelAdmin(model_or_admin, site=self)
            self._register_model_admin(model_or_admin, instance)
            return instance

        # Case 3: parameterless decorator: @admin.register class MyModel(Model): ...
        def parameterless_decorator(cls: type[Model]) -> type[Model]:
            if not isinstance(cls, type) or not issubclass(cls, Model):
                raise TypeError(f"AdminSite.register() expects a Model subclass, got {cls!r}.")
            instance = ModelAdmin(cls, site=self)
            self._register_model_admin(cls, instance)
            return cls

        return parameterless_decorator

    def _register_model_admin(self, model: type[Model], admin_instance: ModelAdmin) -> None:
        if model in self._registry:
            raise ValueError(f"Model '{model.__name__}' is already registered with AdminSite '{self.title}'.")
        if admin_instance.slug in self._slug_registry:
            existing_model = self._slug_registry[admin_instance.slug][0]
            raise ValueError(
                f"Admin slug collision: '{admin_instance.slug}' is already registered for model '{existing_model.__name__}'."
            )
        self._registry[model] = admin_instance
        self._slug_registry[admin_instance.slug] = (model, admin_instance)

    def unregister(self, model: type[Model]) -> None:
        """Remove a model from the admin registry."""
        admin_instance = self._registry.pop(model, None)
        if admin_instance:
            self._slug_registry.pop(admin_instance.slug, None)

    def is_registered(self, model: type[Model]) -> bool:
        """Check if a Model class is registered in the admin site."""
        return model in self._registry

    def get_model_admin(self, model: type[Model]) -> ModelAdmin:
        """Retrieve the ModelAdmin instance for a registered model."""
        if model not in self._registry:
            raise KeyError(f"Model '{model.__name__}' is not registered with the admin site.")
        return self._registry[model]

    def get_by_slug(self, slug: str) -> tuple[type[Model], ModelAdmin] | None:
        """Retrieve (Model, ModelAdmin) pair by its URL slug."""
        return self._slug_registry.get(slug.lower().strip())

    def all_registered(self) -> list[ModelAdmin]:
        """Return all registered ModelAdmin instances."""
        return list(self._registry.values())

    def mount(self, app: Application, prefix: str = "/admin") -> None:
        """Mount auto-generated CRUD routes onto a SwiftPY Application."""
        if _ROUTE_COMPILER is None:
            raise RuntimeError("Admin views route compiler is not initialized.")
        _ROUTE_COMPILER(self, app, prefix)

    def get_router(self, prefix: str = "/admin") -> Router:
        """Return a configured Router containing all admin CRUD routes."""
        if _ROUTER_FACTORY is None:
            raise RuntimeError("Admin views router factory is not initialized.")
        return cast("Router", _ROUTER_FACTORY(self, prefix))


# Global singleton instance
admin = AdminSite()

__all__ = [
    "AdminSite",
    "FieldMetadata",
    "ModelAdmin",
    "RelationshipMetadata",
    "admin",
]
