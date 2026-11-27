from __future__ import annotations

from app.models.user import User


class UserRepository:
    """
    Data access repository for User entities.
    Encapsulates database operations and keeps query logic decoupled from business services.
    """

    def __init__(self) -> None:
        self.model = User

    async def get_all(self) -> list[User]:
        return await self.model.with_('profile').get()

    async def find_by_id(self, user_id: int) -> User | None:
        """Find a single user by primary key ID."""
        return await User.where("id", user_id).with_('profile').first()

    async def find_by_email(self, email: str) -> User | None:
        """Find a single user by email address."""
        return await User.where("email", email).with_('profile').first()

    async def create(self, **attributes: object) -> User:
        """Persist a new User model instance."""
        return await User.create(**attributes)

