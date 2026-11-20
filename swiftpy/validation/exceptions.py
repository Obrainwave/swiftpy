from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from swiftpy.http.exceptions import HttpException


def _extract_first_error(
    errors: Mapping[str, list[str]], field_name: str | None = None
) -> str | None:
    if field_name is not None:
        field_errors = errors.get(field_name)
        return field_errors[0] if field_errors else None
    for field_errors in errors.values():
        if field_errors:
            return field_errors[0]
    return None


def _extract_first_errors(errors: Mapping[str, list[str]]) -> dict[str, str]:
    return {field: msgs[0] for field, msgs in errors.items() if msgs}


class ValidationException(HttpException):
    """
    Exception raised when validation fails.
    Carries structured field error messages and defaults to HTTP status 422.
    """

    def __init__(
        self,
        errors: dict[str, list[str]],
        message: str = "The given data was invalid.",
        headers: dict[str, str] | None = None,
        *,
        single_error: bool = False,
    ) -> None:
        self.errors = errors
        self._single = single_error
        super().__init__(
            message=message,
            status_code=422,
            headers=headers,
        )

    @property
    def is_single(self) -> bool:
        """Return True if this exception is configured to render in single-error format."""
        return self._single

    def first_field(self) -> str | None:
        """Return the name of the first field that failed validation."""
        for field, msgs in self.errors.items():
            if msgs:
                return field
        return None

    def first_error(self, field_name: str | None = None) -> str | None:
        """Return the first error message for `field_name`, or across all fields."""
        return _extract_first_error(self.errors, field_name)

    def first_errors(self) -> dict[str, str]:
        """Return a mapping of each failing field to its first error message."""
        return _extract_first_errors(self.errors)

    def single(self) -> ValidationException:
        """Return a copy configured to render a single primary error message."""
        return ValidationException(
            self.errors,
            message=self.message,
            headers=self.headers,
            single_error=True,
        )

    def to_dict(self, *, single: bool | None = None) -> dict[str, Any]:
        """Serialize the validation error payload as either full field map or single error."""
        use_single = self._single if single is None else single
        if use_single:
            field = self.first_field()
            err = self.first_error()
            if field is not None and err is not None:
                return {"message": err, "field": field}
            return {"message": self.message}
        return {"errors": {k: list(v) for k, v in self.errors.items()}}


__all__ = ["ValidationException"]
