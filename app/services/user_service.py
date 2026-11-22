from __future__ import annotations

from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user_schema import UserResponseSchema


class UserService:
    """
    Domain business logic layer for user entity management.
    Coordinates between repositories, schemas, transactions, events, and background notifications.
    """

    def __init__(self, repository: UserRepository | None = None) -> None:
        self.repository = repository or UserRepository()

    async def list_users(self) -> list[UserResponseSchema]:
        """Return starter user response DTOs."""
        return [
            UserResponseSchema(
                id=1,
                name="Alice Doe",
                email="alice@example.com",
            )
        ]

    async def create_user(
        self, name: str, email: str, password_hash: str
    ) -> User:
        """Create a new user account with hashed credentials."""
        user = await self.repository.create(
            name=name,
            email=email,
            password=password_hash,
            is_admin=False,
        )
        return user

    async def create_user_schema(
        self, name: str, email: str, password_hash: str
    ) -> UserResponseSchema:
        """Create a new user account and return an explicit response schema."""
        user = await self.create_user(name, email, password_hash)
        return UserResponseSchema.from_model(user)
