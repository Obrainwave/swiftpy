from __future__ import annotations

from typing import Any, ClassVar

from swiftpy.core.config import ConfigurationError
from swiftpy.core.container import Container, get_container
from swiftpy.http.request import Request
from swiftpy.validation.rules.registry import RuleRegistry, default_registry
from swiftpy.validation.validator import ValidationResult


class FormRequest:
    """
    Base class for typed request validation. Handlers can declare a parameter
    typed with a FormRequest subclass; SwiftPY automatically validates incoming
    request payloads and returns HTTP 422 if validation fails.
    """

    schema: ClassVar[dict[str, Any]] = {}
    _cached_parsed_rules: ClassVar[dict[str, list[Any]] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._cached_parsed_rules = None

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

    @classmethod
    def get_rules(cls, container: Container | None = None) -> dict[str, Any]:
        """
        Resolve validation rules for this class.
        Fails closed with ConfigurationError if rule resolution or instantiation fails (D-9).
        """
        try:
            cont = container
            if cont is None:
                try:
                    cont = get_container()
                except RuntimeError:
                    pass

            if cont is not None and cont.has(cls):
                instance = cont.resolve(cls)
            else:
                try:
                    instance = cls()
                except TypeError:
                    if cont is not None and cont.can_resolve(cls):
                        instance = cont.resolve(cls)
                    else:
                        raise

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
    def get_parsed_rules(
        cls, registry: RuleRegistry | None = None, container: Container | None = None
    ) -> dict[str, list[Any]]:
        """
        Resolve and cache parsed validation rules once per FormRequest class.
        """
        if cls._cached_parsed_rules is not None:
            return cls._cached_parsed_rules

        rules_dict = cls.get_rules(container=container)
        reg = registry or default_registry
        parsed: dict[str, list[Any]] = {}
        for field_name, rule_def in rules_dict.items():
            parsed[field_name] = reg.parse_rules(rule_def)

        cls._cached_parsed_rules = parsed
        return parsed

    @classmethod
    def clear_rule_cache(cls) -> None:
        """Clear the parsed rules cache for this class (useful for tests)."""
        cls._cached_parsed_rules = None

    def validated(self) -> dict[str, Any]:
        """Return the dictionary of validated, cleaned parameters."""
        return dict(self.data)

    def all(self) -> dict[str, Any]:
        """Return all raw input parameters."""
        return dict(self.raw_data)

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
