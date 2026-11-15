from __future__ import annotations

from app.models.user import User


class UserRepository:
    """
    Data access repository for User entities.
    Encapsulates database operations and keeps query logic decoupled from business services.
    """

    async def find_by_id(self, user_id: int) -> User | None:
        """Find a single user by primary key ID."""
        return await User.find(user_id)

    async def find_by_email(self, email: str) -> User | None:
        """Find a single user by email address."""
        return await User.where("email", email).first()

    async def create(self, **attributes: object) -> User:
        """Persist a new User model instance."""
        return await User.create(**attributes)

