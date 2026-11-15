from __future__ import annotations

import asyncio
import base64
import logging
import ssl
from collections.abc import Callable
from typing import Any, Protocol

from swiftpy.mail.envelope import Envelope


class _AiosmtplibModule(Protocol):
    @property
    def SMTP(self) -> Callable[..., Any]: ...


aiosmtplib: _AiosmtplibModule | None
_AioAuthError: type[Exception]
_AioConnectError: type[Exception]
_AioSMTPException: type[Exception]
_AioRecipientsRefused: type[Exception]
_AioTimeoutError: type[Exception]

try:
    import aiosmtplib as _aiosmtplib
    from aiosmtplib.errors import (
        SMTPAuthenticationError as _ImportedAioAuthError,
    )
    from aiosmtplib.errors import (
        SMTPConnectError as _ImportedAioConnectError,
    )
    from aiosmtplib.errors import (
        SMTPException as _ImportedAioSMTPException,
    )
    from aiosmtplib.errors import (
        SMTPRecipientsRefused as _ImportedAioRecipientsRefused,
    )
    from aiosmtplib.errors import (
        SMTPTimeoutError as _ImportedAioTimeoutError,
    )

    aiosmtplib = _aiosmtplib
    _AioAuthError = _ImportedAioAuthError
    _AioConnectError = _ImportedAioConnectError
    _AioSMTPException = _ImportedAioSMTPException
    _AioRecipientsRefused = _ImportedAioRecipientsRefused
    _AioTimeoutError = _ImportedAioTimeoutError
except ImportError:
    aiosmtplib = None
    _AioAuthError = Exception
    _AioConnectError = Exception
    _AioSMTPException = Exception
    _AioRecipientsRefused = Exception
    _AioTimeoutError = Exception
from swiftpy.mail.exceptions import (
    MailAuthenticationError,
    MailConnectionError,
    MailDeliveryError,
    MailTimeoutError,
    MailTransportError,
)
from swiftpy.mail.message import MailMessage, parse_mail_address
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.base import MailTransport

logger = logging.getLogger("swiftpy.mail.smtp")


class SMTPTransport(MailTransport):
    """
    Asynchronous SMTP mail transport supporting STARTTLS, implicit TLS, and authentication.
    Supports aiosmtplib when installed, or native asyncio RFC 5321 streaming.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        encryption: str | None = "starttls",
        timeout: float = 30.0,
        validate_certs: bool = True,
        default_from: tuple[str, str | None] | None = None,
        use_native: bool | None = None,
        max_attachment_size_bytes: int | None = None,
        max_message_size_bytes: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.encryption = (encryption or "").lower() if encryption else None
        self.timeout = timeout
        self.validate_certs = validate_certs
        self.default_from = default_from
        self.use_native = use_native if use_native is not None else (aiosmtplib is None)
        self.max_attachment_size_bytes = max_attachment_size_bytes
        self.max_message_size_bytes = max_message_size_bytes

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        default_from: tuple[str, str | None] | None = None,
        max_attachment_size_bytes: int | None = None,
        max_message_size_bytes: int | None = None,
    ) -> SMTPTransport:
        raw_att = config.get("max_attachment_size_bytes", max_attachment_size_bytes)
        raw_msg = config.get("max_message_size_bytes", max_message_size_bytes)
        return cls(
            host=str(config.get("host", "127.0.0.1")),
            port=int(config.get("port", 587)),
            username=config.get("username"),
            password=config.get("password"),
            encryption=config.get("encryption", "starttls"),
            timeout=float(config.get("timeout", 30.0)),
            validate_certs=bool(config.get("validate_certs", True)),
            default_from=default_from,
            use_native=config.get("use_native"),
            max_attachment_size_bytes=int(raw_att) if raw_att is not None else None,
            max_message_size_bytes=int(raw_msg) if raw_msg is not None else None,
        )

    def _create_ssl_context(self) -> ssl.SSLContext:
        """Create a secure SSL context for TLS/STARTTLS verification."""
        if self.validate_certs:
            context = ssl.create_default_context()
        else:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def send(self, message: MailMessage) -> MailSendResult:
        if self.use_native or aiosmtplib is None:
            return await self._send_native(message)

        return await self._send_aiosmtplib(message)

    async def _send_aiosmtplib(self, message: MailMessage) -> MailSendResult:
        assert aiosmtplib is not None

        email_msg = await Envelope.build(
            message,
            default_from=self.default_from,
            max_message_size_bytes=self.max_message_size_bytes,
            max_attachment_size_bytes=self.max_attachment_size_bytes,
        )

        use_tls = self.encryption in ("tls", "ssl")
        start_tls = self.encryption == "starttls"

        tls_context = self._create_ssl_context() if (use_tls or start_tls) else None

        client = aiosmtplib.SMTP(
            hostname=self.host,
            port=self.port,
            use_tls=use_tls,
            start_tls=start_tls,
            tls_context=tls_context,
            timeout=self.timeout,
        )

        all_recipients = message.all_recipient_addresses()
        from_header = str(email_msg["From"])
        _, sender_addr = parse_mail_address(from_header, field_name="from_address")

        try:
            async with client:
                if self.username and self.password:
                    await client.login(self.username, self.password)

                resp = await client.send_message(
                    email_msg,
                    sender=sender_addr,
                    recipients=all_recipients,
                )

                rejected: list[str] = []
                accepted: list[str] = list(all_recipients)
                message_id = str(email_msg.get("Message-ID", ""))

                if (
                    isinstance(resp, tuple)
                    and len(resp) >= 2
                    and isinstance(resp[0], dict)
                ):
                    errors_dict, status_msg = resp[0], resp[1]
                    for rec in errors_dict:
                        rejected.append(rec)
                        if rec in accepted:
                            accepted.remove(rec)
                    status_text = status_msg
                else:
                    status_text = str(resp)

                if not accepted and rejected:
                    raise MailDeliveryError(
                        f"All recipients were refused by SMTP server: {rejected}"
                    )

                return MailSendResult(
                    success=True,
                    message_id=message_id,
                    provider="smtp",
                    accepted=accepted,
                    rejected=rejected,
                    raw_response=status_text,
                )

        except _AioAuthError as exc:
            raise MailAuthenticationError(f"SMTP authentication failed: {exc}") from exc
        except _AioTimeoutError as exc:
            raise MailTimeoutError(
                f"SMTP connection/transmission timed out: {exc}"
            ) from exc
        except _AioConnectError as exc:
            raise MailConnectionError(
                f"Failed to connect to SMTP server {self.host}:{self.port}: {exc}"
            ) from exc
        except _AioRecipientsRefused as exc:
            raise MailDeliveryError(f"SMTP server refused recipients: {exc}") from exc
        except _AioSMTPException as exc:
            raise MailTransportError(f"SMTP delivery error: {exc}") from exc
        except Exception as exc:
            if isinstance(
                exc,
                (
                    MailAuthenticationError,
                    MailConnectionError,
                    MailTimeoutError,
                    MailDeliveryError,
                    MailTransportError,
                ),
            ):
                raise
            raise MailTransportError(
                f"Unexpected error during SMTP delivery: {exc}"
            ) from exc

    async def _send_native(self, message: MailMessage) -> MailSendResult:
        """Pure-Python asynchronous RFC 5321 SMTP implementation using asyncio streams."""
        email_msg = await Envelope.build(
            message,
            default_from=self.default_from,
            max_message_size_bytes=self.max_message_size_bytes,
            max_attachment_size_bytes=self.max_attachment_size_bytes,
        )
        all_recipients = message.all_recipient_addresses()
        from_header = str(email_msg["From"])
        _, sender_addr = parse_mail_address(from_header, field_name="from_address")

        use_tls = self.encryption in ("tls", "ssl")
        start_tls = self.encryption == "starttls"
        ssl_ctx = self._create_ssl_context() if (use_tls or start_tls) else None

        try:
            reader, current_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host,
                    self.port,
                    ssl=ssl_ctx if use_tls else None,
                ),
                timeout=self.timeout,
            )
            _kept_plain_writer = current_writer
        except TimeoutError as exc:
            raise MailTimeoutError(
                f"Connection to SMTP server {self.host}:{self.port} timed out"
            ) from exc
        except (ConnectionRefusedError, OSError) as exc:
            raise MailConnectionError(
                f"Failed to connect to SMTP server {self.host}:{self.port}: {exc}"
            ) from exc

        async def _read_reply() -> tuple[int, str]:
            lines: list[str] = []
            while True:
                line_bytes = await asyncio.wait_for(
                    reader.readline(), timeout=self.timeout
                )
                if not line_bytes:
                    raise MailConnectionError(
                        "SMTP server closed connection unexpectedly"
                    )
                line_str = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                lines.append(line_str)
                if len(line_str) < 4 or line_str[3] != "-":
                    break
            try:
                code = int(lines[-1][:3])
            except ValueError:
                code = -1
            return code, "\n".join(lines)

        async def _send_cmd(cmd: str) -> tuple[int, str]:
            current_writer.write(f"{cmd}\r\n".encode())
            await current_writer.drain()
            return await _read_reply()

        try:
            # 1. Greeting
            greet_code, greet_msg = await _read_reply()
            if greet_code != 220:
                raise MailConnectionError(
                    f"SMTP greeting failed with code {greet_code}: {greet_msg}"
                )

            # 2. EHLO
            code, _ehlo_resp = await _send_cmd("EHLO swiftpy.local")
            if code != 250:
                code, helo_resp = await _send_cmd("HELO swiftpy.local")
                if code != 250:
                    raise MailTransportError(
                        f"EHLO/HELO rejected with code {code}: {helo_resp}"
                    )

            # 3. STARTTLS
            if start_tls and ssl_ctx is not None:
                code, stls_resp = await _send_cmd("STARTTLS")
                if code != 220:
                    raise MailTransportError(
                        f"STARTTLS rejected with code {code}: {stls_resp}"
                    )

                loop = asyncio.get_running_loop()
                protocol = current_writer.transport.get_protocol()
                new_transport = await loop.start_tls(
                    current_writer.transport,
                    protocol,
                    ssl_ctx,
                    server_hostname=self.host if self.validate_certs else None,
                )
                if not isinstance(new_transport, asyncio.WriteTransport):
                    raise MailTransportError(
                        "Stream transport unavailable after TLS upgrade"
                    )
                current_writer = asyncio.StreamWriter(
                    new_transport, protocol, reader, loop
                )

                code, ehlo_post_tls = await _send_cmd("EHLO swiftpy.local")
                if code != 250:
                    raise MailTransportError(
                        f"EHLO after STARTTLS rejected with code {code}: {ehlo_post_tls}"
                    )

            # 4. Authentication
            if self.username and self.password:
                auth_str = f"\0{self.username}\0{self.password}".encode()
                auth_b64 = base64.b64encode(auth_str).decode("ascii")
                code, auth_resp = await _send_cmd(f"AUTH PLAIN {auth_b64}")
                if code == 535 or (code >= 500 and code not in (500, 501, 502, 504)):
                    raise MailAuthenticationError(
                        f"SMTP authentication failed ({code}): {auth_resp}"
                    )
                if code != 235:
                    # Fallback to AUTH LOGIN
                    code, _ = await _send_cmd("AUTH LOGIN")
                    if code == 334:
                        u_b64 = base64.b64encode(self.username.encode("utf-8")).decode(
                            "ascii"
                        )
                        p_b64 = base64.b64encode(self.password.encode("utf-8")).decode(
                            "ascii"
                        )
                        await _send_cmd(u_b64)
                        code, auth_resp = await _send_cmd(p_b64)
                    if code != 235:
                        raise MailAuthenticationError(
                            f"SMTP authentication failed ({code}): {auth_resp}"
                        )

            # 5. MAIL FROM
            from_code, from_resp = await _send_cmd(f"MAIL FROM:<{sender_addr}>")
            if from_code not in (250, 200):
                if from_code in (450, 451, 550, 554):
                    raise MailDeliveryError(
                        f"Sender refused ({from_code}): {from_resp}"
                    )
                raise MailTransportError(
                    f"MAIL FROM refused ({from_code}): {from_resp}"
                )

            # 6. RCPT TO
            accepted: list[str] = []
            rejected: list[str] = []
            for rcpt in all_recipients:
                rcpt_code, _rcpt_resp = await _send_cmd(f"RCPT TO:<{rcpt}>")
                if rcpt_code in (250, 251):
                    accepted.append(rcpt)
                else:
                    rejected.append(rcpt)

            if not accepted:
                raise MailDeliveryError(
                    f"All recipients were refused by SMTP server: {rejected}"
                )

            # 7. DATA
            data_code, data_resp = await _send_cmd("DATA")
            if data_code != 354:
                if data_code in (450, 451, 550, 554):
                    raise MailDeliveryError(
                        f"DATA command refused ({data_code}): {data_resp}"
                    )
                raise MailTransportError(
                    f"DATA command rejected ({data_code}): {data_resp}"
                )

            # 8. Transmit message with RFC 5321 dot-stuffing
            raw_bytes = email_msg.as_bytes()
            normalized_bytes = raw_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            if normalized_bytes.endswith(b"\n"):
                normalized_bytes = normalized_bytes[:-1]
            stuffed_lines = [
                b"." + line if line.startswith(b".") else line
                for line in normalized_bytes.split(b"\n")
            ]
            body_payload = b"\r\n".join(stuffed_lines) + b"\r\n.\r\n"
            current_writer.write(body_payload)
            await current_writer.drain()

            msg_code, msg_resp = await _read_reply()
            if msg_code not in (250, 200):
                if msg_code in (450, 451, 550, 554):
                    raise MailDeliveryError(
                        f"Message delivery refused ({msg_code}): {msg_resp}"
                    )
                raise MailTransportError(
                    f"Message delivery rejected ({msg_code}): {msg_resp}"
                )

            # 9. QUIT
            try:
                await _send_cmd("QUIT")
            except (OSError, TimeoutError):
                logger.debug(
                    "Failed to cleanly send QUIT to SMTP server", exc_info=True
                )

            return MailSendResult(
                success=True,
                message_id=str(email_msg.get("Message-ID", "")),
                provider="smtp",
                accepted=accepted,
                rejected=rejected,
                raw_response=msg_resp,
            )

        except TimeoutError as exc:
            raise MailTimeoutError(f"SMTP transmission timed out: {exc}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"SMTP connection or TLS error: {exc}") from exc
        finally:
            try:
                current_writer.close()
                await current_writer.wait_closed()
            except (OSError, TimeoutError):
                logger.debug("Error while closing SMTP client writer", exc_info=True)
            del _kept_plain_writer


__all__ = ["SMTPTransport"]
