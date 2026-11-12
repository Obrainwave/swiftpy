from __future__ import annotations

from abc import ABC, abstractmethod

from swiftpy.mail.message import MailMessage
from swiftpy.mail.models import MailSendResult


class MailTransport(ABC):
    """Abstract contract for SwiftPY email delivery backends."""

    @abstractmethod
    async def send(self, message: MailMessage) -> MailSendResult:
        """
        Deliver the given MailMessage.

        Raises MailTransportError or specialized subclasses on network/auth failure.
        """

    async def close(self) -> None:
        """Release underlying connections, pools, or open handles."""


__all__ = ["MailTransport"]
