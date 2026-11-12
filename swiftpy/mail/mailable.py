from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any

from swiftpy.mail.message import MailMessage
from swiftpy.mail.templates.renderer import MailTemplateRenderer


class Mailable(ABC):
    """
    Abstract base class for reusable email definitions.
    Separates email composition and rendering from business logic.
    """

    mailer_name: str | None = None
    queue: str = "emails"
    tries: int = 3
    timeout: float | None = 60.0
    backoff: int | float = 5

    @abstractmethod
    def build(self) -> MailMessage | Any:
        """Construct the MailMessage representing this email."""

    async def build_message(self) -> MailMessage:
        """
        Execute build(), awaiting if asynchronous, apply template rendering if needed,
        and return the validated MailMessage.
        """
        result = self.build()
        if inspect.isawaitable(result):
            result = await result

        if not isinstance(result, MailMessage):
            raise TypeError(
                f"Mailable {self.__class__.__name__}.build() must return MailMessage, "
                f"got {type(result).__name__}"
            )

        if result.template:
            renderer = MailTemplateRenderer.get_instance()
            rendered = await renderer.render(result.template, result.context)
            if rendered.html and not result.html_body:
                result.html_body = rendered.html
            if rendered.text and not result.text_body:
                result.text_body = rendered.text

        return result


__all__ = ["Mailable"]
