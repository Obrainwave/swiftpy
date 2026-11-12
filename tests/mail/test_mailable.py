from __future__ import annotations

from pathlib import Path

import pytest

from swiftpy.mail.mailable import Mailable
from swiftpy.mail.message import MailMessage
from swiftpy.mail.templates.renderer import MailTemplateRenderer


class UserWelcomeMailable(Mailable):
    def __init__(self, username: str, email: str) -> None:
        self.username = username
        self.email = email

    def build(self) -> MailMessage:
        return MailMessage(
            subject=f"Welcome {self.username}",
            to=[self.email],
            text_body=f"Hello {self.username}!",
        )


class AsyncBuildMailable(Mailable):
    async def build(self) -> MailMessage:
        return MailMessage(
            subject="Async Subject",
            to=["user@example.com"],
            text_body="Async Body",
        )


class InvalidReturnMailable(Mailable):
    def build(self) -> str:
        return "not a MailMessage"


@pytest.mark.asyncio
async def test_mailable_sync_build() -> None:
    mailable = UserWelcomeMailable("Charlie", "charlie@example.com")
    msg = await mailable.build_message()
    assert msg.subject == "Welcome Charlie"
    assert msg.to == ["charlie@example.com"]
    assert msg.text_body == "Hello Charlie!"


@pytest.mark.asyncio
async def test_mailable_async_build() -> None:
    mailable = AsyncBuildMailable()
    msg = await mailable.build_message()
    assert msg.subject == "Async Subject"
    assert msg.text_body == "Async Body"


@pytest.mark.asyncio
async def test_mailable_invalid_return_raises() -> None:
    mailable = InvalidReturnMailable()
    with pytest.raises(TypeError, match="must return MailMessage"):
        await mailable.build_message()


@pytest.mark.asyncio
async def test_mailable_with_template(tmp_path: Path) -> None:
    template_dir = tmp_path / "views"
    template_dir.mkdir()
    (template_dir / "notification.html").write_text(
        "<p>Hello {{ name }}</p>", encoding="utf-8"
    )

    # Set singleton template dir for test
    renderer = MailTemplateRenderer(template_dir=template_dir)
    MailTemplateRenderer._instance = renderer

    class TemplateMailable(Mailable):
        def build(self) -> MailMessage:
            return MailMessage(
                subject="Notice",
                to=["user@example.com"],
                template="notification",
                context={"name": "David"},
            )

    mailable = TemplateMailable()
    msg = await mailable.build_message()
    assert msg.html_body == "<p>Hello David</p>"
    MailTemplateRenderer._instance = None
