from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class UserCreateSchema:
    """Input validation DTO for user creation requests."""

    name: str
    email: str
    password: str


@dataclass(slots=True)
class UserResponseSchema:
    """Output serialization schema for user responses."""

    id: int
    name: str
    email: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

