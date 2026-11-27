from __future__ import annotations

from datetime import datetime

from swiftpy.database.orm import HasOne, Model


class User(Model):
    """
    Default User ORM model representing application accounts.
    Maps to the `users` database table.
    """

    __table__ = "users"
    __primary_key__ = "id"
    __fillable__ = ["name", "email", "password", "is_admin"]
    __hidden__ = ["password", "remember_token"]

    id: int
    name: str
    email: str
    password: str
    is_admin: bool
    remember_token: str | None = None
    created_at: datetime
    updated_at: datetime

    profile = HasOne("Profile", foreign_key="user_id")
