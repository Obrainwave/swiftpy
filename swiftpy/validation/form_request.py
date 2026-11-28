from __future__ import annotations

import inspect
import types
from collections.abc import Mapping
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    Self,
    TypeVar,
    get_args,
    get_origin,
    overload,
)
from weakref import WeakKeyDictionary

from swiftpy.core.config import ConfigurationError
from swiftpy.core.container import Container, get_container
from swiftpy.validation.rules.registry import RuleRegistry, default_registry
from swiftpy.validation.validator import ValidatedData, ValidationResult

if TYPE_CHECKING:
    from typing_extensions import TypeVar as _SchemaTypeVar

    from swiftpy.http.request import Request
    from swiftpy.schemas.base import BaseSchema

    SchemaT_co = _SchemaTypeVar("SchemaT_co", covariant=True, default=Any)
else:
    SchemaT_co = TypeVar("SchemaT_co", covariant=True)

_TargetSchemaT = TypeVar("_TargetSchemaT", bound="BaseSchema")


def _is_concrete_schema_class(value: object) -> bool:
    return (
        isinstance(value, type)
        and getattr(value, "_is_swiftpy_schema", False) is True
        and any(getattr(b, "_is_swiftpy_schema", False) is True for b in value.__mro__[1:])
    )


def unwrap_form_annotation(
    annotation: Any,
) -> tuple[type[FormRequest[Any]], type[BaseSchema] | None]:
    """Resolve a `FormRequest` subclass (or `FormRequest[Schema]`) into `(form_cls, schema_cls)`."""
    if isinstance(annotation, type) and issubclass(annotation, FormRequest):
        schema_cls = getattr(annotation, "schema_model", None)
        return (
            annotation,
            schema_cls if _is_concrete_schema_class(schema_cls) else None,
        )
    origin = get_origin(annotation)
    if isinstance(origin, type) and issubclass(origin, FormRequest):
        args = get_args(annotation)
        schema_arg = args[0] if args else getattr(origin, "schema_model", None)
        return (
            origin,
            schema_arg if _is_concrete_schema_class(schema_arg) else None,
        )
    raise TypeError(f"Expected a FormRequest subclass, got {annotation!r}.")


# Python 3.12 requires Generic[...] here because PEP 696 default=Any syntax on
# `[SchemaT_co = Any]` requires Python 3.13.
class FormRequest(Generic[SchemaT_co]):  # noqa: UP046
    """
    Base class for typed request validation. Handlers can declare a parameter
    typed with a FormRequest subclass (optionally parameterized as
    `FormRequest[MySchema]` so `.validated()` returns and coerces `MySchema`);
    SwiftPY automatically validates incoming request payloads and returns HTTP 422
    if validation fails.
    """

    _is_swiftpy_form_request: ClassVar[bool] = True
    schema: ClassVar[dict[str, Any]] = {}
    schema_model: ClassVar[type[BaseSchema] | None] = None
    stop_on_first_error: ClassVar[bool] = False
    _cached_parsed_rules: ClassVar[dict[str, list[Any]] | None] = None
    _cached_parsed_by_registry: ClassVar[
        WeakKeyDictionary[RuleRegistry, dict[str, list[Any]]] | None
    ] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._cached_parsed_rules = None
        cls._cached_parsed_by_registry = None

        if "schema_model" in cls.__dict__:
            explicit = cls.__dict__["schema_model"]
            if explicit is not None and not _is_concrete_schema_class(explicit):
                raise TypeError(
                    f"{cls.__name__}.schema_model must be a BaseSchema subclass or None, "
                    f"got {explicit!r}."
                )
            return

        for base in types.get_original_bases(cls):
            origin = get_origin(base)
            if isinstance(origin, type) and issubclass(origin, FormRequest):
                args = get_args(base)
                if len(args) == 1 and _is_concrete_schema_class(args[0]):
                    cls.schema_model = args[0]
                    return

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        raw_data: dict[str, Any] | None = None,
        request: Request | None = None,
        validation_result: ValidationResult | None = None,
    ) -> None:
        self.data: dict[str, Any] = data or {}
        self.raw_data: dict[str, Any] = (
            raw_data if raw_data is not None else dict(self.data)
        )
        self.request = request
        self.validation_result = validation_result
        self._validated_schema: BaseSchema | None = None
        self._validated_schema_data_snapshot: dict[str, Any] | None = None
        self._bound_schema_model: type[BaseSchema] | None = None
        self._schema_fields: tuple[str, ...] | None = None

    def rules(self) -> Mapping[str, Any]:
        """
        Define validation rules. Defaults to returning the class 'schema' attribute.
        Can be overridden in subclasses for dynamic rules.
        """
        return self.schema

    def messages(self) -> Mapping[str, str]:
        """
        Define custom validation error messages keyed by 'field.rule', 'field', or 'rule'.
        Can be overridden in subclasses.
        """
        return {}

    @classmethod
    def _requires_di_resolution(cls) -> bool:
        """Return True only when cls.__init__ declares required dependency parameters."""
        if cls.__init__ is FormRequest.__init__:
            return False
        sig = inspect.signature(cls.__init__)
        for idx, param in enumerate(sig.parameters.values()):
            if idx == 0:
                continue
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            if param.default is inspect.Parameter.empty:
                return True
        return False

    @classmethod
    def resolve_instance(
        cls,
        *,
        data: dict[str, Any] | None = None,
        raw_data: dict[str, Any] | None = None,
        request: Request | None = None,
        validation_result: ValidationResult | None = None,
        container: Container | None = None,
    ) -> Self:
        """
        Instantiate a FormRequest using explicit DI resolution only when required,
        avoiding broad TypeError fallbacks that mask constructor bugs.
        """
        cont = container
        if cont is None:
            try:
                cont = get_container()
            except RuntimeError:
                pass

        if cont is not None and cont.has(cls):
            instance = cont.resolve(cls)
        elif cls._requires_di_resolution():
            if cont is not None and cont.can_resolve(cls):
                instance = cont.resolve(cls)
            else:
                raise ConfigurationError(
                    f"Failed to resolve dependencies for {cls.__name__}"
                )
        elif cls.__init__ is FormRequest.__init__:
            instance = cls(
                data=data,
                raw_data=raw_data,
                request=request,
                validation_result=validation_result,
            )
        else:
            sig = inspect.signature(cls.__init__)
            params = sig.parameters
            has_varkw = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            init_kwargs: dict[str, Any] = {}
            candidate_kwargs: dict[str, Any] = {
                "data": data,
                "raw_data": raw_data,
                "request": request,
                "validation_result": validation_result,
            }
            for key, val in candidate_kwargs.items():
                if val is not None and (has_varkw or key in params):
                    init_kwargs[key] = val
            instance = cls(**init_kwargs)

        if data is not None:
            instance.data = data
        elif not hasattr(instance, "data"):
            instance.data = {}
        if raw_data is not None:
            instance.raw_data = raw_data
        elif not hasattr(instance, "raw_data"):
            instance.raw_data = dict(instance.data)
        if request is not None or not hasattr(instance, "request"):
            instance.request = request
        if validation_result is not None or not hasattr(
            instance, "validation_result"
        ):
            instance.validation_result = validation_result
        return instance

    @classmethod
    def get_rules(
        cls,
        container: Container | None = None,
        request: Request | None = None,
    ) -> dict[str, Any]:
        """
        Resolve validation rules for this class.
        Fails closed with ConfigurationError if rule resolution fails (D-9).
        """
        instance = cls.resolve_instance(request=request, container=container)
        try:
            rules_def = instance.rules()
            if not isinstance(rules_def, Mapping):
                raise ConfigurationError(
                    f"'{cls.__name__}.rules()' must return a dict, got {type(rules_def).__name__}"
                )
            return dict(rules_def)
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError(
                f"Failed to resolve validation rules for {cls.__name__}: {exc}"
            ) from exc

    @classmethod
    def prepare_validation(
        cls,
        *,
        registry: RuleRegistry | None = None,
        container: Container | None = None,
        request: Request | None = None,
    ) -> tuple[Self, dict[str, list[Any]], dict[str, str]]:
        """
        Resolve FormRequest instance with current request context and return
        (instance, parsed_rules, custom_messages).
        Dynamic rules() overrides are evaluated per request rather than frozen on the class.
        """
        instance = cls.resolve_instance(request=request, container=container)
        try:
            rules_def = instance.rules()
            if not isinstance(rules_def, Mapping):
                raise ConfigurationError(
                    f"'{cls.__name__}.rules()' must return a dict, got {type(rules_def).__name__}"
                )
            messages_def = instance.messages()
            if not isinstance(messages_def, Mapping):
                raise ConfigurationError(
                    f"'{cls.__name__}.messages()' must return a dict, got {type(messages_def).__name__}"
                )
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError(
                f"Failed to resolve validation metadata for {cls.__name__}: {exc}"
            ) from exc

        reg = registry or default_registry
        messages_dict = dict(messages_def)
        is_static_schema = cls.rules is FormRequest.rules
        if is_static_schema:
            cached = cls._get_cached_static_rules(reg)
            if cached is not None:
                return instance, cached, messages_dict

        parsed: dict[str, list[Any]] = {}
        for field_name, rule_def in rules_def.items():
            parsed[field_name] = reg.parse_rules(rule_def)

        if is_static_schema:
            cls._set_cached_static_rules(reg, parsed)

        return instance, parsed, messages_dict

    @classmethod
    def _get_cached_static_rules(
        cls, registry: RuleRegistry
    ) -> dict[str, list[Any]] | None:
        if registry is default_registry:
            return cls._cached_parsed_rules
        if cls._cached_parsed_by_registry is not None:
            return cls._cached_parsed_by_registry.get(registry)
        return None

    @classmethod
    def _set_cached_static_rules(
        cls, registry: RuleRegistry, parsed: dict[str, list[Any]]
    ) -> None:
        if registry is default_registry:
            cls._cached_parsed_rules = parsed
            return
        if cls._cached_parsed_by_registry is None:
            cls._cached_parsed_by_registry = WeakKeyDictionary()
        cls._cached_parsed_by_registry[registry] = parsed

    @classmethod
    def get_parsed_rules(
        cls,
        registry: RuleRegistry | None = None,
        container: Container | None = None,
        request: Request | None = None,
    ) -> dict[str, list[Any]]:
        """
        Resolve and parse validation rules. Static schema classes cache per registry;
        dynamic rules() subclasses are evaluated per call with request context.
        """
        reg = registry or default_registry
        is_static_schema = cls.rules is FormRequest.rules
        if is_static_schema:
            cached = cls._get_cached_static_rules(reg)
            if cached is not None:
                return cached

        rules_dict = cls.get_rules(container=container, request=request)
        parsed: dict[str, list[Any]] = {}
        for field_name, rule_def in rules_dict.items():
            parsed[field_name] = reg.parse_rules(rule_def)

        if is_static_schema:
            cls._set_cached_static_rules(reg, parsed)
        return parsed

    @classmethod
    def clear_rule_cache(cls) -> None:
        """Clear the parsed rules cache for this class (useful for tests)."""
        cls._cached_parsed_rules = None
        if cls._cached_parsed_by_registry is not None:
            cls._cached_parsed_by_registry.clear()

    def _resolve_declared_field_names(self) -> tuple[str, ...]:
        schema_fields: tuple[str, ...] | None = getattr(self, "_schema_fields", None)
        if schema_fields is not None:
            return schema_fields
        try:
            rules_def = self.rules()
        except (
            AttributeError,
            ConfigurationError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            rules_def = self.schema
        resolved: tuple[str, ...] = ()
        if isinstance(rules_def, Mapping):
            resolved = tuple(
                dict.fromkeys(
                    k
                    for k in rules_def
                    if "*" not in k and "." not in k
                )
            )
        self._schema_fields = resolved
        return resolved

    def _hydrate_bound_schema(
        self,
        schema_cls: type[BaseSchema],
        *,
        use_raw_data: bool = False,
    ) -> BaseSchema:
        """Validate payload into `schema_cls` and synchronize instance state."""
        stop_first = bool(
            getattr(self, "stop_on_first_error", False)
            or getattr(schema_cls, "stop_on_first_error", False)
        )
        source_payload = self.raw_data if use_raw_data else self.data
        schema_inst = schema_cls._validate_mapping(
            source_payload,
            stop_on_first_error=stop_first,
        )
        merged_data = dict(self.data)
        fields_set = schema_inst.fields_set
        for alias_key, canonical_name in schema_cls.__schema_aliases__.items():
            if alias_key != canonical_name and alias_key in merged_data:
                del merged_data[alias_key]
        for fname in schema_cls.__schema_field_order__:
            if fname in fields_set:
                merged_data[fname] = getattr(schema_inst, fname)
        extra_fields = getattr(schema_inst, "__extra_fields__", None)
        if isinstance(extra_fields, Mapping):
            merged_data.update(extra_fields)
        self.data = merged_data
        self._bound_schema_model = schema_cls
        self._validated_schema = schema_inst
        self._validated_schema_data_snapshot = dict(merged_data)
        return schema_inst

    def _has_fresh_cached_schema(self, schema: type[BaseSchema] | None = None) -> bool:
        cached = getattr(self, "_validated_schema", None)
        if cached is None:
            return False
        if schema is not None and type(cached) is not schema:
            return False
        snapshot = getattr(self, "_validated_schema_data_snapshot", None)
        return isinstance(snapshot, dict) and snapshot == self.data

    def to_schema(self, schema: type[_TargetSchemaT]) -> _TargetSchemaT:
        """Validate and hydrate this FormRequest's cleaned data into `schema`."""
        if not _is_concrete_schema_class(schema):
            raise TypeError(
                f"Expected a BaseSchema subclass, got {schema!r}."
            )
        cached = getattr(self, "_validated_schema", None)
        if (
            isinstance(cached, schema)
            and type(cached) is schema
            and self._has_fresh_cached_schema(schema)
        ):
            return cached
        stop_first = bool(
            getattr(self, "stop_on_first_error", False)
            or getattr(schema, "stop_on_first_error", False)
        )
        return schema._validate_mapping(
            self.data,
            stop_on_first_error=stop_first,
        )

    @overload
    def validated(self, schema: None = None) -> SchemaT_co: ...

    @overload
    def validated(self, schema: type[_TargetSchemaT]) -> _TargetSchemaT: ...

    def validated(self, schema: type[_TargetSchemaT] | None = None) -> Any:
        """
        Return the validated, cleaned parameters as the bound `BaseSchema` (when
        parameterized via `FormRequest[Schema]` / `schema_model` or passed `schema`)
        or as `ValidatedData` with dot and dict access.
        """
        if schema is not None:
            return self.to_schema(schema)
        cached = getattr(self, "_validated_schema", None)
        if cached is not None and self._has_fresh_cached_schema():
            return cached
        bound_schema: type[BaseSchema] | None = getattr(
            self, "_bound_schema_model", None
        )
        if bound_schema is None:
            bound_schema = type(self).schema_model
        if bound_schema is not None and _is_concrete_schema_class(bound_schema):
            validated_schema = self.to_schema(bound_schema)
            self._validated_schema = validated_schema
            self._validated_schema_data_snapshot = dict(self.data)
            return validated_schema
        return ValidatedData(
            self.data,
            _declared_fields=self._resolve_declared_field_names(),
        )

    def all(self) -> dict[str, Any]:
        """Return all raw input parameters."""
        return dict(self.raw_data)

    def route(self, param: str | None = None, default: Any = None) -> Any:
        """Return a route path parameter from the underlying Request, or `default` if unavailable."""
        if self.request is None:
            if param is None:
                empty: dict[str, Any] = {}
                return empty
            return default
        return self.request.route(param, default)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, item: str) -> Any:
        return self.data[item]

    def __contains__(self, item: str) -> bool:
        return item in self.data

    def __getattr__(self, name: str) -> Any:
        if name in self.data:
            return self.data[name]
        cached_schema = self.__dict__.get("_validated_schema")
        if (
            cached_schema is not None
            and name in cached_schema.__class__.__schema_fields__
        ):
            return getattr(cached_schema, name)
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )


__all__ = ["FormRequest", "unwrap_form_annotation"]
