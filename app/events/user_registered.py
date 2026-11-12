from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class UserRegistered:
    """
    Domain event triggered when a new user registers an account.
    """

    user_id: int
    email: str
    name: str

