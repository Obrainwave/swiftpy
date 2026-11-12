from __future__ import annotations

from collections.abc import Callable
from typing import Any

from swiftpy.core.config import Config
from swiftpy.core.container import get_container
from swiftpy.mail.config import MailConfig
from swiftpy.mail.exceptions import MailConfigurationError, MailerNotFoundError
from swiftpy.mail.message import MailMessage
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
        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
        except RuntimeError:
            pass

        if cls._instance is None:
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

        if driver_type in self._custom_creators:
            return self._custom_creators[driver_type](mailer_config)

        if driver_type == "smtp":
            return SMTPTransport.from_config(
                mailer_config, default_from=self.default_from
            )

        if driver_type in ("console", "log"):
            return ConsoleTransport.from_config(mailer_config)

        if driver_type == "sendmail":
            return SendmailTransport.from_config(
                mailer_config, default_from=self.default_from
            )

        if driver_type == "resend":
            return ResendTransport.from_config(
                mailer_config, default_from=self.default_from
            )

        if driver_type == "sendgrid":
            return SendGridTransport.from_config(
                mailer_config, default_from=self.default_from
            )

        if driver_type == "mailgun":
            return MailgunTransport.from_config(
                mailer_config, default_from=self.default_from
            )

        raise MailConfigurationError(f"Unsupported mail transport: '{driver_type}'")

    async def send(
        self,
        message: MailMessage,
        mailer: str | None = None,
    ) -> MailSendResult:
        """Deliver an email message using the specified or default mailer."""
        if not message.from_address:
            addr, name = self.default_from
            message.from_address = addr
            if not message.from_name:
                message.from_name = name

        transport = self.mailer(mailer)
        return await transport.send(message)

    async def close(self) -> None:
        """Close and clean up all instantiated mail transports."""
        for transport in self._transports.values():
            await transport.close()
        self._transports.clear()


__all__ = ["MailManager"]
