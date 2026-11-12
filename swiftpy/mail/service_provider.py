from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider
from swiftpy.mail.manager import MailManager


class MailServiceProvider(ServiceProvider):
    """
    Registers the MailManager singleton into the application DI container.
    """

    def register(self) -> None:
        self.container.singleton(MailManager)

    def boot(self) -> None:
        pass


__all__ = ["MailServiceProvider"]
