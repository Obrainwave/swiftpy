from __future__ import annotations

import email.utils
import html
import re
from email.message import EmailMessage

from swiftpy.mail.exceptions import (
    AttachmentTooLargeError,
    MailValidationError,
)
from swiftpy.mail.message import MailMessage

_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_plain_text(html_content: str) -> str:
    """Best-effort plain text conversion for HTML-only emails."""
    text = _TAG_RE.sub(" ", html_content)
    return html.unescape(" ".join(text.split()))


def _format_address(address: str, name: str | None = None) -> str:
    if name:
        return email.utils.formataddr((name, address))
    return address


class Envelope:
    """Builder converting a validated MailMessage into standard library EmailMessage."""

    @classmethod
    async def build(
        cls,
        message: MailMessage,
        default_from: tuple[str, str | None] | None = None,
        max_message_size_bytes: int | None = None,
        max_attachment_size_bytes: int | None = None,
    ) -> EmailMessage:
        """Construct an RFC 5322/2045 compliant EmailMessage asynchronously."""
        if not message.to and not message.cc and not message.bcc:
            raise MailValidationError(
                "Email message must have at least one recipient (to, cc, or bcc)"
            )

        msg = EmailMessage()

        # Subject
        msg["Subject"] = message.subject

        # From
        from_addr = message.from_address
        from_name = message.from_name
        if not from_addr and default_from:
            from_addr, default_name = default_from
            if not from_name:
                from_name = default_name

        if from_addr:
            msg["From"] = _format_address(from_addr, from_name)
        else:
            raise MailValidationError(
                "Email message must specify a from_address or have a default configured"
            )

        # To
        if message.to:
            msg["To"] = ", ".join(message.to)

        # Cc
        if message.cc:
            msg["Cc"] = ", ".join(message.cc)

        # Reply-To
        if message.reply_to:
            msg["Reply-To"] = message.reply_to

        # Date & Message-ID
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid()

        # Custom headers
        for k, v in message.headers.items():
            if k.lower() not in (
                "to",
                "from",
                "subject",
                "cc",
                "bcc",
                "reply-to",
                "date",
                "message-id",
            ):
                msg[k] = v

        # Body handling
        has_text = bool(message.text_body)
        has_html = bool(message.html_body)

        if has_text and has_html:
            msg.set_content(message.text_body or "")
            msg.add_alternative(message.html_body or "", subtype="html")
        elif has_html:
            # HTML only: add plain text fallback
            plain_fallback = _html_to_plain_text(message.html_body or "")
            msg.set_content(plain_fallback)
            msg.add_alternative(message.html_body or "", subtype="html")
        else:
            msg.set_content(message.text_body or "")

        # Process attachments
        inline_attachments = [
            a for a in message.attachments if a.disposition == "inline"
        ]
        regular_attachments = [
            a for a in message.attachments if a.disposition != "inline"
        ]

        # Add inline attachments to the html part if available
        html_part = msg.get_body(preferencelist=("html",))
        target_part = html_part if html_part is not None else msg

        for inline_att in inline_attachments:
            data = await inline_att.read_bytes(max_size=max_attachment_size_bytes)
            content_type = inline_att.content_type
            maintype, _, subtype = content_type.partition("/")
            cid = (
                f"<{inline_att.cid}>"
                if inline_att.cid and not inline_att.cid.startswith("<")
                else inline_att.cid
            )
            target_part.add_related(
                data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                cid=cid,
                disposition="inline",
                filename=inline_att.filename,
            )

        # Add regular attachments
        for att in regular_attachments:
            data = await att.read_bytes(max_size=max_attachment_size_bytes)
            content_type = att.content_type
            maintype, _, subtype = content_type.partition("/")
            msg.add_attachment(
                data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=att.filename,
            )

        if max_message_size_bytes is not None:
            raw_bytes = msg.as_bytes()
            if len(raw_bytes) > max_message_size_bytes:
                raise AttachmentTooLargeError(
                    f"Total email size ({len(raw_bytes)} bytes) exceeds configured limit ({max_message_size_bytes} bytes)"
                )

        return msg


__all__ = ["Envelope"]
