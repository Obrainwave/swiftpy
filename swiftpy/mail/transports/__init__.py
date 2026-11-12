from __future__ import annotations

from swiftpy.mail.transports.api.base import ApiMailTransport
from swiftpy.mail.transports.api.mailgun import MailgunTransport
from swiftpy.mail.transports.api.resend import ResendTransport
from swiftpy.mail.transports.api.sendgrid import SendGridTransport
from swiftpy.mail.transports.base import MailTransport
from swiftpy.mail.transports.console import ConsoleTransport
from swiftpy.mail.transports.sendmail import SendmailTransport
from swiftpy.mail.transports.smtp import SMTPTransport

__all__ = [
    "ApiMailTransport",
    "ConsoleTransport",
    "MailTransport",
    "MailgunTransport",
    "ResendTransport",
    "SMTPTransport",
    "SendGridTransport",
    "SendmailTransport",
]
