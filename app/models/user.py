from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from swiftpy.database.orm import Model


class User(Model):
    """
    Default User ORM model representing application accounts.
    Maps to the `users` database table.
    """

    __table__ = "users"
    __primary_key__ = "id"
    __fillable__: ClassVar[list[str]] = ["name", "email", "password", "is_admin"]
    __hidden__: ClassVar[list[str]] = ["password"]

    id: int
    name: str
    email: str
    password: str
    is_admin: bool
    created_at: datetime
    updated_at: datetime
