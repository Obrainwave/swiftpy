from __future__ import annotations

from swiftpy.mail.attachments import Attachment
from swiftpy.mail.envelope import Envelope
from swiftpy.mail.exceptions import (
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    HeaderInjectionError,
    InvalidAddressError,
    MailAttachmentError,
    MailAuthenticationError,
    MailConfigurationError,
    MailConnectionError,
    MailDeliveryError,
    MailerNotFoundError,
    MailError,
    MailRateLimitError,
    MailTimeoutError,
    MailTransportError,
    MailValidationError,
)
from swiftpy.mail.facade import Mail, MailFake
from swiftpy.mail.mailable import Mailable
from swiftpy.mail.manager import MailManager
from swiftpy.mail.message import MailMessage
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.base import MailTransport
from swiftpy.mail.transports.console import ConsoleTransport
from swiftpy.mail.transports.smtp import SMTPTransport

__all__ = [
    "Attachment",
    "AttachmentNotFoundError",
    "AttachmentTooLargeError",
    "ConsoleTransport",
    "Envelope",
    "HeaderInjectionError",
    "InvalidAddressError",
    "Mail",
    "MailAttachmentError",
    "MailAuthenticationError",
    "MailConfigurationError",
    "MailConnectionError",
    "MailDeliveryError",
    "MailError",
    "MailFake",
    "MailManager",
    "MailMessage",
    "MailRateLimitError",
    "MailSendResult",
    "MailTimeoutError",
    "MailTransport",
    "MailTransportError",
    "MailValidationError",
    "Mailable",
    "MailerNotFoundError",
    "SMTPTransport",
]
