from __future__ import annotations

import types
from collections.abc import Callable
from contextvars import ContextVar, Token
from typing import Any, Self, TypedDict

from swiftpy.mail.mailable import Mailable
from swiftpy.mail.manager import MailManager
from swiftpy.mail.message import MailMessage
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.queue.job import SendQueuedMailableJob
from swiftpy.mail.transports.base import MailTransport


class _SentMailRecord(TypedDict):
    """Correlated record of a message captured by MailFake."""

    message: MailMessage
    mailable: Mailable | None
    mailer: str | None


class MailFake(MailTransport):
    """
    In-memory test double for SwiftPY Mail subsystem.
    Records sent messages and queued mailables with expressive assertion helpers.
    Supports task-local isolation via ContextVar and synchronous/asynchronous context managers.
    """

    def __init__(self) -> None:
        self.sent_messages: list[MailMessage] = []
        self.sent_mailables: list[Mailable] = []
        self.sent_records: list[_SentMailRecord] = []
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

    async def send(
        self,
        message: MailMessage,
        *,
        mailable: Mailable | None = None,
        mailer: str | None = None,
    ) -> MailSendResult:
        self.sent_messages.append(message)
        if mailable is not None:
            self.sent_mailables.append(mailable)
        self.sent_records.append(
            {
                "message": message,
                "mailable": mailable,
                "mailer": mailer,
            }
        )
        return MailSendResult(
            success=True,
            message_id=f"fake-{len(self.sent_messages)}",
            provider="fake",
            accepted=message.all_recipient_addresses(),
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

    @staticmethod
    def _record_matches(
        record: _SentMailRecord,
        mailable_cls_or_filter: type[Mailable]
        | Callable[[MailMessage], bool]
        | None,
        recipient: str | None,
    ) -> tuple[bool, str | None]:
        msg = record["message"]
        if recipient and recipient not in msg.all_recipient_addresses():
            return False, None

        if mailable_cls_or_filter is not None:
            if isinstance(mailable_cls_or_filter, type) and issubclass(
                mailable_cls_or_filter, Mailable
            ):
                if not isinstance(record["mailable"], mailable_cls_or_filter):
                    return False, None
                suffix = f" to recipient '{recipient}'" if recipient else ""
                return (
                    True,
                    f"Unexpected mailable of type '{mailable_cls_or_filter.__name__}' was sent{suffix}",
                )
            if not isinstance(mailable_cls_or_filter, type) and callable(
                mailable_cls_or_filter
            ):
                if not mailable_cls_or_filter(msg):
                    return False, None
                suffix = f" to recipient '{recipient}'" if recipient else ""
                return True, f"Unexpected email matching predicate was sent{suffix}"

        if recipient:
            return True, f"Unexpected email sent to recipient '{recipient}'"
        return True, "Unexpected email was sent"

    def assert_sent(
        self,
        mailable_cls_or_filter: type[Mailable]
        | Callable[[MailMessage], bool]
        | None = None,
        recipient: str | None = None,
        count: int | None = None,
    ) -> None:
        """Assert that matching emails were sent."""
        matches = [
            record["message"]
            for record in self.sent_records
            if self._record_matches(record, mailable_cls_or_filter, recipient)[0]
        ]

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
        for record in self.sent_records:
            matched, reason = self._record_matches(
                record, mailable_cls_or_filter, recipient
            )
            if matched:
                raise AssertionError(reason)

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
        self.sent_records.clear()
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
        if isinstance(mailable_or_message, Mailable):
            msg = await mailable_or_message.build_message()
            target_mailer = mailer or mailable_or_message.mailer_name
            sent_mailable: Mailable | None = mailable_or_message
        else:
            msg = mailable_or_message
            target_mailer = mailer
            sent_mailable = None

        fake_instance = cls._fake_var.get()
        if fake_instance is not None:
            return await fake_instance.send(
                msg, mailable=sent_mailable, mailer=target_mailer
            )

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
