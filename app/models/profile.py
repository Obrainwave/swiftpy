from __future__ import annotations

from datetime import datetime

from swiftpy.database.orm import BelongsTo, Model


class Profile(Model):
    """Profile model."""

    __table__ = "profiles"
    __fillable__ = ["user_id", "username"]

    id: int
    user_id: int
    username: str
    created_at: datetime
    updated_at: datetime

    user = BelongsTo("User", foreign_key="user_id")
