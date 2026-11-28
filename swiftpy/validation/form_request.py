from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, ClassVar, Self
from weakref import WeakKeyDictionary

from swiftpy.core.config import ConfigurationError
from swiftpy.core.container import Container, get_container
from swiftpy.validation.rules.registry import RuleRegistry, default_registry
from swiftpy.validation.validator import ValidatedData, ValidationResult

if TYPE_CHECKING:
    from swiftpy.http.request import Request


class FormRequest:
    """
    Base class for typed request validation. Handlers can declare a parameter
    typed with a FormRequest subclass; SwiftPY automatically validates incoming
    request payloads and returns HTTP 422 if validation fails.
    """

    schema: ClassVar[dict[str, Any]] = {}
    stop_on_first_error: ClassVar[bool] = False
    _cached_parsed_rules: ClassVar[dict[str, list[Any]] | None] = None
    _cached_parsed_by_registry: ClassVar[
        WeakKeyDictionary[RuleRegistry, dict[str, list[Any]]] | None
    ] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._cached_parsed_rules = None
        cls._cached_parsed_by_registry = None

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

    def rules(self) -> dict[str, Any]:
        """
        Define validation rules. Defaults to returning the class 'schema' attribute.
        Can be overridden in subclasses for dynamic rules.
        """
        return self.schema

    def messages(self) -> dict[str, str]:
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
            if not isinstance(rules_def, dict):
                raise ConfigurationError(
                    f"'{cls.__name__}.rules()' must return a dict, got {type(rules_def).__name__}"
                )
            return rules_def
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
            if not isinstance(rules_def, dict):
                raise ConfigurationError(
                    f"'{cls.__name__}.rules()' must return a dict, got {type(rules_def).__name__}"
                )
            messages_def = instance.messages()
            if not isinstance(messages_def, dict):
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
        is_static_schema = cls.rules is FormRequest.rules
        if is_static_schema:
            cached = cls._get_cached_static_rules(reg)
            if cached is not None:
                return instance, cached, messages_def

        parsed: dict[str, list[Any]] = {}
        for field_name, rule_def in rules_def.items():
            parsed[field_name] = reg.parse_rules(rule_def)

        if is_static_schema:
            cls._set_cached_static_rules(reg, parsed)

        return instance, parsed, messages_def

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

    def validated(self) -> ValidatedData:
        """Return the validated, cleaned parameters with dot and dict access."""
        return ValidatedData(self.data)

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
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )


__all__ = ["FormRequest"]
