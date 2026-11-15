from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from swiftpy.core.config import Config
from swiftpy.core.container import get_container
from swiftpy.mail.config import MailConfig
from swiftpy.mail.exceptions import (
    AttachmentTooLargeError,
    MailConfigurationError,
    MailerNotFoundError,
)
from swiftpy.mail.message import MailMessage, _validate_no_crlf, parse_mail_address
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.api.mailgun import MailgunTransport
from swiftpy.mail.transports.api.resend import ResendTransport
from swiftpy.mail.transports.api.sendgrid import SendGridTransport
from swiftpy.mail.transports.base import MailTransport
from swiftpy.mail.transports.console import ConsoleTransport
from swiftpy.mail.transports.sendmail import SendmailTransport
from swiftpy.mail.transports.smtp import SMTPTransport


class MailManager:
    """
    Registry and factory for configured MailTransport instances.
    Integrates with SwiftPY's configuration repository and DI container.
    """

    _instance: MailManager | None = None

    def __init__(self, config: Config | None = None) -> None:
        if config is None:
            try:
                container = get_container()
                if container.has(Config):
                    self.config = container.resolve(Config)
                else:
                    self.config = Config.get_instance()
            except RuntimeError:
                self.config = Config.get_instance()
        else:
            self.config = config

        self._transports: dict[str, MailTransport] = {}
        self._custom_creators: dict[str, Callable[[dict[str, Any]], MailTransport]] = {}

    @classmethod
    def get_instance(cls) -> MailManager:
        if cls._instance is not None:
            return cls._instance

        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
        except RuntimeError:
            pass

        cls._instance = cls()
        return cls._instance

    @property
    def mail_config(self) -> MailConfig:
        raw_mail = self.config.get("mail", {})
        if isinstance(raw_mail, dict):
            return MailConfig.from_dict(raw_mail)
        return MailConfig()

    @property
    def default_mailer_name(self) -> str:
        return self.mail_config.default

    @property
    def default_from(self) -> tuple[str, str | None]:
        cfg = self.mail_config
        return cfg.from_address, cfg.from_name

    def extend(
        self,
        driver_name: str,
        factory: Callable[[dict[str, Any]], MailTransport],
    ) -> None:
        """Register a custom mail transport creator."""
        self._custom_creators[driver_name.lower()] = factory

    def mailer(self, name: str | None = None) -> MailTransport:
        """Resolve a mail transport instance by name, or return the default mailer."""
        mailer_name = name or self.default_mailer_name

        if mailer_name in self._transports:
            return self._transports[mailer_name]

        transport = self._resolve_transport(mailer_name)
        self._transports[mailer_name] = transport
        return transport

    def _resolve_transport(self, name: str) -> MailTransport:
        mailers = self.mail_config.mailers
        if name not in mailers:
            # Fallback check directly in config if not parsed into mailers dict
            raw_mailers = self.config.get("mail.mailers", {})
            if isinstance(raw_mailers, dict) and name in raw_mailers:
                mailer_config = raw_mailers[name]
            else:
                raise MailerNotFoundError(f"Mailer '{name}' is not configured.")
        else:
            mailer_config = mailers[name]

        if not isinstance(mailer_config, dict):
            raise MailConfigurationError(
                f"Configuration for mailer '{name}' must be a dictionary."
            )

        driver_type = str(mailer_config.get("transport", "")).lower()

        cfg = self.mail_config
        max_att_val = mailer_config.get(
            "max_attachment_size_bytes", cfg.max_attachment_size_bytes
        )
        max_msg_val = mailer_config.get(
            "max_message_size_bytes", cfg.max_message_size_bytes
        )
        max_att = int(max_att_val) if max_att_val is not None else None
        max_msg = int(max_msg_val) if max_msg_val is not None else None

        if driver_type in self._custom_creators:
            return self._custom_creators[driver_type](mailer_config)

        if driver_type == "smtp":
            return SMTPTransport.from_config(
                mailer_config,
                default_from=self.default_from,
                max_attachment_size_bytes=max_att,
                max_message_size_bytes=max_msg,
            )

        if driver_type in ("console", "log"):
            return ConsoleTransport.from_config(
                mailer_config,
                max_attachment_size_bytes=max_att,
                max_message_size_bytes=max_msg,
            )

        if driver_type == "sendmail":
            return SendmailTransport.from_config(
                mailer_config,
                default_from=self.default_from,
                max_attachment_size_bytes=max_att,
                max_message_size_bytes=max_msg,
            )

        if driver_type == "resend":
            return ResendTransport.from_config(
                mailer_config,
                default_from=self.default_from,
                max_attachment_size_bytes=max_att,
                max_message_size_bytes=max_msg,
            )

        if driver_type == "sendgrid":
            return SendGridTransport.from_config(
                mailer_config,
                default_from=self.default_from,
                max_attachment_size_bytes=max_att,
                max_message_size_bytes=max_msg,
            )

        if driver_type == "mailgun":
            return MailgunTransport.from_config(
                mailer_config,
                default_from=self.default_from,
                max_attachment_size_bytes=max_att,
                max_message_size_bytes=max_msg,
            )

        raise MailConfigurationError(f"Unsupported mail transport: '{driver_type}'")

    async def send(
        self,
        message: MailMessage,
        mailer: str | None = None,
    ) -> MailSendResult:
        """Deliver an email message using the specified or default mailer."""
        cfg = self.mail_config
        default_addr, default_name = cfg.from_address, cfg.from_name
        if default_addr:
            parse_mail_address(default_addr, field_name="default_from")
        if default_name:
            _validate_no_crlf(default_name, "default_from_name")

        outbound_from_addr = message.from_address or default_addr or None
        outbound_from_name = (
            message.from_name
            if message.from_address
            else (message.from_name or default_name)
        )
        outbound = replace(
            message,
            to=list(message.to),
            cc=list(message.cc),
            bcc=list(message.bcc),
            from_address=outbound_from_addr,
            from_name=outbound_from_name,
            context=dict(message.context),
            attachments=list(message.attachments),
            headers=dict(message.headers),
            tags=list(message.tags),
            metadata=dict(message.metadata),
        )

        # Enforce upfront limits for known sizes across all transports
        total_known = len((outbound.text_body or "").encode("utf-8")) + len(
            (outbound.html_body or "").encode("utf-8")
        )
        for att in outbound.attachments:
            if att.size is not None:
                if att.size > cfg.max_attachment_size_bytes:
                    raise AttachmentTooLargeError(
                        f"Attachment '{att.filename}' ({att.size} bytes) exceeds size limit ({cfg.max_attachment_size_bytes} bytes)"
                    )
                total_known += att.size
        if total_known > cfg.max_message_size_bytes:
            raise AttachmentTooLargeError(
                f"Total email size ({total_known} bytes) exceeds configured limit ({cfg.max_message_size_bytes} bytes)"
            )

        transport = self.mailer(mailer)
        return await transport.send(outbound)

    async def close(self) -> None:
        """Close and clean up all instantiated mail transports."""
        for transport in self._transports.values():
            await transport.close()
        self._transports.clear()


__all__ = ["MailManager"]
