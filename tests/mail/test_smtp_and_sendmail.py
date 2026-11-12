from __future__ import annotations

import asyncio
import email
import email.policy
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swiftpy.mail.attachments import Attachment
from swiftpy.mail.exceptions import (
    MailAuthenticationError,
    MailConfigurationError,
    MailConnectionError,
    MailDeliveryError,
    MailTimeoutError,
    MailTransportError,
)
from swiftpy.mail.message import MailMessage
from swiftpy.mail.transports.sendmail import SendmailTransport
from swiftpy.mail.transports.smtp import SMTPTransport

logger = logging.getLogger("tests.mail.smtp_server")


class TestSMTPServer:
    """
    Lightweight, programmable async SMTP server for socket-level integration testing.
    Runs on 127.0.0.1 on an ephemeral port.
    """

    __test__ = False

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 0
        self.received_messages: list[dict[str, Any]] = []

        # Programmable failure triggers
        self.auth_success: bool = True
        self.reject_recipients: set[str] = set()
        self.reject_all_recipients: bool = False
        self.mail_from_error_code: int | None = None
        self.data_error_code: int | None = None
        self.delivery_error_code: int | None = None
        self.disconnect_on_connect: bool = False
        self.disconnect_on_mail_from: bool = False
        self.disconnect_on_data: bool = False
        self.greeting_delay: float = 0.0
        self.command_delay: float = 0.0

        self._active_writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle_client,
            self.host,
            0,
        )
        self.port = self.server.sockets[0].getsockname()[1]
        logger.info("TestSMTPServer started on %s:%d", self.host, self.port)

    async def close(self) -> None:
        for w in list(self._active_writers):
            try:
                w.close()
                await w.wait_closed()
            except (OSError, TimeoutError):
                pass
        self._active_writers.clear()

        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._active_writers.add(writer)
        envelope_from: str | None = None
        envelope_to: list[str] = []

        try:
            if self.disconnect_on_connect:
                writer.close()
                await writer.wait_closed()
                return

            if self.greeting_delay > 0:
                await asyncio.sleep(self.greeting_delay)

            # Send 220 banner greeting
            writer.write(b"220 swiftpy.test.smtp Service Ready\r\n")
            await writer.drain()

            while True:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if self.command_delay > 0:
                    await asyncio.sleep(self.command_delay)

                upper_line = line.upper()

                if upper_line.startswith(("EHLO", "HELO")):
                    resp = (
                        b"250-swiftpy.test.smtp\r\n"
                        b"250-AUTH PLAIN LOGIN\r\n"
                        b"250-8BITMIME\r\n"
                        b"250-STARTTLS\r\n"
                        b"250 OK\r\n"
                    )
                    writer.write(resp)
                    await writer.drain()

                elif upper_line.startswith("STARTTLS"):
                    # Dummy STARTTLS acknowledgment
                    writer.write(b"220 2.0.0 Ready to start TLS\r\n")
                    await writer.drain()

                elif upper_line.startswith("AUTH PLAIN"):
                    if not self.auth_success:
                        writer.write(
                            b"535 5.7.8 Authentication credentials invalid\r\n"
                        )
                    else:
                        writer.write(b"235 2.7.0 Authentication successful\r\n")
                    await writer.drain()

                elif upper_line.startswith("AUTH LOGIN"):
                    if not self.auth_success:
                        writer.write(
                            b"535 5.7.8 Authentication credentials invalid\r\n"
                        )
                    else:
                        # Request username
                        writer.write(b"334 VXNlcm5hbWU6\r\n")
                        await writer.drain()
                        _ = await reader.readline()
                        # Request password
                        writer.write(b"334 UGFzc3dvcmQ6\r\n")
                        await writer.drain()
                        _ = await reader.readline()
                        writer.write(b"235 2.7.0 Authentication successful\r\n")
                    await writer.drain()

                elif upper_line.startswith("MAIL FROM:"):
                    if self.disconnect_on_mail_from:
                        writer.close()
                        await writer.wait_closed()
                        return

                    if self.mail_from_error_code is not None:
                        code = self.mail_from_error_code
                        writer.write(f"{code} Sender address rejected\r\n".encode())
                        await writer.drain()
                        continue

                    # Extract address between <...>
                    start = line.find("<")
                    end = line.find(">")
                    if start != -1 and end != -1:
                        envelope_from = line[start + 1 : end]
                    else:
                        envelope_from = line.split(":", 1)[1].strip()

                    envelope_to = []
                    writer.write(b"250 2.1.0 Sender OK\r\n")
                    await writer.drain()

                elif upper_line.startswith("RCPT TO:"):
                    start = line.find("<")
                    end = line.find(">")
                    if start != -1 and end != -1:
                        addr = line[start + 1 : end]
                    else:
                        addr = line.split(":", 1)[1].strip()

                    if self.reject_all_recipients or addr in self.reject_recipients:
                        writer.write(b"550 5.1.1 User unknown / recipient rejected\r\n")
                    else:
                        envelope_to.append(addr)
                        writer.write(b"250 2.1.5 Recipient OK\r\n")
                    await writer.drain()

                elif upper_line.startswith("DATA"):
                    if self.disconnect_on_data:
                        writer.close()
                        await writer.wait_closed()
                        return

                    if self.data_error_code is not None:
                        code = self.data_error_code
                        writer.write(f"{code} DATA command rejected\r\n".encode())
                        await writer.drain()
                        continue

                    writer.write(b"354 Start mail input; end with <CRLF>.<CRLF>\r\n")
                    await writer.drain()

                    data_lines: list[bytes] = []
                    while True:
                        msg_line = await reader.readline()
                        if not msg_line:
                            break
                        if msg_line in (b".\r\n", b".\n"):
                            break
                        # Handle dot un-stuffing
                        if msg_line.startswith(b".."):
                            msg_line = msg_line[1:]
                        data_lines.append(msg_line)

                    raw_data = b"".join(data_lines)

                    if self.delivery_error_code is not None:
                        code = self.delivery_error_code
                        writer.write(f"{code} Delivery processing failed\r\n".encode())
                        await writer.drain()
                    else:
                        self.received_messages.append(
                            {
                                "from": envelope_from,
                                "to": list(envelope_to),
                                "data": raw_data,
                            }
                        )
                        writer.write(b"250 2.0.0 OK message queued\r\n")
                        await writer.drain()

                elif upper_line.startswith("QUIT"):
                    writer.write(b"221 2.0.0 Service closing transmission\r\n")
                    await writer.drain()
                    break

                elif upper_line.startswith("RSET"):
                    envelope_from = None
                    envelope_to = []
                    writer.write(b"250 OK\r\n")
                    await writer.drain()

                else:
                    writer.write(b"500 Command unrecognized\r\n")
                    await writer.drain()

        except (OSError, TimeoutError, asyncio.CancelledError) as exc:
            logger.debug("SMTP client session ended with: %s", exc)
        finally:
            self._active_writers.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, TimeoutError):
                pass


# ---------------------------------------------------------------------------
# Real Socket-Level Async SMTP Integration Tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def smtp_server() -> AsyncGenerator[TestSMTPServer, None]:
    server = TestSMTPServer()
    await server.start()
    try:
        yield server
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_smtp_real_socket_plain_delivery(
    smtp_server: TestSMTPServer,
) -> None:
    transport = SMTPTransport(
        host=smtp_server.host,
        port=smtp_server.port,
        encryption=None,
        default_from=("noreply@swiftpy.org", "SwiftPY Mailer"),
        use_native=True,
    )

    msg = MailMessage(
        subject="Integration Test Email",
        to=["recipient@example.com"],
        cc=["audit@example.com"],
        text_body="Hello from real async socket SMTP!",
    )
    msg.attach(Attachment.from_bytes(b"report_content", filename="report.txt"))

    result = await transport.send(msg)

    assert result.success is True
    assert result.provider == "smtp"
    assert "recipient@example.com" in result.accepted
    assert "audit@example.com" in result.accepted
    assert result.rejected == []

    # Verify server received the envelope and data
    assert len(smtp_server.received_messages) == 1
    received = smtp_server.received_messages[0]
    assert received["from"] == "noreply@swiftpy.org"
    assert "recipient@example.com" in received["to"]
    assert "audit@example.com" in received["to"]

    # Re-parse raw MIME data received by the SMTP server
    parsed = email.message_from_bytes(received["data"], policy=email.policy.default)
    assert parsed["Subject"] == "Integration Test Email"
    body_part = parsed.get_body()
    assert body_part is not None
    assert "Hello from real async socket SMTP!" in body_part.get_content()

    att_parts = [
        p for p in parsed.walk() if p.get_content_disposition() == "attachment"
    ]
    assert len(att_parts) == 1
    assert att_parts[0].get_filename() == "report.txt"
    assert att_parts[0].get_payload(decode=True) == b"report_content"


@pytest.mark.asyncio
async def test_smtp_real_socket_auth_success(
    smtp_server: TestSMTPServer,
) -> None:
    smtp_server.auth_success = True

    transport = SMTPTransport(
        host=smtp_server.host,
        port=smtp_server.port,
        username="test_user",
        password="correct_password",
        encryption=None,
        use_native=True,
    )

    msg = MailMessage(
        subject="Auth Success",
        to=["user@example.com"],
        from_address="auth@example.com",
        text_body="Authenticated email",
    )

    result = await transport.send(msg)
    assert result.success is True
    assert len(smtp_server.received_messages) == 1


@pytest.mark.asyncio
async def test_smtp_real_socket_auth_failure(
    smtp_server: TestSMTPServer,
) -> None:
    smtp_server.auth_success = False

    transport = SMTPTransport(
        host=smtp_server.host,
        port=smtp_server.port,
        username="test_user",
        password="wrong_password",
        encryption=None,
        use_native=True,
    )

    msg = MailMessage(
        subject="Auth Failure",
        to=["user@example.com"],
        from_address="auth@example.com",
        text_body="Failed auth",
    )

    with pytest.raises(MailAuthenticationError, match="SMTP authentication failed"):
        await transport.send(msg)

    assert len(smtp_server.received_messages) == 0


@pytest.mark.asyncio
async def test_smtp_real_socket_partial_recipient_rejection(
    smtp_server: TestSMTPServer,
) -> None:
    smtp_server.reject_recipients = {"bad-address@example.com"}

    transport = SMTPTransport(
        host=smtp_server.host,
        port=smtp_server.port,
        encryption=None,
        use_native=True,
    )

    msg = MailMessage(
        subject="Partial Recipient Test",
        to=["good-address@example.com", "bad-address@example.com"],
        from_address="sender@example.com",
        text_body="Partial test",
    )

    result = await transport.send(msg)
    assert result.success is True
    assert result.accepted == ["good-address@example.com"]
    assert result.rejected == ["bad-address@example.com"]
    assert len(smtp_server.received_messages) == 1


@pytest.mark.asyncio
async def test_smtp_real_socket_all_recipients_rejected(
    smtp_server: TestSMTPServer,
) -> None:
    smtp_server.reject_all_recipients = True

    transport = SMTPTransport(
        host=smtp_server.host,
        port=smtp_server.port,
        encryption=None,
        use_native=True,
    )

    msg = MailMessage(
        subject="All Rejected",
        to=["user1@example.com", "user2@example.com"],
        from_address="sender@example.com",
        text_body="All rejected",
    )

    with pytest.raises(MailDeliveryError, match="All recipients were refused"):
        await transport.send(msg)

    assert len(smtp_server.received_messages) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_attr", "error_code"),
    [
        ("mail_from_error_code", 450),
        ("mail_from_error_code", 451),
        ("mail_from_error_code", 550),
        ("data_error_code", 450),
        ("data_error_code", 554),
        ("delivery_error_code", 451),
        ("delivery_error_code", 554),
    ],
)
async def test_smtp_real_socket_server_error_codes(
    smtp_server: TestSMTPServer,
    error_attr: str,
    error_code: int,
) -> None:
    setattr(smtp_server, error_attr, error_code)

    transport = SMTPTransport(
        host=smtp_server.host,
        port=smtp_server.port,
        encryption=None,
        use_native=True,
    )

    msg = MailMessage(
        subject="Server Error Test",
        to=["user@example.com"],
        from_address="sender@example.com",
        text_body="Error code test",
    )

    with pytest.raises((MailDeliveryError, MailTransportError)):
        await transport.send(msg)

    assert len(smtp_server.received_messages) == 0


@pytest.mark.asyncio
async def test_smtp_real_socket_greeting_timeout(
    smtp_server: TestSMTPServer,
) -> None:
    smtp_server.greeting_delay = 0.5

    transport = SMTPTransport(
        host=smtp_server.host,
        port=smtp_server.port,
        timeout=0.05,
        encryption=None,
        use_native=True,
    )

    msg = MailMessage(
        subject="Timeout",
        to=["user@example.com"],
        from_address="s@example.com",
        text_body="Timeout",
    )

    with pytest.raises(MailTimeoutError):
        await transport.send(msg)


@pytest.mark.asyncio
async def test_smtp_real_socket_command_timeout(
    smtp_server: TestSMTPServer,
) -> None:
    smtp_server.command_delay = 0.5

    transport = SMTPTransport(
        host=smtp_server.host,
        port=smtp_server.port,
        timeout=0.05,
        encryption=None,
        use_native=True,
    )

    msg = MailMessage(
        subject="Command Timeout",
        to=["user@example.com"],
        from_address="s@example.com",
        text_body="Timeout",
    )

    with pytest.raises(MailTimeoutError):
        await transport.send(msg)


@pytest.mark.asyncio
async def test_smtp_real_socket_disconnect_on_connect(
    smtp_server: TestSMTPServer,
) -> None:
    smtp_server.disconnect_on_connect = True

    transport = SMTPTransport(
        host=smtp_server.host,
        port=smtp_server.port,
        encryption=None,
        use_native=True,
    )

    msg = MailMessage(
        subject="Disconnect",
        to=["user@example.com"],
        from_address="s@example.com",
        text_body="Disconnect",
    )

    with pytest.raises(MailConnectionError):
        await transport.send(msg)


@pytest.mark.asyncio
async def test_smtp_real_socket_disconnect_on_mail_from(
    smtp_server: TestSMTPServer,
) -> None:
    smtp_server.disconnect_on_mail_from = True

    transport = SMTPTransport(
        host=smtp_server.host,
        port=smtp_server.port,
        encryption=None,
        use_native=True,
    )

    msg = MailMessage(
        subject="Disconnect",
        to=["user@example.com"],
        from_address="s@example.com",
        text_body="Disconnect",
    )

    with pytest.raises(MailConnectionError):
        await transport.send(msg)


@pytest.mark.asyncio
async def test_smtp_connection_refused() -> None:
    # Port 1 is not listening
    transport = SMTPTransport(
        host="127.0.0.1",
        port=1,
        timeout=1.0,
        encryption=None,
        use_native=True,
    )

    msg = MailMessage(
        subject="Refused",
        to=["user@example.com"],
        from_address="s@example.com",
        text_body="Refused",
    )

    with pytest.raises(MailConnectionError):
        await transport.send(msg)


@pytest.mark.asyncio
async def test_smtp_transport_aiosmtplib_path() -> None:
    mock_smtp_client = AsyncMock()
    mock_smtp_client.send_message = AsyncMock(return_value=({}, "250 OK"))
    mock_smtp_client.login = AsyncMock()
    mock_smtp_client.__aenter__ = AsyncMock(return_value=mock_smtp_client)
    mock_smtp_client.__aexit__ = AsyncMock(return_value=None)

    mock_aiosmtplib = MagicMock()
    mock_aiosmtplib.SMTP = MagicMock(return_value=mock_smtp_client)

    with patch("swiftpy.mail.transports.smtp.aiosmtplib", mock_aiosmtplib):
        transport = SMTPTransport(
            host="smtp.example.com",
            port=587,
            username="user",
            password="password",
            encryption="starttls",
            default_from=("noreply@example.com", "App"),
            use_native=False,
        )
        msg = MailMessage(
            subject="SMTP Test", to=["client@example.com"], text_body="Hi SMTP"
        )
        result = await transport.send(msg)

        assert result.success is True
        assert result.provider == "smtp"
        assert result.accepted == ["client@example.com"]
        assert result.rejected == []


# ---------------------------------------------------------------------------
# Sendmail Subprocess Hardening Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sendmail_transport_binary_missing() -> None:
    transport = SendmailTransport(path="/non/existent/sendmail")
    msg = MailMessage(subject="Test", to=["user@example.com"], from_address="a@b.com")
    with pytest.raises(MailConfigurationError, match="Sendmail binary not found"):
        await transport.send(msg)


@pytest.mark.asyncio
async def test_sendmail_transport_mocked_exec(tmp_path: Path) -> None:
    fake_sendmail = tmp_path / "sendmail"
    fake_sendmail.write_text("#!/bin/sh\nexit 0\n")
    fake_sendmail.chmod(0o755)

    transport = SendmailTransport(
        path=str(fake_sendmail), default_from=("a@b.com", "App")
    )
    msg = MailMessage(
        subject="Sendmail Test", to=["user@example.com"], text_body="Test Sendmail"
    )

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"Message accepted", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await transport.send(msg)
        assert result.success is True
        assert result.provider == "sendmail"
        assert result.accepted == ["user@example.com"]


@pytest.mark.asyncio
async def test_sendmail_transport_non_zero_exit_code(tmp_path: Path) -> None:
    fake_sendmail = tmp_path / "sendmail"
    fake_sendmail.write_text("#!/bin/sh\nexit 1\n")
    fake_sendmail.chmod(0o755)

    transport = SendmailTransport(
        path=str(fake_sendmail), default_from=("a@b.com", "App")
    )
    msg = MailMessage(
        subject="Sendmail Fail", to=["user@example.com"], text_body="Test"
    )

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b"", b"Recipient address rejected: User unknown")
    )
    mock_proc.returncode = 1

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(MailTransportError, match="Recipient address rejected"),
    ):
        await transport.send(msg)


@pytest.mark.asyncio
async def test_sendmail_transport_timeout_kills_process(tmp_path: Path) -> None:
    fake_sendmail = tmp_path / "sendmail"
    fake_sendmail.write_text("#!/bin/sh\nexit 0\n")
    fake_sendmail.chmod(0o755)

    transport = SendmailTransport(
        path=str(fake_sendmail),
        timeout=0.01,
        default_from=("a@b.com", "App"),
    )
    msg = MailMessage(
        subject="Sendmail Timeout", to=["user@example.com"], text_body="Test"
    )

    mock_proc = AsyncMock()

    async def _hang_communicate(*args: Any, **kwargs: Any) -> tuple[bytes, bytes]:
        await asyncio.sleep(0.5)
        return (b"", b"")

    mock_proc.communicate = _hang_communicate
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(MailTimeoutError, match="timed out"):
            await transport.send(msg)

        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_sendmail_transport_cancellation_kills_process(
    tmp_path: Path,
) -> None:
    fake_sendmail = tmp_path / "sendmail"
    fake_sendmail.write_text("#!/bin/sh\nexit 0\n")
    fake_sendmail.chmod(0o755)

    transport = SendmailTransport(
        path=str(fake_sendmail),
        timeout=10.0,
        default_from=("a@b.com", "App"),
    )
    msg = MailMessage(
        subject="Sendmail Cancel", to=["user@example.com"], text_body="Test"
    )

    mock_proc = AsyncMock()

    async def _long_communicate(*args: Any, **kwargs: Any) -> tuple[bytes, bytes]:
        await asyncio.sleep(5.0)
        return (b"", b"")

    mock_proc.communicate = _long_communicate
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        task = asyncio.create_task(transport.send(msg))
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()
