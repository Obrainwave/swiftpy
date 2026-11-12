from __future__ import annotations

import builtins


class MailError(Exception):
    """Base exception for all SwiftPY email subsystem errors."""


class MailConfigurationError(MailError, ValueError):
    """Raised when mailer or transport configuration is invalid or missing."""


class MailerNotFoundError(MailConfigurationError, KeyError):
    """Raised when an unconfigured mailer name is requested."""


class MailValidationError(MailError, ValueError):
    """Raised when an email message fails validation."""


class HeaderInjectionError(MailValidationError):
    """Raised when CRLF or null bytes are detected in email headers or addresses."""


class InvalidAddressError(MailValidationError):
    """Raised when an email address does not conform to RFC 5322 structure."""


class MailAttachmentError(MailError):
    """Base exception for attachment errors."""


class AttachmentNotFoundError(MailAttachmentError, builtins.FileNotFoundError):
    """Raised when an attachment file does not exist on disk."""


class AttachmentTooLargeError(MailAttachmentError):
    """Raised when an attachment or total message exceeds configured size limits."""


class MailTransportError(MailError):
    """Base exception for network, provider, or delivery failures."""


class MailAuthenticationError(MailTransportError):
    """Raised when SMTP or API credentials are rejected."""


class MailConnectionError(MailTransportError):
    """Raised when connecting to the SMTP server or HTTP API endpoint fails."""


class MailTimeoutError(MailConnectionError, TimeoutError):
    """Raised when connection or transmission times out."""


class MailRateLimitError(MailTransportError):
    """Raised when provider API rate limits are exceeded (e.g. HTTP 429)."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MailDeliveryError(MailTransportError):
    """Raised when the mail server rejects one or more recipients."""


__all__ = [
    "AttachmentNotFoundError",
    "AttachmentTooLargeError",
    "HeaderInjectionError",
    "InvalidAddressError",
    "MailAttachmentError",
    "MailAuthenticationError",
    "MailConfigurationError",
    "MailConnectionError",
    "MailDeliveryError",
    "MailError",
    "MailRateLimitError",
    "MailTimeoutError",
    "MailTransportError",
    "MailValidationError",
    "MailerNotFoundError",
]
