from __future__ import annotations

import logging
import uuid
from typing import Any

from swiftpy.mail.message import MailMessage
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.base import MailTransport


class ConsoleTransport(MailTransport):
    """
    Development and logging transport.
    Outputs email metadata to the logger and retains sent messages in memory for inspection.
    """

    def __init__(self, channel: str = "mail") -> None:
        self.channel = channel
        self.logger = logging.getLogger(f"swiftpy.mail.{channel}")
        self.sent_messages: list[MailMessage] = []

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ConsoleTransport:
        channel = str(config.get("channel", "mail"))
        return cls(channel=channel)

    async def send(self, message: MailMessage) -> MailSendResult:
        self.sent_messages.append(message)
        msg_id = f"console-{uuid.uuid4().hex[:16]}"

        self.logger.info(
            "Email dispatched via ConsoleTransport: To=%s Subject='%s' Message-ID=%s",
            message.to,
            message.subject,
            msg_id,
        )

        return MailSendResult(
            success=True,
            message_id=msg_id,
            provider="console",
            accepted=list(message.to),
            rejected=[],
        )

    async def close(self) -> None:
        self.sent_messages.clear()


__all__ = ["ConsoleTransport"]
