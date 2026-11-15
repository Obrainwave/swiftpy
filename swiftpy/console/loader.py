from __future__ import annotations

import importlib
import sys
from pathlib import Path

from swiftpy.core.application import Application


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


__all__ = ["ensure_project_in_sys_path", "load_application"]
