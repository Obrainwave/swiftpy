from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models.user import User


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

    @classmethod
    def from_model(cls, user: User) -> UserResponseSchema:
        return cls(id=user.id, name=user.name, email=user.email)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


UserSchema = UserResponseSchema
