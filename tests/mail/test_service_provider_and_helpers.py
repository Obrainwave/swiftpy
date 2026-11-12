from __future__ import annotations

import pytest

from swiftpy.core.container import Container, set_container
from swiftpy.helpers.helper import mail
from swiftpy.mail.facade import Mail
from swiftpy.mail.manager import MailManager
from swiftpy.mail.message import MailMessage
from swiftpy.mail.service_provider import MailServiceProvider
from swiftpy.mail.transports.console import ConsoleTransport


def test_mail_service_provider_registers_singleton() -> None:
    container = Container()
    set_container(container)

    provider = MailServiceProvider(container)
    provider.register()
    provider.boot()

    assert container.has(MailManager)
    manager = container.resolve(MailManager)
    assert isinstance(manager, MailManager)

    # Calling resolve again returns the exact same singleton
    manager2 = container.resolve(MailManager)
    assert manager is manager2


def test_mail_helper_function() -> None:
    container = Container()
    set_container(container)

    provider = MailServiceProvider(container)
    provider.register()

    # Call mail() helper with no args returns MailManager
    manager = mail()
    assert isinstance(manager, MailManager)

    # Call mail("console") returns specific transport
    transport = mail("console")
    assert isinstance(transport, ConsoleTransport)


@pytest.mark.asyncio
async def test_mail_facade_unfaked_send() -> None:
    # Test that Mail facade delegates to MailManager when not faked
    msg = MailMessage(
        subject="Direct Unfaked Test",
        to=["direct@example.com"],
        text_body="Content",
    )
    result = await Mail.send(msg, mailer="console")
    assert result.success is True
    assert result.provider == "console"
