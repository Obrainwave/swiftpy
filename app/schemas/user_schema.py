from __future__ import annotations

from swiftpy.schemas import BaseSchema, Field


class UserCreateSchema(BaseSchema):
    """Input validation schema for user creation requests."""

    name: str = Field(..., min_length=2, max_length=100)
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=8)


class UserResponseSchema(BaseSchema):
    """Output serialization schema for user responses."""

    id: int
    name: str
    email: str


UserSchema = UserResponseSchema
