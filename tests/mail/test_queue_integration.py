from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from swiftpy.core.config import Config
from swiftpy.database.transaction import _active_connection
from swiftpy.mail.exceptions import (
    MailAuthenticationError,
    MailConfigurationError,
    MailConnectionError,
    MailRateLimitError,
    MailTimeoutError,
    MailValidationError,
)
from swiftpy.mail.facade import Mail
from swiftpy.mail.mailable import Mailable
from swiftpy.mail.manager import MailManager
from swiftpy.mail.message import MailMessage
from swiftpy.mail.queue.job import SendQueuedMailableJob
from swiftpy.mail.transports.base import MailTransport
from swiftpy.mail.transports.console import ConsoleTransport
from swiftpy.queue.job import Job, set_dispatch_handler


class SampleMailable(Mailable):
    queue = "custom_emails"
    tries = 5
    backoff = 10

    def __init__(self, to_addr: str, note: str) -> None:
        self.to_addr = to_addr
        self.note = note

    def build(self) -> MailMessage:
        return MailMessage(
            subject="Queued Sample",
            to=[self.to_addr],
            text_body=f"Note: {self.note}",
        )


class MockTxConnection:
    """Mock database transaction connection supporting on_commit hooks."""

    def __init__(self) -> None:
        self.closed = False
        self._callbacks: list[Callable[[], Any]] = []

    def on_commit(self, callback: Callable[[], Any]) -> None:
        self._callbacks.append(callback)

    async def commit(self) -> None:
        for cb in self._callbacks:
            res = cb()
            if hasattr(res, "__await__"):
                await res
        self._callbacks.clear()

    async def rollback(self) -> None:
        self._callbacks.clear()


@pytest.mark.asyncio
async def test_queued_mailable_job_serialization() -> None:
    mailable = SampleMailable("user@domain.com", "Important")
    job = SendQueuedMailableJob(mailable=mailable)

    assert job.queue == "custom_emails"
    assert job.tries == 5
    assert job.backoff == 10

    payload = job.serialize()
    assert payload["job_class"] == "swiftpy.mail.queue.job:SendQueuedMailableJob"
    assert "mailable_class" in payload["data"]
    assert payload["data"]["mailable_state"]["to_addr"] == "user@domain.com"
    assert payload["data"]["mailable_state"]["note"] == "Important"


@pytest.mark.asyncio
async def test_queued_mailable_job_execution() -> None:
    config = Config(
        {
            "mail": {
                "default": "console",
                "mailers": {
                    "console": {"transport": "console"},
                },
            }
        }
    )
    MailManager._instance = MailManager(config=config)

    mailable = SampleMailable("exec@domain.com", "Exec test")
    job = SendQueuedMailableJob(mailable=mailable)

    # Execute job
    await job.handle()

    transport = MailManager.get_instance().mailer()
    assert isinstance(transport, ConsoleTransport)
    assert len(transport.sent_messages) == 1
    assert transport.sent_messages[0].to == ["exec@domain.com"]
    body = transport.sent_messages[0].text_body
    assert body is not None and "Note: Exec test" in body

    # Test full deserialize & handle cycle from serialized dictionary
    payload = job.serialize()
    restored_job = Job.deserialize(payload)
    assert isinstance(restored_job, SendQueuedMailableJob)
    await restored_job.handle()

    assert len(transport.sent_messages) == 2
    assert transport.sent_messages[1].to == ["exec@domain.com"]

    MailManager._instance = None


@pytest.mark.asyncio
async def test_queue_transaction_commit_safety() -> None:
    dispatched_jobs: list[Job] = []

    def mock_dispatch(job: Job, queue: str | None, delay: float) -> str:
        dispatched_jobs.append(job)
        return job.job_id

    set_dispatch_handler(mock_dispatch)

    mock_conn = MockTxConnection()
    token = _active_connection.set(mock_conn)

    try:
        mailable = SampleMailable("tx@domain.com", "Commit test")

        # Dispatch with after_commit=True while transaction is active
        await Mail.queue(mailable, after_commit=True)

        # Before commit: job MUST NOT be in the dispatched jobs list
        assert len(dispatched_jobs) == 0

        # Simulate transaction commit
        await mock_conn.commit()

        # After commit: job MUST now be dispatched
        assert len(dispatched_jobs) == 1
        assert isinstance(dispatched_jobs[0], SendQueuedMailableJob)
    finally:
        _active_connection.reset(token)
        set_dispatch_handler(None)


@pytest.mark.asyncio
async def test_queue_transaction_rollback_safety() -> None:
    dispatched_jobs: list[Job] = []

    def mock_dispatch(job: Job, queue: str | None, delay: float) -> str:
        dispatched_jobs.append(job)
        return job.job_id

    set_dispatch_handler(mock_dispatch)

    mock_conn = MockTxConnection()
    token = _active_connection.set(mock_conn)

    try:
        mailable = SampleMailable("rollback@domain.com", "Rollback test")

        # Dispatch with after_commit=True
        await Mail.queue(mailable, after_commit=True)
        assert len(dispatched_jobs) == 0

        # Simulate transaction rollback
        await mock_conn.rollback()

        # After rollback: job was discarded and MUST NOT be dispatched
        assert len(dispatched_jobs) == 0
    finally:
        _active_connection.reset(token)
        set_dispatch_handler(None)


@pytest.mark.asyncio
async def test_queue_after_commit_outside_transaction() -> None:
    dispatched_jobs: list[Job] = []

    def mock_dispatch(job: Job, queue: str | None, delay: float) -> str:
        dispatched_jobs.append(job)
        return job.job_id

    set_dispatch_handler(mock_dispatch)

    # Ensure no active database transaction
    assert _active_connection.get() is None

    try:
        mailable = SampleMailable("direct@domain.com", "No TX test")
        await Mail.queue(mailable, after_commit=True)

        # Dispatches immediately when outside an active transaction
        assert len(dispatched_jobs) == 1
    finally:
        set_dispatch_handler(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transient_exc",
    [
        MailConnectionError("Socket reset by peer"),
        MailRateLimitError("Rate limit exceeded", retry_after=30.0),
        MailTimeoutError("Transmission timed out"),
    ],
)
async def test_queue_transient_errors_trigger_retry(
    transient_exc: Exception,
) -> None:
    mock_transport = AsyncMock(spec=MailTransport)
    mock_transport.send.side_effect = transient_exc

    manager = MailManager()
    manager._transports["mock"] = mock_transport
    MailManager._instance = manager

    mailable = SampleMailable("retry@domain.com", "Retry test")
    mailable.mailer_name = "mock"

    job = SendQueuedMailableJob(mailable=mailable)

    # Transient error MUST be re-raised to allow the queue worker to schedule a retry
    with pytest.raises(type(transient_exc)):
        await job.handle()

    MailManager._instance = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permanent_exc",
    [
        MailValidationError("Invalid recipient address syntax"),
        MailAuthenticationError("Invalid API credentials"),
        MailConfigurationError("Missing required configuration"),
    ],
)
async def test_queue_permanent_errors_fail_fast(
    permanent_exc: Exception,
) -> None:
    mock_transport = AsyncMock(spec=MailTransport)
    mock_transport.send.side_effect = permanent_exc

    manager = MailManager()
    manager._transports["mock"] = mock_transport
    MailManager._instance = manager

    mailable = SampleMailable("permanent@domain.com", "Fail fast test")
    mailable.mailer_name = "mock"

    job = SendQueuedMailableJob(mailable=mailable)
    job.failed = AsyncMock()  # type: ignore[method-assign]

    # Permanent error MUST NOT be re-raised; it calls failed() and fails fast
    await job.handle()
    job.failed.assert_awaited_once_with(permanent_exc)

    MailManager._instance = None
