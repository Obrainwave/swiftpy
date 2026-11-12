from __future__ import annotations

import email.utils
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from swiftpy.mail.attachments import Attachment
from swiftpy.mail.exceptions import (
    HeaderInjectionError,
    InvalidAddressError,
    MailValidationError,
)

_CRLF_PATTERN = re.compile(r"[\r\n\0]")
_BASIC_EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)


def _validate_no_crlf(value: str, field_name: str) -> None:
    if _CRLF_PATTERN.search(value):
        raise HeaderInjectionError(
            f"Detected CRLF or null byte injection in {field_name}: {value!r}"
        )


def _validate_address(raw_address: str, field_name: str = "recipient") -> str:
    """Validate and normalize an email address or formatted name <addr> string."""
    _validate_no_crlf(raw_address, field_name)
    cleaned = raw_address.strip()
    if not cleaned:
        raise InvalidAddressError(f"{field_name} address cannot be empty")

    _name, addr = email.utils.parseaddr(cleaned)
    target = addr if addr else cleaned
    if not _BASIC_EMAIL_REGEX.match(target):
        raise InvalidAddressError(f"Invalid email address '{cleaned}' in {field_name}")
    return cleaned


def _normalize_recipients(
    recipients: list[str] | tuple[str, ...] | str | None,
    field_name: str,
) -> list[str]:
    if recipients is None:
        return []
    if isinstance(recipients, str):
        recipients = [recipients]
    normalized: list[str] = []
    for r in recipients:
        validated = _validate_address(str(r), field_name)
        normalized.append(validated)
    return normalized


@dataclass(slots=True)
class MailMessage:
    """Represents a validated, delivery-ready email message."""

    subject: str
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    from_address: str | None = None
    from_name: str | None = None
    reply_to: str | None = None
    text_body: str | None = None
    html_body: str | None = None
    template: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        # Validate subject
        if self.subject is None:
            raise MailValidationError("Email subject cannot be None")
        _validate_no_crlf(self.subject, "subject")

        # Normalize & validate recipient lists
        self.to = _normalize_recipients(self.to, "to")
        self.cc = _normalize_recipients(self.cc, "cc")
        self.bcc = _normalize_recipients(self.bcc, "bcc")

        # Validate from_address and from_name
        if self.from_address:
            self.from_address = _validate_address(self.from_address, "from_address")
        if self.from_name:
            _validate_no_crlf(self.from_name, "from_name")

        # Validate reply_to
        if self.reply_to:
            self.reply_to = _validate_address(self.reply_to, "reply_to")

        # Validate headers
        normalized_headers: dict[str, str] = {}
        for k, v in self.headers.items():
            _validate_no_crlf(k, f"header key '{k}'")
            _validate_no_crlf(v, f"header value for '{k}'")
            normalized_headers[k.strip()] = v.strip()
        self.headers = normalized_headers

    def attach(self, attachment: Attachment) -> Self:
        """Add an attachment to the message."""
        self.attachments.append(attachment)
        return self

    def attach_from_path(
        self,
        path: str | Path,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> Self:
        """Add a file attachment from a disk path."""
        self.attachments.append(
            Attachment.from_path(path, filename=filename, content_type=content_type)
        )
        return self

    def attach_from_bytes(
        self,
        content: bytes,
        filename: str,
        content_type: str | None = None,
    ) -> Self:
        """Add an in-memory byte attachment."""
        self.attachments.append(
            Attachment.from_bytes(content, filename=filename, content_type=content_type)
        )
        return self

    def add_header(self, key: str, value: str) -> Self:
        """Add a custom email header with CRLF injection validation."""
        _validate_no_crlf(key, f"header key '{key}'")
        _validate_no_crlf(value, f"header value for '{key}'")
        self.headers[key.strip()] = value.strip()
        return self

    def all_recipient_addresses(self) -> list[str]:
        """Extract just the pure email addresses from to, cc, and bcc."""
        recipients: list[str] = []
        for r in self.to + self.cc + self.bcc:
            _, addr = email.utils.parseaddr(r)
            recipients.append(addr if addr else r)
        return recipients


__all__ = ["MailMessage"]
