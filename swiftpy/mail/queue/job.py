from __future__ import annotations

import importlib
import logging
from collections.abc import Sequence
from typing import Any

from swiftpy.mail.exceptions import (
    MailAuthenticationError,
    MailConfigurationError,
    MailConnectionError,
    MailRateLimitError,
    MailTimeoutError,
    MailTransportError,
    MailValidationError,
)
from swiftpy.mail.mailable import Mailable
from swiftpy.mail.manager import MailManager
from swiftpy.queue.job import Job

logger = logging.getLogger("swiftpy.mail.queue")


class SendQueuedMailableJob(Job):
    """
    Asynchronous background job for delivering a Mailable.
    Handles serialization, execution, and retry semantics.
    """

    queue = "emails"
    tries = 3
    backoff: int | float | Sequence[int | float] = (5, 15, 60)

    def __init__(self, mailable: Mailable | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.mailable = mailable

        if mailable is not None:
            self.queue = getattr(mailable, "queue", self.queue)
            self.tries = getattr(mailable, "tries", self.tries)
            self.backoff = getattr(mailable, "backoff", self.backoff)
            self.timeout = getattr(mailable, "timeout", self.timeout)

            # Store mailable class and state in data
            self.data["mailable_class"] = (
                f"{mailable.__class__.__module__}:{mailable.__class__.__qualname__}"
            )
            # Store instance attributes that can be pickled/json-encoded
            state: dict[str, Any] = {}
            for k, v in vars(mailable).items():
                if not k.startswith("_"):
                    state[k] = v
            self.data["mailable_state"] = state
            self.data["mailer_name"] = mailable.mailer_name

    def _resolve_mailable(self) -> Mailable:
        if self.mailable is not None:
            return self.mailable

        mailable_class_str = self.data.get("mailable_class")
        if not mailable_class_str or not isinstance(mailable_class_str, str):
            raise ValueError(
                "SendQueuedMailableJob missing 'mailable_class' in payload"
            )

        if ":" in mailable_class_str:
            mod_name, cls_name = mailable_class_str.split(":", 1)
        else:
            mod_name, cls_name = mailable_class_str.rsplit(".", 1)

        module = importlib.import_module(mod_name)
        target_cls: type[Mailable] = getattr(module, cls_name)

        if not issubclass(target_cls, Mailable):
            raise TypeError(
                f"Class '{mailable_class_str}' is not a subclass of Mailable"
            )

        # Instantiate without calling __init__ directly if constructor requires args,
        # then restore attributes from state dictionary.
        instance = target_cls.__new__(target_cls)
        state = self.data.get("mailable_state", {})
        if isinstance(state, dict):
            vars(instance).update(state)

        mailer_name = self.data.get("mailer_name")
        if mailer_name:
            instance.mailer_name = str(mailer_name)

        self.mailable = instance
        return instance

    async def handle(self) -> None:
        mailable = self._resolve_mailable()
        message = await mailable.build_message()

        manager = MailManager.get_instance()
        transport = manager.mailer(mailable.mailer_name)

        try:
            result = await transport.send(message)
            if not result.success:
                raise MailTransportError(
                    f"Email delivery via {result.provider} was not successful: rejected={result.rejected}"
                )
        except (MailConnectionError, MailRateLimitError, MailTimeoutError) as exc:
            # Transient error: re-raise to trigger queue retry
            logger.warning(
                "Transient delivery failure for mailable %s: %s (attempt %d/%d)",
                mailable.__class__.__name__,
                exc,
                self.attempts,
                self.tries,
            )
            raise
        except (
            MailValidationError,
            MailAuthenticationError,
            MailConfigurationError,
        ) as exc:
            # Permanent error: do not retry
            logger.exception(
                "Permanent delivery failure for mailable %s",
                mailable.__class__.__name__,
            )
            # Do not re-raise to avoid infinite retry loops on non-recoverable errors
            await self.failed(exc)

    async def failed(self, exception: BaseException) -> None:
        logger.error(
            "Permanent failure delivering mailable %s: %s",
            self.mailable.__class__.__name__ if self.mailable else "unknown",
            exception,
        )


__all__ = ["SendQueuedMailableJob"]
