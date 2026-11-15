from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ValidatorRule(Protocol):
    """
    Protocol for validation rules.
    Rules can be synchronous or asynchronous.
    """

    def validate(
        self,
        field: str,
        value: Any,
        data: dict[str, Any],
    ) -> bool | Awaitable[bool]:
        """
        Validate whether the given field value is valid according to this rule.
        Can be synchronous (returning bool) or asynchronous (returning Awaitable[bool]).
        """
        ...

    def message(self, field: str) -> str:
        """
        Return the validation error message when this rule fails.
        """
        ...


__all__ = ["ValidatorRule"]
