from __future__ import annotations

import logging
from typing import Any

from swiftpy.logging.manager import ChannelLogger, LogManager


class Log:
    """Class-level facade for SwiftPY's structured asynchronous logging system."""

    @classmethod
    def _manager(cls) -> LogManager | None:
        return LogManager.get_instance()

    @classmethod
    def channel(cls, name: str | None = None) -> ChannelLogger:
        """Resolve a named channel logger, or the configured default channel."""
        manager = cls._manager()
        if manager is not None:
            return manager.channel(name)
        fallback_name = "swiftpy.app" if name is None else f"swiftpy.app.{name}"
        return ChannelLogger(
            logging.getLogger(fallback_name),
            channel_name=name or "app",
        )

    @classmethod
    def log(
        cls,
        level: int | str,
        message: object,
        *args: Any,
        **context: Any,
    ) -> None:
        """Write a log record to the default channel at the given level."""
        cls.channel().log(level, message, *args, **context)

    @classmethod
    def debug(cls, message: object, *args: Any, **context: Any) -> None:
        """Write a DEBUG log record to the default channel."""
        cls.channel().debug(message, *args, **context)

    @classmethod
    def info(cls, message: object, *args: Any, **context: Any) -> None:
        """Write an INFO log record to the default channel."""
        cls.channel().info(message, *args, **context)

    @classmethod
    def warning(cls, message: object, *args: Any, **context: Any) -> None:
        """Write a WARNING log record to the default channel."""
        cls.channel().warning(message, *args, **context)

    @classmethod
    def error(cls, message: object, *args: Any, **context: Any) -> None:
        """Write an ERROR log record to the default channel."""
        cls.channel().error(message, *args, **context)

    @classmethod
    def critical(cls, message: object, *args: Any, **context: Any) -> None:
        """Write a CRITICAL log record to the default channel."""
        cls.channel().critical(message, *args, **context)

    @classmethod
    def exception(cls, message: object, *args: Any, **context: Any) -> None:
        """Write an ERROR log record with exception traceback to the default channel."""
        cls.channel().exception(message, *args, **context)


__all__ = ["Log"]
