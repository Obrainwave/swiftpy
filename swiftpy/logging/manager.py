from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import queue
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from swiftpy.core.config import Config
from swiftpy.core.container import get_container
from swiftpy.logging.channels import build_target_handler, parse_log_level

_DEFAULT_QUEUE_SIZE = 10_000


class _SwiftpyLoggerCoordinator:
    """Process-level level coordinator for the shared stdlib `'swiftpy'` logger."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_levels: dict[int, int] = {}
        self._saved_original_level: int | None = None

    def attach(self, manager_id: int, level: int) -> None:
        with self._lock:
            swiftpy_logger = logging.getLogger("swiftpy")
            if not self._active_levels:
                self._saved_original_level = swiftpy_logger.level
            self._active_levels[manager_id] = level
            swiftpy_logger.setLevel(min(self._active_levels.values()))

    def detach(self, manager_id: int) -> None:
        with self._lock:
            if manager_id not in self._active_levels:
                return
            del self._active_levels[manager_id]
            swiftpy_logger = logging.getLogger("swiftpy")
            if self._active_levels:
                swiftpy_logger.setLevel(min(self._active_levels.values()))
            else:
                restored = (
                    self._saved_original_level
                    if self._saved_original_level is not None
                    else logging.NOTSET
                )
                swiftpy_logger.setLevel(restored)
                self._saved_original_level = None


_SWIFTPY_COORDINATOR = _SwiftpyLoggerCoordinator()


def _resolve_active_manager_or_none() -> LogManager | None:
    """Resolve the active container's `LogManager` without raising when unbootstrapped."""
    try:
        container = get_container()
    except RuntimeError:
        return None
    if container.has(LogManager):
        existing = container.get_singleton(LogManager)
        if isinstance(existing, LogManager):
            return existing
        resolved = container.resolve(LogManager)
        if isinstance(resolved, LogManager):
            return resolved
    return None


class _SwiftpyActiveManagerFilter(logging.Filter):
    """Filter keeping framework `'swiftpy.*'` log routing isolated per active `Application`."""

    def __init__(self, manager: LogManager, min_level: int) -> None:
        super().__init__()
        self._manager = manager
        self._min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < self._min_level:
            return False
        active = _resolve_active_manager_or_none()
        return not (
            active is not None and active is not self._manager and active.is_started
        )


class _ChannelDisplayNameFilter(logging.Filter):
    """Replace internal per-manager logger name with clean public channel name."""

    def __init__(self, display_name: str) -> None:
        super().__init__()
        self._display_name = display_name

    def filter(self, record: logging.LogRecord) -> bool:
        record.name = self._display_name
        return True


_REDACTED_CONTEXT_VALUE = "[REDACTED]"
_SENSITIVE_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "key_hash",
        "password",
        "password_hash",
        "pepper",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "token",
    }
)


def _sanitize_context_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): (
                _REDACTED_CONTEXT_VALUE
                if str(k).strip().lower() in _SENSITIVE_CONTEXT_KEYS
                else _sanitize_context_value(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_context_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_context_value(item) for item in value)
    return value


def _sanitize_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Redact sensitive credential keys recursively from structured log context."""
    return {
        str(k): (
            _REDACTED_CONTEXT_VALUE
            if str(k).strip().lower() in _SENSITIVE_CONTEXT_KEYS
            else _sanitize_context_value(v)
        )
        for k, v in context.items()
    }


def _serialize_context(context: dict[str, Any]) -> str:
    """Serialize structured log context deterministically without raising."""
    return json.dumps(context, default=str, ensure_ascii=False)


class ChannelLogger:
    """Structured logger bound to a resolved stdlib `logging.Logger` channel."""

    def __init__(self, logger: logging.Logger, channel_name: str = "app") -> None:
        self._logger = logger
        self.channel_name = channel_name

    def log(
        self,
        level: int | str,
        message: object,
        *args: Any,
        **context: Any,
    ) -> None:
        """Emit a log record at `level` with optional positional args and keyword context."""
        numeric_level = parse_log_level(level)
        if not self._logger.isEnabledFor(numeric_level):
            return

        ctx = dict(context)
        exc_info = ctx.pop("exc_info", None)
        stack_info = bool(ctx.pop("stack_info", False))

        if ctx:
            sanitized_ctx = _sanitize_context(ctx)
            rendered = (
                message % args
                if args and isinstance(message, str)
                else str(message)
            )
            payload = f"{rendered} {_serialize_context(sanitized_ctx)}"
            self._logger.log(
                numeric_level,
                payload,
                exc_info=exc_info,
                stack_info=stack_info,
                extra={"swiftpy_context": sanitized_ctx},
            )
            return

        self._logger.log(
            numeric_level,
            message,
            *args,
            exc_info=exc_info,
            stack_info=stack_info,
        )

    def debug(self, message: object, *args: Any, **context: Any) -> None:
        """Log a DEBUG message."""
        self.log(logging.DEBUG, message, *args, **context)

    def info(self, message: object, *args: Any, **context: Any) -> None:
        """Log an INFO message."""
        self.log(logging.INFO, message, *args, **context)

    def warning(self, message: object, *args: Any, **context: Any) -> None:
        """Log a WARNING message."""
        self.log(logging.WARNING, message, *args, **context)

    def error(self, message: object, *args: Any, **context: Any) -> None:
        """Log an ERROR message."""
        self.log(logging.ERROR, message, *args, **context)

    def critical(self, message: object, *args: Any, **context: Any) -> None:
        """Log a CRITICAL message."""
        self.log(logging.CRITICAL, message, *args, **context)

    def exception(self, message: object, *args: Any, **context: Any) -> None:
        """Log an ERROR message including exception traceback information."""
        ctx = dict(context)
        ctx.setdefault("exc_info", True)
        self.log(logging.ERROR, message, *args, **ctx)


@dataclass(slots=True)
class _LeafChannelRuntime:
    name: str
    level: int
    log_queue: queue.Queue[logging.LogRecord]
    queue_handler: logging.handlers.QueueHandler
    target_handler: logging.Handler
    listener: logging.handlers.QueueListener


def _extract_logging_config(
    config: Config | Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Extract the `logging` configuration section, or `None` if absent/empty."""
    raw: Any
    if config is None:
        try:
            container = get_container()
            cfg = container.resolve(Config) if container.has(Config) else None
        except RuntimeError:
            cfg = None
        raw = cfg.get("logging") if cfg is not None else None
    elif isinstance(config, Config):
        raw = config.get("logging")
    elif isinstance(config, Mapping):
        if "logging" in config and isinstance(config["logging"], Mapping):
            raw = config["logging"]
        else:
            raw = config
    else:
        raise TypeError(
            f"LogManager config must be Config, Mapping, or None, got {type(config).__name__}."
        )

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TypeError("Configuration key 'logging' must be a dictionary.")
    if not raw:
        return None
    return raw


class LogManager:
    """
    Per-application logging lifecycle and channel registry.

    Wraps file/stream handlers behind `QueueHandler` and `QueueListener` so
    request paths never block on filesystem I/O, and flushes/stops cleanly on shutdown.
    """

    def __init__(
        self,
        config: Config | Mapping[str, Any] | None = None,
        *,
        base_path: str | Path | None = None,
    ) -> None:
        self._config = _extract_logging_config(config)
        self._base_path = Path(base_path).resolve() if base_path is not None else Path.cwd()
        self._lock = threading.RLock()
        self._started = False

        self._default_channel_name: str | None = None
        self._leaf_runtimes: dict[str, _LeafChannelRuntime] = {}
        self._channel_loggers: dict[str, ChannelLogger] = {}
        self._channel_leaves: dict[str, tuple[str, ...]] = {}
        self._attached_swiftpy_handlers: list[logging.Handler] = []

    @classmethod
    def get_instance(cls) -> LogManager | None:
        """Resolve the active application's `LogManager`, or `None` if unavailable."""
        return _resolve_active_manager_or_none()

    @property
    def is_configured(self) -> bool:
        """Return True if a non-empty `logging` configuration was supplied."""
        return self._config is not None

    @property
    def is_started(self) -> bool:
        """Return True while queue listeners and handlers are running."""
        with self._lock:
            return self._started

    @property
    def default_channel(self) -> str | None:
        """Return the resolved default channel name when configured."""
        return self._default_channel_name

    def target_handlers_for(self, channel_name: str) -> tuple[logging.Handler, ...]:
        """Return the concrete stdlib target handlers backing `channel_name`."""
        with self._lock:
            leaf_names = self._channel_leaves.get(channel_name)
            if leaf_names is None:
                raise ValueError(f"Log channel '{channel_name}' is not configured.")
            return tuple(
                self._leaf_runtimes[leaf_name].target_handler
                for leaf_name in leaf_names
            )

    def channel(self, name: str | None = None) -> ChannelLogger:
        """Return a `ChannelLogger` for `name` or the configured default channel."""
        with self._lock:
            if not self._started:
                target_name = name or self._default_channel_name or "app"
                fallback_name = "swiftpy.app" if name is None else f"swiftpy.app.{name}"
                return ChannelLogger(
                    logging.getLogger(fallback_name),
                    channel_name=target_name,
                )

            resolved_name = name or self._default_channel_name
            if resolved_name is None or resolved_name not in self._channel_loggers:
                raise ValueError(f"Log channel '{resolved_name}' is not configured.")
            return self._channel_loggers[resolved_name]

    def _parse_queue_size(self, cfg: Mapping[str, Any]) -> int:
        raw_size = cfg.get("queue_size", _DEFAULT_QUEUE_SIZE)
        if isinstance(raw_size, bool) or not isinstance(raw_size, int):
            raise TypeError(
                f"logging.queue_size must be an integer, got {type(raw_size).__name__}."
            )
        if raw_size < 0:
            raise ValueError(f"logging.queue_size must be >= 0, got {raw_size}.")
        return raw_size

    def _resolve_stack_leaves(
        self,
        channel_name: str,
        channels_cfg: Mapping[str, Any],
        *,
        visiting: tuple[str, ...],
    ) -> list[str]:
        if channel_name in visiting:
            cycle = " -> ".join((*visiting, channel_name))
            raise ValueError(f"Cyclic log stack configuration detected: {cycle}")
        if channel_name not in channels_cfg:
            raise ValueError(
                f"Log stack references unknown channel '{channel_name}'."
            )

        ch_cfg = channels_cfg[channel_name]
        if not isinstance(ch_cfg, Mapping):
            raise TypeError(
                f"Log channel '{channel_name}' configuration must be a dictionary."
            )

        driver = ch_cfg.get("driver")
        if isinstance(driver, str) and driver.strip().lower() == "stack":
            sub_channels = ch_cfg.get("channels")
            if (
                isinstance(sub_channels, (str, bytes))
                or not isinstance(sub_channels, Sequence)
                or not sub_channels
            ):
                raise ValueError(
                    f"Stack log channel '{channel_name}' must define a non-empty list of 'channels'."
                )
            collected: list[str] = []
            seen: set[str] = set()
            next_visiting = (*visiting, channel_name)
            for sub in sub_channels:
                if not isinstance(sub, str) or not sub:
                    raise ValueError(
                        f"Stack log channel '{channel_name}' contains an invalid channel entry: {sub!r}."
                    )
                for leaf in self._resolve_stack_leaves(
                    sub,
                    channels_cfg,
                    visiting=next_visiting,
                ):
                    if leaf not in seen:
                        seen.add(leaf)
                        collected.append(leaf)
            return collected

        return [channel_name]

    def _build_runtime(
        self,
        cfg: Mapping[str, Any],
    ) -> tuple[
        str,
        int,
        dict[str, _LeafChannelRuntime],
        dict[str, tuple[str, ...]],
        dict[str, int],
    ]:
        default_level = parse_log_level(cfg.get("level", "info"))
        raw_channels = cfg.get("channels")
        if not isinstance(raw_channels, Mapping) or not raw_channels:
            raise ValueError("Configuration 'logging.channels' must be a non-empty dict.")

        raw_default = cfg.get("default")
        if raw_default is None:
            default_channel = (
                "stack" if "stack" in raw_channels else next(iter(raw_channels))
            )
        else:
            if not isinstance(raw_default, str) or raw_default not in raw_channels:
                raise ValueError(
                    f"Default log channel {raw_default!r} is not configured in logging.channels."
                )
            default_channel = raw_default

        queue_size = self._parse_queue_size(cfg)
        leaf_runtimes: dict[str, _LeafChannelRuntime] = {}
        channel_leaves: dict[str, tuple[str, ...]] = {}
        channel_levels: dict[str, int] = {}

        try:
            # 1. Build leaf drivers first so each file/stream owns a single QueueListener.
            for ch_name, ch_cfg in raw_channels.items():
                if not isinstance(ch_cfg, Mapping):
                    raise TypeError(
                        f"Log channel '{ch_name}' configuration must be a dictionary."
                    )
                driver = ch_cfg.get("driver")
                if not isinstance(driver, str) or not driver.strip():
                    raise ValueError(
                        f"Log channel '{ch_name}' must define a non-empty 'driver'."
                    )
                if driver.strip().lower() == "stack":
                    continue

                raw_ch_level = ch_cfg.get("level")
                ch_level = (
                    parse_log_level(raw_ch_level)
                    if raw_ch_level is not None
                    else default_level
                )
                target_handler = build_target_handler(
                    ch_name,
                    ch_cfg,
                    default_level=default_level,
                    base_path=self._base_path,
                )
                log_queue: queue.Queue[logging.LogRecord] = queue.Queue(
                    maxsize=queue_size
                )
                q_handler = logging.handlers.QueueHandler(log_queue)
                q_handler.setLevel(ch_level)
                listener = logging.handlers.QueueListener(
                    log_queue,
                    target_handler,
                    respect_handler_level=True,
                )
                leaf_runtimes[ch_name] = _LeafChannelRuntime(
                    name=ch_name,
                    level=ch_level,
                    log_queue=log_queue,
                    queue_handler=q_handler,
                    target_handler=target_handler,
                    listener=listener,
                )
                channel_leaves[ch_name] = (ch_name,)
                channel_levels[ch_name] = ch_level

            # 2. Resolve stack channels recursively with cycle detection.
            for ch_name, ch_cfg in raw_channels.items():
                driver = str(ch_cfg.get("driver", "")).strip().lower()
                if driver != "stack":
                    continue
                leaves = self._resolve_stack_leaves(
                    ch_name,
                    raw_channels,
                    visiting=(),
                )
                channel_leaves[ch_name] = tuple(leaves)
                raw_stack_level = ch_cfg.get("level")
                if raw_stack_level is not None:
                    channel_levels[ch_name] = parse_log_level(raw_stack_level)
                else:
                    channel_levels[ch_name] = min(
                        leaf_runtimes[leaf].level for leaf in leaves
                    )
        except Exception:
            for leaf_rt in leaf_runtimes.values():
                leaf_rt.target_handler.close()
            raise

        return (
            default_channel,
            default_level,
            leaf_runtimes,
            channel_leaves,
            channel_levels,
        )

    def start(self) -> None:
        """Start queue listeners and attach isolated channel and framework handlers."""
        with self._lock:
            if self._started or self._config is None:
                return

            (
                default_channel,
                _default_level,
                leaf_runtimes,
                channel_leaves,
                channel_levels,
            ) = self._build_runtime(self._config)

            started_listeners: list[logging.handlers.QueueListener] = []
            try:
                for runtime in leaf_runtimes.values():
                    runtime.listener.start()
                    started_listeners.append(runtime.listener)

                channel_loggers: dict[str, ChannelLogger] = {}
                for ch_name, leaves in channel_leaves.items():
                    ch_level = channel_levels[ch_name]
                    internal_name = f"_swiftpy_ch_{id(self)}_{ch_name}"
                    ch_logger = logging.getLogger(internal_name)
                    ch_logger.handlers.clear()
                    ch_logger.filters.clear()
                    ch_logger.setLevel(ch_level)
                    ch_logger.propagate = True
                    ch_logger.addFilter(_ChannelDisplayNameFilter(ch_name))
                    for leaf_name in leaves:
                        ch_logger.addHandler(leaf_runtimes[leaf_name].queue_handler)
                    channel_loggers[ch_name] = ChannelLogger(
                        ch_logger,
                        channel_name=ch_name,
                    )

                default_leaves = channel_leaves[default_channel]
                default_ch_level = channel_levels[default_channel]
                swiftpy_logger = logging.getLogger("swiftpy")
                attached_handlers: list[logging.Handler] = []
                active_filter = _SwiftpyActiveManagerFilter(
                    self,
                    min_level=default_ch_level,
                )
                for leaf_name in default_leaves:
                    leaf_rt = leaf_runtimes[leaf_name]
                    fw_handler = logging.handlers.QueueHandler(leaf_rt.log_queue)
                    fw_handler.setLevel(max(default_ch_level, leaf_rt.level))
                    fw_handler.addFilter(active_filter)
                    swiftpy_logger.addHandler(fw_handler)
                    attached_handlers.append(fw_handler)

                _SWIFTPY_COORDINATOR.attach(id(self), default_ch_level)

                self._default_channel_name = default_channel
                self._leaf_runtimes = leaf_runtimes
                self._channel_leaves = channel_leaves
                self._channel_loggers = channel_loggers
                self._attached_swiftpy_handlers = attached_handlers
                self._started = True
            except Exception:
                for listener in started_listeners:
                    listener.stop()
                for runtime in leaf_runtimes.values():
                    runtime.target_handler.close()
                raise

    def stop(self) -> None:
        """Detach handlers, flush queued log records, stop listeners, and close targets."""
        with self._lock:
            if not self._started:
                return

            swiftpy_logger = logging.getLogger("swiftpy")
            for handler in self._attached_swiftpy_handlers:
                swiftpy_logger.removeHandler(handler)
            self._attached_swiftpy_handlers.clear()
            _SWIFTPY_COORDINATOR.detach(id(self))

            for ch_logger in self._channel_loggers.values():
                ch_logger._logger.handlers.clear()
                ch_logger._logger.filters.clear()
                logging.Logger.manager.loggerDict.pop(ch_logger._logger.name, None)

            for runtime in self._leaf_runtimes.values():
                runtime.listener.stop()
            for runtime in self._leaf_runtimes.values():
                runtime.target_handler.flush()
                runtime.target_handler.close()

            self._leaf_runtimes.clear()
            self._channel_loggers.clear()
            self._channel_leaves.clear()
            self._default_channel_name = None
            self._started = False

    async def astart(self) -> None:
        """Start logging off the asyncio event loop thread."""
        await asyncio.to_thread(self.start)

    async def astop(self) -> None:
        """Stop logging and flush queued records off the asyncio event loop thread."""
        await asyncio.to_thread(self.stop)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()


__all__ = ["ChannelLogger", "LogManager"]
