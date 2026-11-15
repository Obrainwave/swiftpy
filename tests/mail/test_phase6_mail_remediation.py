from __future__ import annotations

import asyncio
import email
import email.policy
import io
import json
import ssl
import subprocess
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.mail.welcome_email import WelcomeEmail
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.mail.attachments import Attachment, _sanitize_filename
from swiftpy.mail.envelope import Envelope, _format_address
from swiftpy.mail.exceptions import (
    AttachmentTooLargeError,
    HeaderInjectionError,
    MailAuthenticationError,
    MailConnectionError,
    MailTransportError,
    MailValidationError,
)
from swiftpy.mail.facade import Mail
from swiftpy.mail.mailable import Mailable
from swiftpy.mail.manager import MailManager
from swiftpy.mail.message import MailMessage
from swiftpy.mail.queue.job import SendQueuedMailableJob
from swiftpy.mail.templates.renderer import MailTemplateRenderer
from swiftpy.mail.transports.api.mailgun import MailgunTransport
from swiftpy.mail.transports.api.resend import ResendTransport
from swiftpy.mail.transports.api.sendgrid import SendGridTransport
from swiftpy.mail.transports.console import ConsoleTransport
from swiftpy.mail.transports.sendmail import SendmailTransport
from swiftpy.mail.transports.smtp import SMTPTransport
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job, JobSerializationError
from swiftpy.queue.worker import QueueWorker


class _AliceMailable(Mailable):
    def __init__(self, to_addr: str) -> None:
        self.to_addr = to_addr

    def build(self) -> MailMessage:
        return MailMessage(
            subject="Alice Notice",
            to=[self.to_addr],
            text_body="Hello Alice",
        )


class _BobMailable(Mailable):
    def __init__(self, to_addr: str) -> None:
        self.to_addr = to_addr

    def build(self) -> MailMessage:
        return MailMessage(
            subject="Bob Receipt",
            to=[self.to_addr],
            text_body="Hello Bob",
        )


class _CountingBinaryStream(io.BytesIO):
    """Seekable binary stream tracking total bytes read to prove streaming max_size enforcement."""

    def __init__(self, chunk_size: int, total_chunks: int) -> None:
        super().__init__()
        self._chunk_size = chunk_size
        self._total_chunks = total_chunks
        self._chunk_index = 0
        self.bytes_read = 0

    def seek(self, cookie: int, whence: int = 0) -> int:
        if cookie == 0 and whence == 0:
            self._chunk_index = 0
        return cookie

    def read(self, size: int | None = -1) -> bytes:
        if self._chunk_index >= self._total_chunks:
            return b""
        if size is None or size < 0:
            # Fail loudly if code tries an unbounded full .read()
            raise AssertionError("Attachment read called unbounded bio.read() without chunk size")
        to_read = min(size, self._chunk_size)
        self._chunk_index += 1
        self.bytes_read += to_read
        return b"A" * to_read


class _FakeWorkerDispatcher(QueueDispatcher):
    def __init__(self) -> None:
        super().__init__(prefix="test_mail_worker:")
        self.acked: list[tuple[str, str, str]] = []
        self.released: list[tuple[str, str, str, float]] = []
        self.failed_dlq: list[tuple[Any, BaseException, str]] = []

    async def ack(self, queue: str, worker_id: str, raw_payload: str) -> bool:
        self.acked.append((queue, worker_id, raw_payload))
        return True

    async def release(
        self,
        queue: str,
        worker_id: str,
        raw_payload: str,
        delay: float = 0.0,
        new_payload: str | None = None,
    ) -> bool:
        self.released.append((queue, worker_id, raw_payload, delay))
        return True

    async def push_failed(
        self,
        job: Job | None,
        exception: BaseException,
        queue: str = "default",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.failed_dlq.append((job, exception, queue))


class _SMTPServerProtocol(asyncio.StreamReaderProtocol):
    """StreamReaderProtocol that starts the SMTP handler once across STARTTLS."""

    def __init__(
        self,
        stream_reader: asyncio.StreamReader,
        on_connect: Callable[
            [asyncio.StreamReader, asyncio.StreamWriter], Coroutine[Any, Any, None]
        ],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__(stream_reader, loop=loop)
        self._on_connect = on_connect
        self._session_started = False
        self._reader_ref = stream_reader
        self._loop_ref = loop

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        if self._session_started:
            return
        self._session_started = True
        assert isinstance(transport, asyncio.WriteTransport)
        writer = asyncio.StreamWriter(
            transport, self, self._reader_ref, self._loop_ref
        )
        task = self._loop_ref.create_task(self._on_connect(self._reader_ref, writer))

        def _done(t: asyncio.Task[None]) -> None:
            if not t.cancelled() and t.exception() is not None:
                transport.close()

        task.add_done_callback(_done)


class _LoopbackSMTPServer:
    """Async TCP SMTP server supporting real STARTTLS upgrade and configurable AUTH replies."""

    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext | None = None,
        auth_plain_code: int = 235,
    ) -> None:
        self.ssl_context = ssl_context
        self.auth_plain_code = auth_plain_code
        self.server: asyncio.Server | None = None
        self.host = "127.0.0.1"
        self.port = 0
        self.tls_upgraded = False
        self.commands_seen: list[str] = []
        self.received_messages: list[dict[str, Any]] = []

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        def _factory() -> _SMTPServerProtocol:
            stream_reader = asyncio.StreamReader()
            return _SMTPServerProtocol(stream_reader, self._handle, loop)

        self.server = await loop.create_server(_factory, self.host, 0)
        assert self.server.sockets
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        loop = asyncio.get_running_loop()
        current_writer = writer
        sender: str | None = None
        recipients: list[str] = []
        auth_login_step = 0

        try:
            current_writer.write(b"220 localhost ESMTP Ready\r\n")
            await current_writer.drain()

            while True:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                upper = line.upper()
                self.commands_seen.append(line)

                if auth_login_step == 1:
                    auth_login_step = 2
                    current_writer.write(b"334 UGFzc3dvcmQ6\r\n")
                    await current_writer.drain()
                    continue
                if auth_login_step == 2:
                    auth_login_step = 0
                    current_writer.write(b"235 2.7.0 Authentication successful\r\n")
                    await current_writer.drain()
                    continue

                if upper.startswith("EHLO"):
                    caps = [
                        b"250-localhost Hello\r\n",
                        b"250-AUTH PLAIN LOGIN\r\n",
                    ]
                    if self.ssl_context is not None and not self.tls_upgraded:
                        caps.append(b"250-STARTTLS\r\n")
                    caps.append(b"250 OK\r\n")
                    current_writer.write(b"".join(caps))
                    await current_writer.drain()
                elif upper == "STARTTLS":
                    if self.ssl_context is None:
                        current_writer.write(b"454 TLS not available\r\n")
                        await current_writer.drain()
                        continue
                    current_writer.write(b"220 2.0.0 Ready to start TLS\r\n")
                    await current_writer.drain()
                    protocol = current_writer.transport.get_protocol()
                    new_transport = await loop.start_tls(
                        current_writer.transport,
                        protocol,
                        self.ssl_context,
                        server_side=True,
                    )
                    assert isinstance(new_transport, asyncio.WriteTransport)
                    current_writer = asyncio.StreamWriter(
                        new_transport, protocol, reader, loop
                    )
                    self.tls_upgraded = True
                elif upper.startswith("AUTH PLAIN"):
                    if self.auth_plain_code == 235:
                        current_writer.write(b"235 2.7.0 OK\r\n")
                    else:
                        current_writer.write(
                            f"{self.auth_plain_code} AUTH PLAIN failed/unsupported\r\n".encode()
                        )
                    await current_writer.drain()
                elif upper.startswith("AUTH LOGIN"):
                    auth_login_step = 1
                    current_writer.write(b"334 VXNlcm5hbWU6\r\n")
                    await current_writer.drain()
                elif upper.startswith("MAIL FROM:"):
                    sender = line.split(":", 1)[1].strip("<> ")
                    current_writer.write(b"250 2.1.0 OK\r\n")
                    await current_writer.drain()
                elif upper.startswith("RCPT TO:"):
                    recipients.append(line.split(":", 1)[1].strip("<> "))
                    current_writer.write(b"250 2.1.5 OK\r\n")
                    await current_writer.drain()
                elif upper == "DATA":
                    current_writer.write(b"354 End with <CRLF>.<CRLF>\r\n")
                    await current_writer.drain()
                    chunks: list[bytes] = []
                    while True:
                        raw_line = await reader.readline()
                        if not raw_line or raw_line in (b".\r\n", b".\n"):
                            break
                        if raw_line.startswith(b".."):
                            raw_line = raw_line[1:]
                        chunks.append(raw_line)
                    self.received_messages.append(
                        {"from": sender, "to": list(recipients), "data": b"".join(chunks)}
                    )
                    current_writer.write(b"250 2.0.0 Queued\r\n")
                    await current_writer.drain()
                elif upper == "QUIT":
                    current_writer.write(b"221 Bye\r\n")
                    await current_writer.drain()
                    break
                else:
                    current_writer.write(b"500 Unknown\r\n")
                    await current_writer.drain()
        except (OSError, ssl.SSLError, TimeoutError, asyncio.CancelledError):
            pass
        finally:
            try:
                current_writer.close()
                await current_writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass


# ---------------------------------------------------------------------------
# MAIL-1: Manager default_from CRLF check, immutability, & address formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mail1_manager_validates_default_from_and_does_not_mutate_caller() -> None:
    bad_cfg = Config(
        {
            "mail": {
                "default": "console",
                "from": {"address": "noreply@app.com\r\nBcc: evil@attacker.com", "name": "App"},
                "mailers": {"console": {"transport": "console"}},
            }
        }
    )
    manager = MailManager(config=bad_cfg)
    msg = MailMessage(subject="Hello", to=["alice@example.com"], text_body="Hi")

    with pytest.raises(HeaderInjectionError):
        await manager.send(msg)

    clean_cfg = Config(
        {
            "mail": {
                "default": "console",
                "from": {"address": "noreply@app.com", "name": "Acme, Inc."},
                "mailers": {"console": {"transport": "console"}},
            }
        }
    )
    clean_mgr = MailManager(config=clean_cfg)
    caller_msg = MailMessage(subject="Hello", to=["alice@example.com"], text_body="Hi")
    result = await clean_mgr.send(caller_msg)

    assert result.success is True
    # Caller's MailMessage must NOT be mutated in place
    assert caller_msg.from_address is None
    assert caller_msg.from_name is None

    # Envelope and _format_address must handle display overrides & comma quoting cleanly
    assert _format_address("Support <sup@x.com>", "Override") == "Override <sup@x.com>"
    assert _format_address("team@acme.com", "Acme, Inc.") == '"Acme, Inc." <team@acme.com>'

    comma_msg = MailMessage(
        subject="Commas",
        to=["Acme, Inc. <team@acme.com>"],
        from_address="Support <sup@x.com>",
        from_name="Override, Co.",
        text_body="Test",
    )
    mime_msg = await Envelope.build(comma_msg)
    assert mime_msg["From"] == '"Override, Co." <sup@x.com>'
    assert mime_msg["To"] == '"Acme, Inc." <team@acme.com>'


# ---------------------------------------------------------------------------
# MAIL-2: Sendmail delivers BCC via explicit argv without -t
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mail2_sendmail_delivers_bcc_via_explicit_argv(tmp_path: Path) -> None:
    fake_sendmail = tmp_path / "sendmail"
    fake_sendmail.write_text("#!/bin/sh\nexit 0\n")
    fake_sendmail.chmod(0o755)

    captured_cmd: list[str] = []
    captured_stdin = b""

    mock_proc = AsyncMock()

    async def _communicate(input: bytes = b"") -> tuple[bytes, bytes]:
        nonlocal captured_stdin
        captured_stdin = input
        return (b"OK", b"")

    mock_proc.communicate = _communicate
    mock_proc.returncode = 0

    async def _fake_exec(*cmd: str, **kwargs: Any) -> Any:
        captured_cmd.extend(cmd)
        return mock_proc

    transport = SendmailTransport(
        path=str(fake_sendmail),
        default_from=("noreply@app.com", "App"),
    )
    msg = MailMessage(
        subject="Secret BCC",
        to=["alice@example.com"],
        cc=["cc@example.com"],
        bcc=["secret-bcc@example.com"],
        text_body="Hello",
    )

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        result = await transport.send(msg)

    assert "-t" not in captured_cmd
    assert "alice@example.com" in captured_cmd
    assert "cc@example.com" in captured_cmd
    assert "secret-bcc@example.com" in captured_cmd
    assert b"secret-bcc@example.com" not in captured_stdin
    assert set(result.accepted) == {
        "alice@example.com",
        "cc@example.com",
        "secret-bcc@example.com",
    }


# ---------------------------------------------------------------------------
# MAIL-3 (G-8): SendQueuedMailableJob DLQ, trusted namespaces, state & manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mail3_queued_job_trusted_namespaces_state_manager_and_dlq() -> None:
    # 1. Untrusted mailable module namespace rejected before import
    untrusted_payload = {
        "job_class": "swiftpy.mail.queue.job:SendQueuedMailableJob",
        "job_id": "job-untrusted",
        "queue": "emails",
        "tries": 3,
        "attempts": 0,
        "data": {
            "mailable_class": "tests.mail.test_phase6_mail_remediation:_AliceMailable",
            "mailable_state": {"to_addr": "alice@example.com"},
        },
    }
    Job.set_allow_unsigned(True)
    try:
        untrusted_job = Job.deserialize(untrusted_payload, allow_unsigned=True)
        assert isinstance(untrusted_job, SendQueuedMailableJob)
        with pytest.raises((JobSerializationError, MailValidationError, ValueError)):
            await untrusted_job.handle()
    finally:
        Job.set_allow_unsigned(False)

    # 2. Allowlisted mailable rejects private/undeclared state attributes
    class _PoisonMailable(Mailable):
        def __init__(self, to_addr: str) -> None:
            self.to_addr = to_addr

        def build(self) -> MailMessage:
            return MailMessage(subject="Ok", to=[self.to_addr], text_body="Ok")

    SendQueuedMailableJob.allow_mailable_class(_PoisonMailable)
    Job.set_allow_unsigned(True)
    try:
        poison_payload = {
            "job_class": "swiftpy.mail.queue.job:SendQueuedMailableJob",
            "job_id": "job-poison",
            "queue": "emails",
            "tries": 3,
            "attempts": 0,
            "data": {
                "mailable_class": f"{_PoisonMailable.__module__}:{_PoisonMailable.__qualname__}",
                "mailable_state": {"to_addr": "a@b.com", "_secret_override": True},
            },
        }
        poison_job = Job.deserialize(poison_payload, allow_unsigned=True)
        assert isinstance(poison_job, SendQueuedMailableJob)
        with pytest.raises((JobSerializationError, MailValidationError, ValueError)):
            await poison_job.handle()
    finally:
        SendQueuedMailableJob._trusted_mailable_classes.clear()
        Job.set_allow_unsigned(False)

    # 3. Handle dispatches via MailManager.send() (default_from applies)
    cfg = Config(
        {
            "mail": {
                "default": "console",
                "from": {"address": "system@app.com", "name": "System"},
                "mailers": {"console": {"transport": "console"}},
            }
        }
    )
    MailManager._instance = MailManager(config=cfg)
    try:
        job_ok = SendQueuedMailableJob(mailable=_AliceMailable("alice@example.com"))
        await job_ok.handle()
        console = MailManager.get_instance().mailer("console")
        assert isinstance(console, ConsoleTransport)
        sent = console.sent_messages[-1]
        assert sent.from_address == "system@app.com"
    finally:
        MailManager._instance = None

    # 4. Permanent mail errors propagate and move directly to DLQ without retry
    mock_transport = AsyncMock()
    mock_transport.send.side_effect = MailAuthenticationError("Bad SMTP key")
    mgr = MailManager(config=cfg)
    mgr._transports["console"] = mock_transport
    MailManager._instance = mgr
    dispatcher = _FakeWorkerDispatcher()
    worker = QueueWorker(dispatcher=dispatcher, allow_unsigned=True)
    SendQueuedMailableJob.allow_mailable_class(_AliceMailable)
    try:
        perm_job = SendQueuedMailableJob(mailable=_AliceMailable("alice@example.com"))
        raw = json.dumps(perm_job.serialize(allow_unsigned=True))
        ok = await worker.process_job("emails", raw)
        assert ok is False
        assert len(dispatcher.released) == 0
        assert len(dispatcher.failed_dlq) == 1
        assert isinstance(dispatcher.failed_dlq[0][1], MailAuthenticationError)
        assert len(dispatcher.acked) == 1
    finally:
        SendQueuedMailableJob._trusted_mailable_classes.clear()
        MailManager._instance = None


# ---------------------------------------------------------------------------
# MAIL-4: Correlated MailFake assertions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mail4_mailfake_correlates_mailable_and_recipient() -> None:
    with Mail.fake() as fake:
        await Mail.send(_AliceMailable("alice@example.com"))
        await Mail.send(_BobMailable("bob@example.com"))

        # Bob never received _AliceMailable, only _BobMailable
        fake.assert_not_sent(_AliceMailable, recipient="bob@example.com")
        with pytest.raises(AssertionError):
            fake.assert_sent(_AliceMailable, recipient="bob@example.com")

        fake.assert_sent(_AliceMailable, recipient="alice@example.com", count=1)
        fake.assert_sent(_BobMailable, recipient="bob@example.com", count=1)
        with pytest.raises(AssertionError):
            fake.assert_not_sent(_AliceMailable, recipient="alice@example.com")


# ---------------------------------------------------------------------------
# MAIL-5 (G-8): Sandboxed templates, views/ default, WelcomeEmail rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mail5_template_defaults_welcome_email_and_dunder_block(
    tmp_path: Path,
) -> None:
    default_renderer = MailTemplateRenderer()
    assert default_renderer.template_dir == Path("views")

    # WelcomeEmail resolves views/mail/template.html cleanly
    welcome_msg = await WelcomeEmail("Alice", to="alice@example.com").build_message()
    assert welcome_msg.html_body is not None
    assert "Welcome, Alice!" in welcome_msg.html_body
    assert "SwiftPY" in welcome_msg.html_body

    # Fallback renderer blocks private/dunder traversal
    with pytest.raises(MailValidationError):
        default_renderer.render_string("{{ user.__class__ }}", {"user": "x"})
    with pytest.raises(MailValidationError):
        default_renderer.render_string("{{ user._secret }}", {"user": "x"})

    # When Jinja2 is configured, SandboxedEnvironment is selected and get_template runs off-loop
    fake_template = MagicMock()
    fake_template.render_async = AsyncMock(return_value="<p>Hi</p>")
    fake_env = MagicMock()
    fake_env.get_template = MagicMock(return_value=fake_template)
    fake_sandbox_cls = MagicMock(return_value=fake_env)
    fake_jinja = MagicMock()

    tdir = tmp_path / "views"
    (tdir / "mail").mkdir(parents=True)
    (tdir / "mail" / "demo.html").write_text("<p>{{ name }}</p>", encoding="utf-8")

    with (
        patch("swiftpy.mail.templates.renderer.jinja2", fake_jinja),
        patch("swiftpy.mail.templates.renderer._jinja_sandbox_env_cls", fake_sandbox_cls),
    ):
        r = MailTemplateRenderer(template_dir=tdir)
        fake_sandbox_cls.assert_called_once()
        out = await r.render("mail/demo", {"name": "Alice"})
        assert out.html == "<p>Hi</p>"
        fake_env.get_template.assert_called_once_with("mail/demo.html")


# ---------------------------------------------------------------------------
# MAIL-6: Auth fallback (500/501/504), STARTTLS loopback, & CRLF dot-stuffing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mail6_smtp_real_starttls_loopback_auth_fallback_and_dot_stuffing(
    tmp_path: Path,
) -> None:
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    proc = await asyncio.to_thread(
        subprocess.run,
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_file),
            "-out",
            str(cert_file),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.returncode == 0

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))

    server = _LoopbackSMTPServer(ssl_context=server_ctx, auth_plain_code=504)
    await server.start()
    try:
        # 1. Untrusted self-signed cert rejected when validate_certs=True without cafile
        untrusted = SMTPTransport(
            host="127.0.0.1",
            port=server.port,
            encryption="starttls",
            validate_certs=True,
            use_native=True,
        )
        msg = MailMessage(
            subject="TLS Test",
            to=["u@example.com"],
            from_address="s@example.com",
            text_body=".leading dot line\nSecond line",
        )
        with pytest.raises((MailConnectionError, MailTransportError)):
            await untrusted.send(msg)

        # 2. Trusted CA cert completes real STARTTLS + AUTH PLAIN(504)->AUTH LOGIN + dot-stuffing
        trusted_ctx = ssl.create_default_context(cafile=str(cert_file))
        transport = SMTPTransport(
            host="127.0.0.1",
            port=server.port,
            username="smtp_user",
            password="smtp_password",
            encryption="starttls",
            validate_certs=True,
            use_native=True,
        )
        with patch.object(transport, "_create_ssl_context", return_value=trusted_ctx):
            result = await transport.send(msg)

        assert result.success is True
        assert server.tls_upgraded is True
        assert any(c.startswith("AUTH LOGIN") for c in server.commands_seen)
        assert len(server.received_messages) == 1
        parsed = email.message_from_bytes(
            server.received_messages[0]["data"], policy=email.policy.default
        )
        assert ".leading dot line" in str(parsed.get_content())
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# MAIL-7 & MAIL-8: API fields, provider registration, limits, & sanitization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mail7_and_mail8_api_transports_limits_provider_and_attachments() -> None:
    # 1. Application binds MailManager via default providers
    app = Application(auto_discover_providers=False)
    assert app.container.has(MailManager)

    # 2. Streaming BinaryIO Attachment enforces max_size incrementally
    counting_stream = _CountingBinaryStream(chunk_size=65536, total_chunks=10)
    stream_att = Attachment.from_stream(counting_stream, filename='bad"name.bin')
    assert '\\"' in stream_att.filename
    with pytest.raises(AttachmentTooLargeError):
        await stream_att.read_bytes(max_size=100)
    assert counting_stream.bytes_read <= 65536
    assert _sanitize_filename('..\\..\\sub"dir\\ok.txt') == "ok.txt"
    assert _sanitize_filename('dir/my"file.txt') == 'my\\"file.txt'

    # 3. API transports forward cc/bcc accepted, headers/tags/metadata, comma quotes, limits
    captured_sg: dict[str, Any] = {}
    captured_rs: dict[str, Any] = {}
    captured_mg: bytes = b""

    def sg_handler(req: httpx.Request) -> httpx.Response:
        nonlocal captured_sg
        captured_sg = json.loads(req.read().decode("utf-8"))
        return httpx.Response(202, headers={"x-message-id": "sg-1"})

    def rs_handler(req: httpx.Request) -> httpx.Response:
        nonlocal captured_rs
        captured_rs = json.loads(req.read().decode("utf-8"))
        return httpx.Response(200, json={"id": "rs-1"})

    def mg_handler(req: httpx.Request) -> httpx.Response:
        nonlocal captured_mg
        captured_mg = req.read()
        return httpx.Response(200, json={"id": "mg-1"})

    msg = MailMessage(
        subject="API Parity",
        to=["to@example.com"],
        cc=["cc@example.com"],
        bcc=["bcc@example.com"],
        from_address="team@acme.com",
        from_name="Acme, Inc.",
        headers={"X-Custom": "abc"},
        tags=["welcome"],
        metadata={"tenant": "t-42"},
        text_body="Hello API",
    )

    sg = SendGridTransport(
        api_key="sg_k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(sg_handler)),
        max_attachment_size_bytes=50,
        max_message_size_bytes=5000,
    )
    sg_res = await sg.send(msg)
    assert set(sg_res.accepted) == {"to@example.com", "cc@example.com", "bcc@example.com"}
    assert captured_sg["headers"] == {"X-Custom": "abc"}
    assert captured_sg["categories"] == ["welcome"]
    assert captured_sg["custom_args"] == {"tenant": "t-42"}

    rs = ResendTransport(
        api_key="re_k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(rs_handler)),
        max_attachment_size_bytes=50,
    )
    rs_res = await rs.send(msg)
    assert set(rs_res.accepted) == {"to@example.com", "cc@example.com", "bcc@example.com"}
    assert captured_rs["from"] == '"Acme, Inc." <team@acme.com>'
    assert captured_rs["headers"] == {"X-Custom": "abc"}
    assert {"name": "tenant", "value": "t-42"} in captured_rs["tags"]

    mg = MailgunTransport(
        api_key="mg_k",
        domain="acme.test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(mg_handler)),
        max_attachment_size_bytes=50,
    )
    mg_res = await mg.send(msg)
    assert set(mg_res.accepted) == {"to@example.com", "cc@example.com", "bcc@example.com"}
    assert b'"Acme, Inc." <team@acme.com>' in captured_mg
    assert b'name="o:tag"' in captured_mg
    assert b'name="v:tenant"' in captured_mg

    # 4. Oversized attachment fails mail.limits cleanly
    msg_large = MailMessage(
        subject="Too Big",
        to=["u@example.com"],
        from_address="s@example.com",
        text_body="Body",
    )
    msg_large.attach_from_bytes(b"X" * 100, filename="big.bin")
    with pytest.raises(AttachmentTooLargeError):
        await sg.send(msg_large)

    # 5. Upstream HTTP error bodies are sanitized & truncated
    def err_handler(req: httpx.Request) -> httpx.Response:
        secret_body = "<html>Bearer sk_live_SECRET1234567890 " + ("Z" * 1000) + "</html>"
        return httpx.Response(502, text=secret_body)

    err_rs = ResendTransport(
        api_key="re_k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(err_handler)),
    )
    with pytest.raises(MailTransportError) as err_exc:
        await err_rs.send(msg)
    err_str = str(err_exc.value)
    assert len(err_str) <= 380
    assert "sk_live_SECRET1234567890" not in err_str
