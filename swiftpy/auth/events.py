from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from swiftpy.events.dispatcher import EventDispatcher


@dataclass(slots=True)
class Attempting:
    """Dispatched before user credentials are verified."""

    guard: str
    credentials: dict[str, Any] = field(default_factory=dict)
    remember: bool = False


@dataclass(slots=True)
class Authenticated:
    """Dispatched once a user has been authenticated for the current request or session."""

    guard: str
    user: Any


@dataclass(slots=True)
class Login:
    """Dispatched when a user logs in persistently (not dispatched for ``once``)."""

    guard: str
    user: Any
    remember: bool = False


@dataclass(slots=True)
class Failed:
    """Dispatched when credential verification or ``attempt_when`` fails."""

    guard: str
    user: Any | None = None
    credentials: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Logout:
    """Dispatched when an authenticated user logs out."""

    guard: str
    user: Any | None = None


@dataclass(slots=True)
class Lockout:
    """Dispatched when login throttling blocks an authentication attempt."""

    identifier: str
    ip: str
    max_attempts: int
    retry_after: int
    guard: str = "jwt"


_EVENT_NAME_MAP: dict[type[Any], str] = {
    Attempting: "auth.attempting",
    Authenticated: "auth.authenticated",
    Login: "auth.login",
    Failed: "auth.failed",
    Logout: "auth.logout",
    Lockout: "auth.lockout",
}


async def dispatch_auth_event(event: Any) -> None:
    """Dispatch both the typed auth event and its dotted ``auth.<event>`` string alias."""
    dispatcher = EventDispatcher.get_instance()
    await dispatcher.dispatch(event)
    alias = _EVENT_NAME_MAP.get(type(event))
    if alias is not None and dispatcher.has_listeners(alias):
        await dispatcher.dispatch(alias, event)


__all__ = [
    "Attempting",
    "Authenticated",
    "Failed",
    "Lockout",
    "Login",
    "Logout",
    "dispatch_auth_event",
]
