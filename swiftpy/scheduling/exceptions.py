from __future__ import annotations


class ScheduleError(Exception):
    """Base exception for all SwiftPY scheduling subsystem errors."""


class InvalidScheduleError(ScheduleError, ValueError):
    """Raised when a schedule definition, datetime, interval, or target is invalid."""


class InvalidCronError(InvalidScheduleError):
    """Raised when a cron expression is syntactically or semantically invalid."""


class InvalidTimezoneError(InvalidScheduleError):
    """Raised when a configured or requested IANA timezone cannot be resolved."""


class DuplicateScheduleError(ScheduleError):
    """Raised when registering a schedule with a name or ID that already exists."""


class ScheduleNotFoundError(ScheduleError):
    """Raised when referencing a schedule or occurrence ID that does not exist."""


class ScheduleSignatureError(ScheduleError):
    """Raised when a persisted schedule's execution fields fail HMAC verification."""


__all__ = [
    "DuplicateScheduleError",
    "InvalidCronError",
    "InvalidScheduleError",
    "InvalidTimezoneError",
    "ScheduleError",
    "ScheduleNotFoundError",
    "ScheduleSignatureError",
]
