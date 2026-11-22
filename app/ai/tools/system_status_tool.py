from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from swiftpy.ai.tools import BaseTool


class SystemStatusTool(BaseTool):
    """Function-calling tool reporting basic application runtime status."""

    @property
    def name(self) -> str:
        return "system_status"

    @property
    def description(self) -> str:
        return "Return the current UTC timestamp and application health status."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def handle(self, *args: Any, **kwargs: Any) -> Any:
        """Return structured UTC timestamp and ok status."""
        return {
            "status": "ok",
            "utc_time": datetime.now(tz=UTC).isoformat(),
        }
