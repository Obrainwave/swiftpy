from __future__ import annotations

import importlib
import sys
from pathlib import Path

from swiftpy.core.application import Application


def load_application(target: str = "main:app") -> Application:
    """Import `module:attribute` from the current project and return the Application."""
    module_name, _, attribute = target.partition(":")
    project_root = str(Path.cwd())
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    module = importlib.import_module(module_name)
    app = getattr(module, attribute or "app")
    if not isinstance(app, Application):
        raise TypeError(f"{target!r} is not a SwiftPY Application")
    return app
