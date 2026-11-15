from __future__ import annotations

from app.models.user import User
from app.repositories.user_repository import UserRepository


class UserService:
    """
    Domain business logic layer for user entity management.
    Coordinates between repositories, transactions, events, and background notifications.
    """

    def __init__(self, repository: UserRepository | None = None) -> None:
        self.repository = repository or UserRepository()

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

