from __future__ import annotations

import asyncio
from contextvars import copy_context

import pytest

from swiftpy.mail.facade import Mail
from swiftpy.mail.mailable import Mailable
from swiftpy.mail.message import MailMessage


class NotificationEmail(Mailable):
    def __init__(self, recipient: str, title: str) -> None:
        self.recipient = recipient
        self.title = title

    def build(self) -> MailMessage:
        return MailMessage(
            subject=self.title,
            to=[self.recipient],
            text_body=f"Details for {self.title}",
        )


@pytest.mark.asyncio
async def test_mail_fake_workflow() -> None:
    fake = Mail.fake()
    assert Mail.is_faked() is True

    fake.assert_nothing_sent()

    await Mail.send(NotificationEmail("alice@example.com", "Security Alert"))

    # Assertions
    fake.assert_sent(NotificationEmail)
    fake.assert_sent(NotificationEmail, recipient="alice@example.com", count=1)
    fake.assert_sent(lambda msg: "Security Alert" in msg.subject)
    fake.assert_not_sent(recipient="bob@example.com")

    with pytest.raises(AssertionError):
        fake.assert_sent(NotificationEmail, recipient="bob@example.com")

    Mail.restore()
    assert Mail.is_faked() is False


@pytest.mark.asyncio
async def test_mail_fake_queue_workflow() -> None:
    fake = Mail.fake()
    mailable = NotificationEmail("alice@example.com", "Queued Job Alert")

    job_id = await Mail.queue(mailable)
    assert job_id.startswith("fake-job")

    fake.assert_queued(NotificationEmail, count=1)

    with pytest.raises(AssertionError):
        fake.assert_queued(NotificationEmail, count=2)

    class OtherMailable(Mailable):
        def build(self) -> MailMessage:
            return MailMessage(subject="Other", to=["o@example.com"])

    with pytest.raises(AssertionError):
        fake.assert_queued(OtherMailable)

    with pytest.raises(AssertionError):
        fake.assert_nothing_sent()

    Mail.restore()


@pytest.mark.asyncio
async def test_mail_fake_assert_not_sent_failures() -> None:
    fake = Mail.fake()
    await Mail.send(NotificationEmail("alice@example.com", "Alert 1"))

    with pytest.raises(
        AssertionError, match="Unexpected email sent to recipient 'alice@example.com'"
    ):
        fake.assert_not_sent(recipient="alice@example.com")

    with pytest.raises(
        AssertionError, match="Unexpected mailable of type 'NotificationEmail' was sent"
    ):
        fake.assert_not_sent(NotificationEmail)

    with pytest.raises(
        AssertionError, match="Unexpected email matching predicate was sent"
    ):
        fake.assert_not_sent(lambda msg: "Alert 1" in msg.subject)

    with pytest.raises(AssertionError, match="Expected no emails to be sent"):
        fake.assert_nothing_sent()

    Mail.restore()


@pytest.mark.asyncio
async def test_mail_fake_sync_context_manager() -> None:
    assert Mail.is_faked() is False

    with Mail.fake() as fake:
        assert Mail.is_faked() is True
        await Mail.send(NotificationEmail("user@example.com", "Inside Context"))
        fake.assert_sent(NotificationEmail, count=1)

    assert Mail.is_faked() is False


@pytest.mark.asyncio
async def test_mail_fake_async_context_manager() -> None:
    assert Mail.is_faked() is False

    async with Mail.fake() as fake:
        assert Mail.is_faked() is True
        await Mail.send(NotificationEmail("async@example.com", "Async Context"))
        fake.assert_sent(NotificationEmail, count=1)

    assert Mail.is_faked() is False


@pytest.mark.asyncio
async def test_mail_fake_context_manager_exception_restores() -> None:
    assert Mail.is_faked() is False

    with pytest.raises(ValueError, match="boom"), Mail.fake() as fake:
        assert Mail.is_faked() is True
        fake.assert_nothing_sent()
        raise ValueError("boom")

    assert Mail.is_faked() is False


@pytest.mark.asyncio
async def test_mail_fake_concurrent_task_isolation() -> None:
    Mail.restore()

    async def task_with_fake(task_id: int) -> int:
        # Create isolated context
        ctx = copy_context()

        async def _run() -> int:
            with Mail.fake() as fake:
                await Mail.send(
                    NotificationEmail(f"user{task_id}@example.com", f"Task {task_id}")
                )
                await asyncio.sleep(0.01)
                return len(fake.sent_messages)

        return await ctx.run(_run)

    results = await asyncio.gather(
        task_with_fake(1),
        task_with_fake(2),
        task_with_fake(3),
    )

    # Each task had exactly 1 message isolated to its own context
    assert list(results) == [1, 1, 1]
    assert Mail.is_faked() is False
