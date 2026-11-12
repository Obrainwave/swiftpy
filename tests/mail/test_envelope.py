from __future__ import annotations

import email
import email.policy

import pytest

from swiftpy.mail.attachments import Attachment
from swiftpy.mail.envelope import Envelope
from swiftpy.mail.exceptions import AttachmentTooLargeError, MailValidationError
from swiftpy.mail.message import MailMessage


@pytest.mark.asyncio
async def test_envelope_text_only() -> None:
    msg = MailMessage(
        subject="Simple Text",
        to=["alice@example.com"],
        from_address="noreply@example.com",
        from_name="App",
        text_body="Hello Plain Text",
    )
    email_msg = await Envelope.build(msg)
    assert email_msg["Subject"] == "Simple Text"
    assert email_msg["To"] == "alice@example.com"
    assert "App <noreply@example.com>" in email_msg["From"]
    assert email_msg.get_content_type() == "text/plain"
    assert email_msg.get_content() == "Hello Plain Text\n"


@pytest.mark.asyncio
async def test_envelope_html_and_text() -> None:
    msg = MailMessage(
        subject="HTML Email",
        to=["alice@example.com"],
        from_address="noreply@example.com",
        text_body="Plain Text Version",
        html_body="<h1>HTML Version</h1>",
    )
    email_msg = await Envelope.build(msg)
    assert email_msg.get_content_type() == "multipart/alternative"
    body_types = [p.get_content_type() for p in email_msg.iter_parts()]
    assert "text/plain" in body_types
    assert "text/html" in body_types


@pytest.mark.asyncio
async def test_envelope_exact_mime_tree_and_inline_cid() -> None:
    msg = MailMessage(
        subject="Invoice & Inline Logo",
        to=["alice@example.com"],
        from_address="billing@example.com",
        text_body="Please find your invoice attached.",
        html_body="<h1>Invoice</h1><img src='cid:logo'>",
    )
    msg.attach(
        Attachment.inline_from_bytes(b"png_logo_bytes", cid="logo", filename="logo.png")
    )
    msg.attach(
        Attachment.from_bytes(
            b"pdf_invoice_bytes",
            filename="invoice.pdf",
            content_type="application/pdf",
        )
    )

    email_msg = await Envelope.build(msg)

    # 1. Root must be multipart/mixed
    assert email_msg.get_content_type() == "multipart/mixed"
    root_parts = list(email_msg.iter_parts())
    assert len(root_parts) == 2

    # 2. First part of mixed is multipart/alternative
    alt_part = root_parts[0]
    assert alt_part.get_content_type() == "multipart/alternative"
    alt_subparts = list(alt_part.iter_parts())
    assert len(alt_subparts) == 2

    # 2a. text/plain part
    plain_part = alt_subparts[0]
    assert plain_part.get_content_type() == "text/plain"
    assert "Please find your invoice attached." in plain_part.get_content()

    # 2b. multipart/related part holding html + inline image
    related_part = alt_subparts[1]
    assert related_part.get_content_type() == "multipart/related"
    related_subparts = list(related_part.iter_parts())
    assert len(related_subparts) == 2

    html_part = related_subparts[0]
    assert html_part.get_content_type() == "text/html"
    assert "<h1>Invoice</h1><img src='cid:logo'>" in html_part.get_content()

    inline_part = related_subparts[1]
    assert inline_part.get_content_type() == "image/png"
    assert inline_part.get_content_disposition() == "inline"
    assert inline_part.get("Content-ID") == "<logo>"
    assert inline_part.get_payload(decode=True) == b"png_logo_bytes"

    # 3. Second part of mixed is the regular attachment
    pdf_part = root_parts[1]
    assert pdf_part.get_content_type() == "application/pdf"
    assert pdf_part.get_content_disposition() == "attachment"
    assert pdf_part.get_filename() == "invoice.pdf"
    assert pdf_part.get_payload(decode=True) == b"pdf_invoice_bytes"


@pytest.mark.asyncio
async def test_envelope_bcc_privacy_guarantee() -> None:
    msg = MailMessage(
        subject="Privacy Test",
        to=["alice@example.com"],
        cc=["bob@example.com"],
        bcc=["secret-audit@example.com", "hidden@example.com"],
        from_address="boss@example.com",
        text_body="Confidential project details",
        headers={"Bcc": "malicious-header-injection@example.com", "X-Job-ID": "12345"},
    )

    email_msg = await Envelope.build(msg)

    # 1. Bcc header MUST NOT exist on the email message
    assert "Bcc" not in email_msg
    assert "bcc" not in email_msg
    assert email_msg["Bcc"] is None

    # 2. X-Job-ID custom header is preserved
    assert email_msg["X-Job-ID"] == "12345"

    # 3. Serialized bytes and string MUST NOT contain any of the BCC addresses
    raw_str = email_msg.as_string()
    raw_bytes = email_msg.as_bytes()

    assert "secret-audit@example.com" not in raw_str
    assert b"secret-audit@example.com" not in raw_bytes

    assert "hidden@example.com" not in raw_str
    assert b"hidden@example.com" not in raw_bytes

    assert "malicious-header-injection@example.com" not in raw_str
    assert b"malicious-header-injection@example.com" not in raw_bytes


@pytest.mark.asyncio
async def test_envelope_non_ascii_filename_and_parser_roundtrip() -> None:
    non_ascii_name = "отчет_2026_résumé.pdf"
    content = b"%PDF-1.4 simulated pdf document data"

    msg = MailMessage(
        subject="Non-ASCII Attachment Test",
        to=["recipient@example.com"],
        from_address="sender@example.com",
        text_body="See attached document.",
    )
    msg.attach(
        Attachment.from_bytes(
            content, filename=non_ascii_name, content_type="application/pdf"
        )
    )

    email_msg = await Envelope.build(msg)
    raw_bytes = email_msg.as_bytes()

    # Re-parse the serialized message bytes using standard library policy.default
    parsed = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    found_attachments = [
        part for part in parsed.walk() if part.get_content_disposition() == "attachment"
    ]
    assert len(found_attachments) == 1

    reparsed_att = found_attachments[0]
    assert reparsed_att.get_filename() == non_ascii_name
    assert reparsed_att.get_payload(decode=True) == content


@pytest.mark.asyncio
async def test_envelope_missing_recipient_raises() -> None:
    msg = MailMessage(subject="No recipient", from_address="a@example.com")
    with pytest.raises(MailValidationError, match="at least one recipient"):
        await Envelope.build(msg)


@pytest.mark.asyncio
async def test_envelope_missing_from_raises() -> None:
    msg = MailMessage(subject="No from", to=["user@example.com"])
    with pytest.raises(MailValidationError, match="from_address"):
        await Envelope.build(msg)


@pytest.mark.asyncio
async def test_envelope_size_limit_exceeded() -> None:
    msg = MailMessage(
        subject="Huge",
        to=["user@example.com"],
        from_address="a@example.com",
        text_body="A" * 1000,
    )
    with pytest.raises(AttachmentTooLargeError, match="exceeds configured limit"):
        await Envelope.build(msg, max_message_size_bytes=500)
