from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swiftpy.core.application import Application
    from swiftpy.http.router import Route

logger = logging.getLogger("swiftpy.http.route_cache")

DEFAULT_ROUTE_CACHE_PATH = "bootstrap/cache/routes.json"


class RouteCache:
    """
    Serializes and manages precompiled routes for production environments,
    eliminating route registration and reflection overhead at boot.
    """

    @staticmethod
    def _serialize_route(route: Route) -> dict[str, Any]:
        handler = route.handler
        mod_name = getattr(handler, "__module__", "")
        qual_name = getattr(
            handler, "__qualname__", getattr(handler, "__name__", str(handler))
        )
        handler_identifier = f"{mod_name}:{qual_name}" if mod_name else qual_name

        middleware_identifiers: list[str] = []
        for m in route.middleware:
            m_mod = getattr(m, "__module__", "")
            m_qual = getattr(m, "__qualname__", getattr(m, "__name__", str(m)))
            middleware_identifiers.append(f"{m_mod}:{m_qual}" if m_mod else m_qual)

        return {
            "method": route.method,
            "path": route.path,
            "handler": handler_identifier,
            "is_websocket": route.is_websocket,
            "is_static": not bool(route.param_names),
            "param_names": list(route.param_names),
            "normalized_path": route.normalized_path,
            "max_body_size": route.max_body_size,
            "middleware": middleware_identifiers,
        }

    @classmethod
    def build(
        cls,
        app: Application,
        cache_path: str | Path = DEFAULT_ROUTE_CACHE_PATH,
    ) -> dict[str, Any]:
        """
        Extract registered routes from the application router and write
        a serialized JSON cache to disk.
        """
        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        serialized_routes = [cls._serialize_route(route) for route in app.router.routes]

        payload: dict[str, Any] = {
            "version": app.version,
            "compiled_at": datetime.now(UTC).isoformat(),
            "count": len(serialized_routes),
            "routes": serialized_routes,
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        logger.info("Compiled %d routes into cache: %s", len(serialized_routes), path)
        return payload

    @classmethod
    def load(
        cls,
        cache_path: str | Path = DEFAULT_ROUTE_CACHE_PATH,
    ) -> dict[str, Any] | None:
        """
        Load precompiled route metadata from disk if the cache file exists.
        """
        path = Path(cache_path)
        if not path.is_file():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load precompiled route cache '%s': %s", path, exc)

        return None

    @classmethod
    def clear(
        cls,
        cache_path: str | Path = DEFAULT_ROUTE_CACHE_PATH,
    ) -> bool:
        """Remove the precompiled route cache file if it exists."""
        path = Path(cache_path)
        if path.is_file():
            path.unlink()
            logger.info("Cleared precompiled route cache: %s", path)
            return True
        return False


__all__ = ["DEFAULT_ROUTE_CACHE_PATH", "RouteCache"]
