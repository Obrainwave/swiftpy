from __future__ import annotations


class HttpException(Exception):
    """
    Base HTTP exception for request pipeline errors.
    Automatically caught and converted into JSON responses by Application.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.headers = headers
        super().__init__(message)


__all__ = ["HttpException"]
