from __future__ import annotations

import json
from typing import Any

from swiftpy.helpers.helper import env


def _parse_proxy_env(raw: Any) -> Any:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return raw
    stripped = raw.strip()
    if not stripped:
        return []
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return [stripped]
        if isinstance(parsed, list):
            return parsed
        return parsed
    return [entry.strip() for entry in stripped.split(",")]


_RAW_TRUSTED_PROXIES = env("TRUSTED_PROXIES", env("HTTP_TRUSTED_PROXIES", "")) or ""
_RAW_MAX_CONTENT_LENGTH = env("HTTP_MAX_CONTENT_LENGTH", None)

CONFIG: dict[str, Any] = {
    "trusted_proxies": _parse_proxy_env(_RAW_TRUSTED_PROXIES),
    "max_body_size": int(
        env("HTTP_MAX_BODY_SIZE", str(2 * 1024 * 1024)) or (2 * 1024 * 1024)
    ),
    "max_content_length": (
        int(_RAW_MAX_CONTENT_LENGTH)
        if _RAW_MAX_CONTENT_LENGTH not in (None, "")
        else None
    ),
    "max_json_depth": int(env("HTTP_MAX_JSON_DEPTH", "32") or "32"),
}
