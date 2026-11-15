from __future__ import annotations

import pytest

from swiftpy.mail.message import MailMessage
from swiftpy.mail.transports.console import ConsoleTransport


@pytest.mark.asyncio
async def test_console_transport_send() -> None:
    transport = ConsoleTransport(channel="test")
    msg = MailMessage(
        subject="Testing Console",
        to=["alice@example.com"],
        text_body="Hello Console!",
    )
    result = await transport.send(msg)
    assert result.success is True
    assert result.provider == "console"
    assert result.accepted == ["alice@example.com"]
    assert len(transport.sent_messages) == 1
    assert transport.sent_messages[0].subject == "Testing Console"

    await transport.close()
    assert len(transport.sent_messages) == 0
