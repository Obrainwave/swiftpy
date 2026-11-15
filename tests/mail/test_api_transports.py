from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from swiftpy.mail.attachments import Attachment
from swiftpy.mail.exceptions import (
    MailAuthenticationError,
    MailConnectionError,
    MailRateLimitError,
    MailTimeoutError,
    MailTransportError,
    MailValidationError,
)
from swiftpy.mail.message import MailMessage
from swiftpy.mail.transports.api.mailgun import MailgunTransport
from swiftpy.mail.transports.api.resend import ResendTransport
from swiftpy.mail.transports.api.sendgrid import SendGridTransport

# ---------------------------------------------------------------------------
# Resend Deep Payload & Error Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resend_deep_payload_verification() -> None:
    captured_payload: dict[str, Any] = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        assert request.url == "https://api.resend.com/emails"
        assert request.headers["Authorization"] == "Bearer re_test_key"
        assert request.headers["Content-Type"] == "application/json"
        captured_payload = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"id": "re_msg_12345"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    transport = ResendTransport(
        api_key="re_test_key",
        client=mock_client,
        default_from=("noreply@app.com", "SwiftPY System"),
    )

    msg = MailMessage(
        subject="Resend Detailed Test",
        to=["to1@example.com", "to2@example.com"],
        cc=["cc1@example.com"],
        bcc=["bcc1@example.com"],
        reply_to="reply@example.com",
        from_address="custom@app.com",
        from_name="Custom Sender",
        text_body="Plain text payload",
        html_body="<p>HTML payload</p>",
        tags=["invoice", "transactional"],
    )
    raw_attachment = b"%PDF-1.4 simulated pdf data"
    msg.attach(
        Attachment.from_bytes(
            raw_attachment, filename="doc.pdf", content_type="application/pdf"
        )
    )

    result = await transport.send(msg)

    assert result.success is True
    assert result.message_id == "re_msg_12345"
    assert result.provider == "resend"
    assert result.accepted == [
        "to1@example.com",
        "to2@example.com",
        "cc1@example.com",
        "bcc1@example.com",
    ]

    # Verify every element of the captured request payload
    assert captured_payload["from"] == "Custom Sender <custom@app.com>"
    assert captured_payload["to"] == ["to1@example.com", "to2@example.com"]
    assert captured_payload["cc"] == ["cc1@example.com"]
    assert captured_payload["bcc"] == ["bcc1@example.com"]
    assert captured_payload["reply_to"] == "reply@example.com"
    assert captured_payload["subject"] == "Resend Detailed Test"
    assert captured_payload["text"] == "Plain text payload"
    assert captured_payload["html"] == "<p>HTML payload</p>"
    assert captured_payload["tags"] == [
        {"name": "invoice", "value": "true"},
        {"name": "transactional", "value": "true"},
    ]

    # Verify attachment encoding
    assert len(captured_payload["attachments"]) == 1
    att = captured_payload["attachments"][0]
    assert att["filename"] == "doc.pdf"
    assert base64.b64decode(att["content"]) == raw_attachment


# ---------------------------------------------------------------------------
# SendGrid Deep Payload & Error Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sendgrid_deep_payload_verification() -> None:
    captured_payload: dict[str, Any] = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        assert request.url == "https://api.sendgrid.com/v3/mail/send"
        assert request.headers["Authorization"] == "Bearer sg_test_key"
        assert request.headers["Content-Type"] == "application/json"
        captured_payload = json.loads(request.read().decode("utf-8"))
        return httpx.Response(202, headers={"x-message-id": "sg_msg_98765"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    transport = SendGridTransport(
        api_key="sg_test_key",
        client=mock_client,
        default_from=("default@sendgrid.com", "Default Sender"),
    )

    msg = MailMessage(
        subject="SendGrid Detailed Test",
        to=["Alice Smith <alice@example.com>", "bob@example.com"],
        cc=["Carol <carol@example.com>"],
        bcc=["Dan <dan@example.com>"],
        reply_to="Support Team <support@example.com>",
        from_address="noreply@sendgrid.com",
        from_name="SendGrid Dispatcher",
        text_body="Plain text body",
        html_body="<p>HTML body</p>",
    )
    msg.attach(
        Attachment.inline_from_bytes(b"logo_bytes", cid="logo", filename="logo.png")
    )
    msg.attach(
        Attachment.from_bytes(
            b"data_bytes", filename="data.csv", content_type="text/csv"
        )
    )

    result = await transport.send(msg)

    assert result.success is True
    assert result.message_id == "sg_msg_98765"
    assert result.provider == "sendgrid"

    # Verify personalizations
    personalizations = captured_payload["personalizations"]
    assert len(personalizations) == 1
    p = personalizations[0]
    assert p["to"] == [
        {"name": "Alice Smith", "email": "alice@example.com"},
        {"email": "bob@example.com"},
    ]
    assert p["cc"] == [{"name": "Carol", "email": "carol@example.com"}]
    assert p["bcc"] == [{"name": "Dan", "email": "dan@example.com"}]

    # Verify from and reply_to
    assert captured_payload["from"] == {
        "name": "SendGrid Dispatcher",
        "email": "noreply@sendgrid.com",
    }
    assert captured_payload["reply_to"] == {
        "name": "Support Team",
        "email": "support@example.com",
    }
    assert captured_payload["subject"] == "SendGrid Detailed Test"

    # Verify content list order
    assert captured_payload["content"] == [
        {"type": "text/plain", "value": "Plain text body"},
        {"type": "text/html", "value": "<p>HTML body</p>"},
    ]

    # Verify attachments (both inline with content_id and standard attachment)
    attachments = captured_payload["attachments"]
    assert len(attachments) == 2

    inline_att = next(a for a in attachments if a["filename"] == "logo.png")
    assert inline_att["disposition"] == "inline"
    assert inline_att["content_id"] == "logo"
    assert base64.b64decode(inline_att["content"]) == b"logo_bytes"

    reg_att = next(a for a in attachments if a["filename"] == "data.csv")
    assert reg_att["disposition"] == "attachment"
    assert "content_id" not in reg_att
    assert base64.b64decode(reg_att["content"]) == b"data_bytes"


# ---------------------------------------------------------------------------
# Mailgun Deep Payload Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mailgun_deep_payload_verification() -> None:
    captured_body: bytes = b""
    captured_headers: dict[str, str] = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body, captured_headers
        captured_body = request.read()
        captured_headers = dict(request.headers)
        assert request.url == "https://api.mailgun.net/v3/my-domain.com/messages"
        # Mailgun uses HTTP Basic Auth: api:<api_key>
        expected_auth = "Basic " + base64.b64encode(b"api:mg_key_test").decode("ascii")
        assert request.headers["authorization"] == expected_auth
        return httpx.Response(
            200, json={"id": "<mg_test_999@my-domain.com>", "message": "Queued."}
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    transport = MailgunTransport(
        api_key="mg_key_test",
        domain="my-domain.com",
        client=mock_client,
        default_from=("noreply@my-domain.com", "Mailgun Default"),
    )

    msg = MailMessage(
        subject="Mailgun Multipart Verification",
        to=["recipient@example.com"],
        cc=["cc@example.com"],
        bcc=["bcc@example.com"],
        reply_to="reply@my-domain.com",
        headers={"X-Campaign": "Spring2026"},
        text_body="Mailgun text body",
        html_body="<h1>Mailgun HTML</h1>",
    )
    msg.attach(
        Attachment.inline_from_bytes(b"logo_data", cid="logo", filename="logo.png")
    )
    msg.attach(Attachment.from_bytes(b"doc_data", filename="doc.txt"))

    result = await transport.send(msg)

    assert result.success is True
    assert result.message_id == "<mg_test_999@my-domain.com>"
    assert result.provider == "mailgun"

    # Verify multipart content contains required field parts
    assert b'name="from"' in captured_body
    assert b'name="to"' in captured_body
    assert b'name="cc"' in captured_body
    assert b'name="bcc"' in captured_body
    assert b'name="subject"' in captured_body
    assert b'name="h:Reply-To"' in captured_body
    assert b'name="h:X-Campaign"' in captured_body
    assert b'name="text"' in captured_body
    assert b'name="html"' in captured_body
    assert b'name="inline"' in captured_body
    assert b'filename="logo.png"' in captured_body
    assert b'name="attachment"' in captured_body
    assert b'filename="doc.txt"' in captured_body


# ---------------------------------------------------------------------------
# Full REST Transport Error Matrix Verification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected_exception", "response_headers", "response_body"),
    [
        (400, MailValidationError, {}, '{"message": "Invalid request"}'),
        (422, MailValidationError, {}, '{"message": "Unprocessable entity"}'),
        (401, MailAuthenticationError, {}, '{"message": "Unauthorized"}'),
        (403, MailAuthenticationError, {}, '{"message": "Forbidden"}'),
        (404, MailTransportError, {}, '{"message": "Domain or resource not found"}'),
        (413, MailTransportError, {}, '{"message": "Payload too large"}'),
        (
            429,
            MailRateLimitError,
            {"Retry-After": "45"},
            '{"message": "Too Many Requests"}',
        ),
        (500, MailTransportError, {}, '{"message": "Internal Server Error"}'),
        (502, MailTransportError, {}, "Bad Gateway"),
        (503, MailTransportError, {}, "Service Unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_rest_transports_error_matrix(
    status_code: int,
    expected_exception: type[Exception],
    response_headers: dict[str, str],
    response_body: str,
) -> None:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=response_body, headers=response_headers)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    transport = ResendTransport(api_key="key", client=mock_client)

    msg = MailMessage(
        subject="Error Matrix",
        to=["test@example.com"],
        from_address="sender@example.com",
        text_body="Error test",
    )

    with pytest.raises(expected_exception) as exc_info:
        await transport.send(msg)

    if status_code == 429:
        rate_limit_err = exc_info.value
        assert isinstance(rate_limit_err, MailRateLimitError)
        assert rate_limit_err.retry_after == 45.0


@pytest.mark.asyncio
async def test_rest_transports_timeout_and_network_errors() -> None:
    # 1. Timeout error
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Request timed out", request=request)

    timeout_client = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
    transport_timeout = ResendTransport(api_key="key", client=timeout_client)
    msg = MailMessage(
        subject="Timeout",
        to=["t@example.com"],
        from_address="s@example.com",
        text_body="Body",
    )

    with pytest.raises(MailTimeoutError, match="timed out"):
        await transport_timeout.send(msg)

    # 2. Connection error
    def connect_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Failed to resolve host", request=request)

    connect_client = httpx.AsyncClient(transport=httpx.MockTransport(connect_handler))
    transport_conn = SendGridTransport(api_key="key", client=connect_client)

    with pytest.raises(MailConnectionError, match="network connection error"):
        await transport_conn.send(msg)


@pytest.mark.asyncio
async def test_rest_transport_malformed_json_response() -> None:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Not a JSON string {invalid}")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    transport = ResendTransport(api_key="key", client=mock_client)
    msg = MailMessage(
        subject="Malformed",
        to=["t@example.com"],
        from_address="s@example.com",
        text_body="Body",
    )

    # Malformed 200 response should not crash with uncaught JSONDecodeError
    result = await transport.send(msg)
    assert result.success is True
    assert result.message_id == ""


# ---------------------------------------------------------------------------
# HTTP Client Lifecycle & Ownership Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_transport_client_lifecycle_and_ownership() -> None:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "msg_001"})

    external_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    transport_injected = ResendTransport(api_key="key", client=external_client)

    # Transport did NOT create this client -> _owns_client is False
    assert transport_injected._owns_client is False

    msg = MailMessage(
        subject="Ownership",
        to=["t@example.com"],
        from_address="s@example.com",
        text_body="Body",
    )
    await transport_injected.send(msg)

    # Closing the transport must NOT close the injected external client
    await transport_injected.close()
    assert external_client.is_closed is False

    # Attempting to send after transport is closed raises MailConnectionError
    with pytest.raises(MailConnectionError, match="Transport has been closed"):
        await transport_injected.send(msg)

    await external_client.aclose()


@pytest.mark.asyncio
async def test_rest_transport_owns_created_client() -> None:
    transport = ResendTransport(api_key="key")
    assert transport._owns_client is True

    # Internally created client is retrieved and closed on transport.close()
    client = await transport._get_client()
    assert client.is_closed is False

    await transport.close()
    assert client.is_closed is True

    # Multiple calls to close() are idempotent
    await transport.close()
