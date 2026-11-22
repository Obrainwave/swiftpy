from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable


class UserResource:
    """HTTP API resource transformer for User entities and schemas."""

    def __init__(self, resource: Any) -> None:
        self.resource = resource

    def to_dict(self) -> dict[str, Any]:
        """Transform a User model or UserResponseSchema into an HTTP-safe dictionary."""
        if isinstance(self.resource, dict):
            return {
                "id": self.resource.get("id"),
                "name": self.resource.get("name"),
                "email": self.resource.get("email"),
            }
        return {
            "id": getattr(self.resource, "id", None),
            "name": getattr(self.resource, "name", None),
            "email": getattr(self.resource, "email", None),
        }

    @classmethod
    def collection(cls, items: Iterable[Any]) -> list[dict[str, Any]]:
        """Transform a collection of user items into HTTP-safe dictionaries."""
        return [cls(item).to_dict() for item in items]
