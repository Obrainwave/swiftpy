from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MailSendResult:
    """Normalized delivery outcome returned by all mail transports."""

    success: bool
    message_id: str | None = None
    provider: str | None = None
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    raw_response: Any = None


__all__ = ["MailSendResult"]
