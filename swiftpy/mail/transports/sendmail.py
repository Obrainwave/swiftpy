from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from swiftpy.mail.envelope import Envelope
from swiftpy.mail.exceptions import (
    MailConfigurationError,
    MailTimeoutError,
    MailTransportError,
)
from swiftpy.mail.message import MailMessage
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.base import MailTransport

logger = logging.getLogger("swiftpy.mail.sendmail")


class SendmailTransport(MailTransport):
    """
    Local sendmail binary transport.
    Invokes sendmail non-blockingly via asyncio.create_subprocess_exec.
    Enforces process timeouts and cancellation cleanup.
    """

    def __init__(
        self,
        path: str = "/usr/sbin/sendmail",
        timeout: float = 30.0,
        default_from: tuple[str, str | None] | None = None,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.default_from = default_from

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        default_from: tuple[str, str | None] | None = None,
    ) -> SendmailTransport:
        path = str(config.get("path", "/usr/sbin/sendmail"))
        timeout = float(config.get("timeout", 30.0))
        return cls(path=path, timeout=timeout, default_from=default_from)

    async def send(self, message: MailMessage) -> MailSendResult:
        if not Path(self.path).exists():
            raise MailConfigurationError(f"Sendmail binary not found at '{self.path}'")

        email_msg = await Envelope.build(message, default_from=self.default_from)
        raw_bytes = email_msg.as_bytes()

        all_recipients = message.all_recipient_addresses()
        cmd = [self.path, "-t", "-i"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=raw_bytes),
                timeout=self.timeout,
            )
        except TimeoutError as exc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                logger.debug("Failed to kill timed-out sendmail process", exc_info=True)
            raise MailTimeoutError(
                f"Sendmail process timed out after {self.timeout}s"
            ) from exc
        except asyncio.CancelledError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                logger.debug("Failed to kill cancelled sendmail process", exc_info=True)
            raise

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            raise MailTransportError(
                f"Sendmail process exited with code {proc.returncode}: {err_text}"
            )

        return MailSendResult(
            success=True,
            message_id=str(email_msg.get("Message-ID", "")),
            provider="sendmail",
            accepted=all_recipients,
            rejected=[],
            raw_response=stdout.decode("utf-8", errors="replace"),
        )


__all__ = ["SendmailTransport"]
