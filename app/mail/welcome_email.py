from __future__ import annotations

from swiftpy.mail.mailable import Mailable
from swiftpy.mail.message import MailMessage


class WelcomeEmail(Mailable):
    """
    Sample transactional email sent to newly registered users.
    Renders views/mail/template.html with user context data.
    """

    def __init__(self, name: str, to: str | list[str] | None = None) -> None:
        super().__init__()
        self.name = name
        self.to_address = [to] if isinstance(to, str) else (to or [])

    def build(self) -> MailMessage:
        return MailMessage(
            subject=f"Welcome to SwiftPY, {self.name}!",
            to=self.to_address,
            template="views/mail/template.html",
            context={
                "title": f"Welcome, {self.name}!",
                "content": "Thank you for joining our platform. We are thrilled to have you here!",
                "app_name": "SwiftPY",
            },
        )

