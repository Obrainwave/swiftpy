from __future__ import annotations

from pathlib import Path

import pytest

from swiftpy.mail.exceptions import (
    HeaderInjectionError,
    InvalidAddressError,
    MailValidationError,
)
from swiftpy.mail.message import MailMessage


def test_basic_mail_message_creation() -> None:
    msg = MailMessage(
        subject="Welcome",
        to=["alice@example.com"],
        text_body="Welcome to SwiftPY!",
    )
    assert msg.subject == "Welcome"
    assert msg.to == ["alice@example.com"]
    assert msg.text_body == "Welcome to SwiftPY!"
    assert msg.html_body is None
    assert msg.cc == []
    assert msg.bcc == []


def test_mail_message_normalizes_single_recipient_string() -> None:
    msg = MailMessage(
        subject="Hello",
        to="user@example.com",  # type: ignore[arg-type]
        cc="manager@example.com",  # type: ignore[arg-type]
        bcc="audit@example.com",  # type: ignore[arg-type]
    )
    assert msg.to == ["user@example.com"]
    assert msg.cc == ["manager@example.com"]
    assert msg.bcc == ["audit@example.com"]


def test_mail_message_formatted_names() -> None:
    msg = MailMessage(
        subject="Hello",
        to=["Alice Smith <alice@example.com>"],
        from_address="Support <support@example.com>",
        reply_to="Helpdesk <help@example.com>",
    )
    assert msg.to == ["Alice Smith <alice@example.com>"]
    assert msg.all_recipient_addresses() == ["alice@example.com"]


def test_fluent_attachments_and_headers(tmp_path: Path) -> None:
    msg = MailMessage(subject="Report", to=["user@example.com"])
    msg.add_header("X-Custom-Tag", "finance")

    test_file = tmp_path / "report.csv"
    test_file.write_text("a,b,c\n1,2,3")

    msg.attach_from_path(test_file)
    msg.attach_from_bytes(b"image bytes", filename="logo.png", content_type="image/png")

    assert len(msg.attachments) == 2
    assert msg.attachments[0].filename == "report.csv"
    assert msg.attachments[1].filename == "logo.png"
    assert msg.headers["X-Custom-Tag"] == "finance"


def test_crlf_injection_in_subject() -> None:
    with pytest.raises(HeaderInjectionError, match="subject"):
        MailMessage(
            subject="Subject\r\nBcc: evil@attacker.com", to=["user@example.com"]
        )


def test_crlf_injection_in_recipients() -> None:
    with pytest.raises(HeaderInjectionError, match="to"):
        MailMessage(
            subject="Subject", to=["user@example.com\r\nBcc: victim@example.com"]
        )

    with pytest.raises(HeaderInjectionError, match="from_address"):
        MailMessage(
            subject="Subject",
            to=["user@example.com"],
            from_address="admin@example.com\nEvil: true",
        )

    with pytest.raises(HeaderInjectionError, match="from_name"):
        MailMessage(subject="Subject", to=["user@example.com"], from_name="Admin\rName")


def test_crlf_injection_in_headers() -> None:
    with pytest.raises(HeaderInjectionError, match="header key"):
        MailMessage(
            subject="Subject", to=["user@example.com"], headers={"X-Header\r\n": "val"}
        )

    with pytest.raises(HeaderInjectionError, match="header value"):
        MailMessage(
            subject="Subject",
            to=["user@example.com"],
            headers={"X-Header": "val\r\nInjected: true"},
        )


def test_invalid_email_addresses() -> None:
    with pytest.raises(InvalidAddressError):
        MailMessage(subject="Test", to=["invalid-address-without-at"])

    with pytest.raises(InvalidAddressError):
        MailMessage(subject="Test", to=["user@example"])

    with pytest.raises(InvalidAddressError):
        MailMessage(subject="Test", to=[""])


def test_none_subject_raises() -> None:
    with pytest.raises(MailValidationError):
        MailMessage(subject=None, to=["user@example.com"])  # type: ignore[arg-type]
