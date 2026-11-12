from __future__ import annotations

import types
from collections.abc import Callable
from contextvars import ContextVar, Token
from typing import Any, Self

from swiftpy.mail.mailable import Mailable
from swiftpy.mail.manager import MailManager
from swiftpy.mail.message import MailMessage
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.queue.job import SendQueuedMailableJob
from swiftpy.mail.transports.base import MailTransport


class MailFake(MailTransport):
    """
    In-memory test double for SwiftPY Mail subsystem.
    Records sent messages and queued mailables with expressive assertion helpers.
    Supports task-local isolation via ContextVar and synchronous/asynchronous context managers.
    """

    def __init__(self) -> None:
        self.sent_messages: list[MailMessage] = []
        self.sent_mailables: list[Mailable] = []
        self.queued_mailables: list[dict[str, Any]] = []
        self._token: Token[MailFake | None] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        Mail.restore()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        Mail.restore()

    async def send(self, message: MailMessage) -> MailSendResult:
        self.sent_messages.append(message)
        return MailSendResult(
            success=True,
            message_id=f"fake-{len(self.sent_messages)}",
            provider="fake",
            accepted=list(message.to),
            rejected=[],
        )

    def record_queued(
        self,
        mailable: Mailable,
        queue: str | None = None,
        delay: float = 0,
        after_commit: bool = False,
    ) -> None:
        self.queued_mailables.append(
            {
                "mailable": mailable,
                "queue": queue or mailable.queue,
                "delay": delay,
                "after_commit": after_commit,
            }
        )

    def assert_sent(
        self,
        mailable_cls_or_filter: type[Mailable]
        | Callable[[MailMessage], bool]
        | None = None,
        recipient: str | None = None,
        count: int | None = None,
    ) -> None:
        """Assert that matching emails were sent."""
        matches: list[MailMessage] = []

        for msg in self.sent_messages:
            if recipient and recipient not in msg.all_recipient_addresses():
                continue
            if mailable_cls_or_filter is not None:
                if isinstance(mailable_cls_or_filter, type) and issubclass(
                    mailable_cls_or_filter, Mailable
                ):
                    matching_mailable = any(
                        isinstance(m, mailable_cls_or_filter)
                        for m in self.sent_mailables
                    )
                    if not matching_mailable:
                        continue
                elif not isinstance(mailable_cls_or_filter, type) and callable(
                    mailable_cls_or_filter
                ):
                    if not mailable_cls_or_filter(msg):
                        continue
            matches.append(msg)

        if count is not None:
            assert len(matches) == count, (
                f"Expected {count} emails matching criteria, found {len(matches)}."
            )
        else:
            assert len(matches) > 0, "Expected email was not sent."

    def assert_not_sent(
        self,
        mailable_cls_or_filter: type[Mailable]
        | Callable[[MailMessage], bool]
        | None = None,
        recipient: str | None = None,
    ) -> None:
        """Assert that no matching emails were sent."""
        for msg in self.sent_messages:
            if recipient and recipient in msg.all_recipient_addresses():
                raise AssertionError(
                    f"Unexpected email sent to recipient '{recipient}'"
                )
            if mailable_cls_or_filter is not None:
                if isinstance(mailable_cls_or_filter, type) and issubclass(
                    mailable_cls_or_filter, Mailable
                ):
                    if any(
                        isinstance(m, mailable_cls_or_filter)
                        for m in self.sent_mailables
                    ):
                        raise AssertionError(
                            f"Unexpected mailable of type '{mailable_cls_or_filter.__name__}' was sent"
                        )
                elif (
                    not isinstance(mailable_cls_or_filter, type)
                    and callable(mailable_cls_or_filter)
                    and mailable_cls_or_filter(msg)
                ):
                    raise AssertionError("Unexpected email matching predicate was sent")

    def assert_queued(
        self,
        mailable_cls: type[Mailable],
        count: int | None = None,
    ) -> None:
        """Assert that a mailable of the given class was queued."""
        matches = [
            item
            for item in self.queued_mailables
            if isinstance(item["mailable"], mailable_cls)
        ]
        if count is not None:
            assert len(matches) == count, (
                f"Expected {count} queued mailables of type '{mailable_cls.__name__}', found {len(matches)}."
            )
        else:
            assert len(matches) > 0, (
                f"Expected mailable '{mailable_cls.__name__}' was not queued."
            )

    def assert_nothing_sent(self) -> None:
        """Assert that zero emails were sent or queued."""
        assert len(self.sent_messages) == 0, (
            f"Expected no emails to be sent, but {len(self.sent_messages)} were sent."
        )
        assert len(self.queued_mailables) == 0, (
            f"Expected no mailables to be queued, but {len(self.queued_mailables)} were queued."
        )

    def reset(self) -> None:
        self.sent_messages.clear()
        self.sent_mailables.clear()
        self.queued_mailables.clear()


class Mail:
    """Class-level facade for SwiftPY Mail Subsystem."""

    _fake_var: ContextVar[MailFake | None] = ContextVar(
        "swiftpy_mail_fake", default=None
    )

    @classmethod
    def _manager(cls) -> MailManager:
        return MailManager.get_instance()

    @classmethod
    def fake(cls) -> MailFake:
        """Swap real delivery with the MailFake test double in the current task/context."""
        current = cls._fake_var.get()
        if current is None:
            fake_instance = MailFake()
            fake_instance._token = cls._fake_var.set(fake_instance)
            return fake_instance
        current.reset()
        return current

    @classmethod
    def restore(cls) -> None:
        """Restore real mail delivery after testing."""
        cls._fake_var.set(None)

    @classmethod
    def is_faked(cls) -> bool:
        return cls._fake_var.get() is not None

    @classmethod
    def mailer(cls, name: str | None = None) -> MailTransport:
        """Resolve a configured mail transport instance."""
        fake_instance = cls._fake_var.get()
        if fake_instance is not None:
            return fake_instance
        return cls._manager().mailer(name)

    @classmethod
    def extend(
        cls,
        driver_name: str,
        factory: Callable[[dict[str, Any]], MailTransport],
    ) -> None:
        """Register a custom mail transport."""
        cls._manager().extend(driver_name, factory)

    @classmethod
    async def send(
        cls,
        mailable_or_message: Mailable | MailMessage,
        mailer: str | None = None,
    ) -> MailSendResult:
        """Deliver an email immediately."""
        fake_instance = cls._fake_var.get()
        if fake_instance is not None:
            if isinstance(mailable_or_message, Mailable):
                fake_instance.sent_mailables.append(mailable_or_message)
                msg = await mailable_or_message.build_message()
            else:
                msg = mailable_or_message
            return await fake_instance.send(msg)

        if isinstance(mailable_or_message, Mailable):
            msg = await mailable_or_message.build_message()
            target_mailer = mailer or mailable_or_message.mailer_name
        else:
            msg = mailable_or_message
            target_mailer = mailer

        return await cls._manager().send(msg, mailer=target_mailer)

    @classmethod
    async def queue(
        cls,
        mailable: Mailable,
        queue: str | None = None,
        delay: float = 0,
        after_commit: bool = False,
    ) -> str:
        """Dispatch a mailable onto the background queue."""
        fake_instance = cls._fake_var.get()
        if fake_instance is not None:
            fake_instance.record_queued(
                mailable,
                queue=queue,
                delay=delay,
                after_commit=after_commit,
            )
            return f"fake-job-{len(fake_instance.queued_mailables)}"

        job = SendQueuedMailableJob(
            mailable=mailable,
            queue=queue or mailable.queue,
            delay=delay,
        )
        return await job.dispatch(
            queue=queue or mailable.queue,
            delay=delay,
            after_commit=after_commit,
        )


__all__ = ["Mail", "MailFake"]
