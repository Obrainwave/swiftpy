from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from swiftpy.http.request import Request
    from swiftpy.validation.validator import ValidationResult


class FormRequest:
    """
    Base class for typed request validation. Handlers can declare a parameter
    typed with a FormRequest subclass; SwiftPY automatically validates incoming
    request payloads and returns HTTP 422 if validation fails.
    """

    schema: ClassVar[dict[str, Any]] = {}

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
    def get_rules(cls) -> dict[str, Any]:
        """Resolve validation rules for this class."""
        try:
            instance = cls()
            return instance.rules()
        except TypeError:
            return getattr(cls, "schema", {})

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
