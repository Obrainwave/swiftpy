from __future__ import annotations

import logging
import logging.handlers
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_LOG_LEVEL_MAP: dict[str, int] = {
    "critical": logging.CRITICAL,
    "fatal": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "notset": logging.NOTSET,
}


class _WritableStream(Protocol):
    """Minimal writable text stream protocol for stream-backed log handlers."""

    def write(self, s: str, /) -> int: ...

    def flush(self) -> None: ...


def parse_log_level(level: int | str) -> int:
    """Parse a logging level name or integer into a stdlib numeric log level."""
    if isinstance(level, bool):
        raise TypeError("Log level cannot be a boolean.")
    if isinstance(level, int):
        if level < 0:
            raise ValueError(f"Log level must be non-negative, got {level}.")
        return level
    if isinstance(level, str):
        normalized = level.strip().lower()
        if normalized in _LOG_LEVEL_MAP:
            return _LOG_LEVEL_MAP[normalized]
        raise ValueError(f"Unsupported log level: {level!r}.")
    raise TypeError(
        f"Log level must be int or str, got {type(level).__name__}."
    )


def resolve_log_path(raw_path: str | Path, base_path: Path) -> Path:
    """Resolve a configured log file path against `base_path` when relative."""
    if not isinstance(raw_path, (str, Path)):
        raise TypeError(
            f"Log path must be str or Path, got {type(raw_path).__name__}."
        )
    path_str = str(raw_path)
    if not path_str.strip():
        raise ValueError("Log path cannot be empty.")
    if "\x00" in path_str:
        raise ValueError("Log path cannot contain NUL bytes.")

    candidate = Path(path_str)
    if candidate.is_absolute():
        return candidate
    return base_path / candidate


def build_formatter(channel_cfg: Mapping[str, Any]) -> logging.Formatter:
    """Create a stdlib formatter for a channel configuration."""
    raw_fmt = channel_cfg.get("format", DEFAULT_LOG_FORMAT)
    if not isinstance(raw_fmt, str) or not raw_fmt:
        raise ValueError("Log channel 'format' must be a non-empty string.")
    raw_datefmt = channel_cfg.get("datefmt")
    if raw_datefmt is not None and not isinstance(raw_datefmt, str):
        raise ValueError("Log channel 'datefmt' must be a string or None.")
    return logging.Formatter(fmt=raw_fmt, datefmt=raw_datefmt)


def _resolve_stream(raw_stream: Any) -> _WritableStream:
    """Resolve a stream option for the `stderr` driver."""
    if raw_stream is None or raw_stream == "stderr":
        return sys.stderr
    if raw_stream == "stdout":
        return sys.stdout
    if hasattr(raw_stream, "write") and callable(raw_stream.write):
        return cast(_WritableStream, raw_stream)
    raise ValueError(
        "Log channel 'stderr' stream must be 'stderr', 'stdout', or a writable text stream."
    )


def _require_non_bool_int(
    value: Any,
    *,
    field: str,
    minimum: int,
) -> int:
    """Validate integer channel configuration fields cleanly."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"Log configuration '{field}' must be an integer, got {type(value).__name__}."
        )
    if value < minimum:
        raise ValueError(
            f"Log configuration '{field}' must be >= {minimum}, got {value}."
        )
    return value


def build_target_handler(
    name: str,
    channel_cfg: Mapping[str, Any],
    *,
    default_level: int,
    base_path: Path,
) -> logging.Handler:
    """Construct the concrete stdlib `logging.Handler` for a leaf channel."""
    driver = channel_cfg.get("driver")
    if not isinstance(driver, str) or not driver.strip():
        raise ValueError(f"Log channel '{name}' must define a non-empty 'driver'.")
    normalized_driver = driver.strip().lower()

    raw_level = channel_cfg.get("level")
    level = parse_log_level(raw_level) if raw_level is not None else default_level
    formatter = build_formatter(channel_cfg)

    if normalized_driver == "single":
        target_path = resolve_log_path(
            channel_cfg.get("path", "storage/logs/swiftpy.log"),
            base_path,
        )
        encoding = str(channel_cfg.get("encoding", "utf-8"))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(target_path, encoding=encoding)
    elif normalized_driver == "daily":
        target_path = resolve_log_path(
            channel_cfg.get("path", "storage/logs/swiftpy.log"),
            base_path,
        )
        encoding = str(channel_cfg.get("encoding", "utf-8"))
        days = _require_non_bool_int(
            channel_cfg.get("days", 14),
            field=f"channels.{name}.days",
            minimum=0,
        )
        when = str(channel_cfg.get("when", "midnight"))
        interval = _require_non_bool_int(
            channel_cfg.get("interval", 1),
            field=f"channels.{name}.interval",
            minimum=1,
        )
        utc = bool(channel_cfg.get("utc", False))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(target_path),
            when=when,
            interval=interval,
            backupCount=days,
            encoding=encoding,
            utc=utc,
        )
    elif normalized_driver == "stderr":
        stream = _resolve_stream(channel_cfg.get("stream"))
        handler = logging.StreamHandler(stream)
    else:
        raise ValueError(
            f"Unsupported log driver {driver!r} for channel '{name}'."
        )

    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


__all__ = [
    "DEFAULT_LOG_FORMAT",
    "build_formatter",
    "build_target_handler",
    "parse_log_level",
    "resolve_log_path",
]
