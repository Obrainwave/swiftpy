from __future__ import annotations

import logging

from app.events.user_registered import UserRegistered

logger = logging.getLogger(__name__)


class SendWelcomeNotification:
    """
    Event listener that sends a welcome notification or queued job
    when a UserRegistered event is dispatched.
    """

    async def handle(self, event: UserRegistered) -> None:
        logger.info(
            "Handling UserRegistered event for %s (%s)",
            event.name,
            event.email,
        )

