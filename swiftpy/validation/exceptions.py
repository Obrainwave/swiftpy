from __future__ import annotations

from swiftpy.http.exceptions import HttpException


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
    ) -> None:
        self.errors = errors
        super().__init__(
            message=message,
            status_code=422,
            headers=headers,
        )


__all__ = ["ValidationException"]
