from __future__ import annotations

import logging
import uuid
from typing import Any

from swiftpy.mail.exceptions import AttachmentTooLargeError
from swiftpy.mail.message import MailMessage
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.base import MailTransport


class ConsoleTransport(MailTransport):
    """
    Development and logging transport.
    Outputs email metadata to the logger and retains sent messages in memory for inspection.
    """

    def __init__(
        self,
        channel: str = "mail",
        *,
        max_attachment_size_bytes: int | None = None,
        max_message_size_bytes: int | None = None,
    ) -> None:
        self.channel = channel
        self.logger = logging.getLogger(f"swiftpy.mail.{channel}")
        self.sent_messages: list[MailMessage] = []
        self.max_attachment_size_bytes = max_attachment_size_bytes
        self.max_message_size_bytes = max_message_size_bytes

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        max_attachment_size_bytes: int | None = None,
        max_message_size_bytes: int | None = None,
    ) -> ConsoleTransport:
        channel = str(config.get("channel", "mail"))
        raw_att = config.get("max_attachment_size_bytes", max_attachment_size_bytes)
        raw_msg = config.get("max_message_size_bytes", max_message_size_bytes)
        return cls(
            channel=channel,
            max_attachment_size_bytes=int(raw_att) if raw_att is not None else None,
            max_message_size_bytes=int(raw_msg) if raw_msg is not None else None,
        )

    async def send(self, message: MailMessage) -> MailSendResult:
        if (
            self.max_attachment_size_bytes is not None
            or self.max_message_size_bytes is not None
        ):
            total_bytes = len((message.text_body or "").encode("utf-8")) + len(
                (message.html_body or "").encode("utf-8")
            )
            for att in message.attachments:
                att_data = await att.read_bytes(
                    max_size=self.max_attachment_size_bytes
                )
                total_bytes += len(att_data)
            if (
                self.max_message_size_bytes is not None
                and total_bytes > self.max_message_size_bytes
            ):
                raise AttachmentTooLargeError(
                    f"Total email size ({total_bytes} bytes) exceeds configured limit ({self.max_message_size_bytes} bytes)"
                )

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
            accepted=message.all_recipient_addresses(),
            rejected=[],
        )

    async def close(self) -> None:
        self.sent_messages.clear()


__all__ = ["ConsoleTransport"]
