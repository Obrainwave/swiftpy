from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import logging
import sys
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

from swiftpy.core.application import Application
from swiftpy.core.config import ConfigurationError
from swiftpy.core.container import bind_container

logger = logging.getLogger("swiftpy.console")


def ensure_project_in_sys_path(project_root: str | Path | None = None) -> str:
    """Ensure the project root directory is at the front of `sys.path`."""
    root = str(Path.cwd() if project_root is None else Path(project_root))
    if sys.path and sys.path[0] == root:
        return root
    while root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    return root


def load_application(target: str = "main:app") -> Application:
    """Import `module:attribute` from the current project and return the Application."""
    module_name, _, attribute = target.partition(":")
    ensure_project_in_sys_path()

    module = importlib.import_module(module_name)
    app = getattr(module, attribute or "app")
    if not isinstance(app, Application):
        raise TypeError(f"{target!r} is not a SwiftPY Application")
    return app


def load_optional_app(
    app_target: str | None,
    *,
    loader: Callable[[str], Application] | None = None,
) -> Application | None:
    """Load the optional ``module:attribute`` application target or exit cleanly on failure."""
    if not app_target:
        return None
    resolve_app = loader or load_application
    try:
        return resolve_app(app_target)
    except (
        ImportError,
        AttributeError,
        TypeError,
        ConfigurationError,
        OSError,
        ValueError,
        RuntimeError,
        SyntaxError,
    ) as exc:
        logger.error("Could not boot application target '%s': %s", app_target, exc)
        raise SystemExit(1) from exc


def run_coroutine[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine synchronously, safely handling active event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def run_app_action(
    action: Callable[[], Awaitable[None]],
    *,
    app: Application | None = None,
    cleanup: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Execute an async CLI action inside optional Application startup/container/shutdown."""

    async def _execute() -> None:
        if app is not None:
            await app.startup()
        try:
            if app is not None:
                with bind_container(app.container):
                    await action()
            else:
                await action()
        finally:
            if cleanup is not None:
                await cleanup()
            if app is not None:
                await app.shutdown()

    run_coroutine(_execute())


__all__ = [
    "ensure_project_in_sys_path",
    "load_application",
    "load_optional_app",
    "run_app_action",
    "run_coroutine",
]
