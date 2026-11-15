from __future__ import annotations

import logging

from swiftpy.queue.job import Job

logger = logging.getLogger(__name__)


class SendNotificationJob(Job):
    """
    Sample background queued job demonstrating asynchronous execution,
    retries, exponential backoff, and transactional dispatching.
    """

    tries: int = 3
    backoff: int = 5

    def __init__(self, user_id: int, message: str) -> None:
        self.user_id = user_id
        self.message = message

    async def handle(self) -> None:
        """Execute the notification dispatch logic."""
        logger.info(
            "Dispatching background notification to user %s: %s",
            self.user_id,
            self.message,
        )

