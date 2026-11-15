from __future__ import annotations

from swiftpy.helpers.helper import env

CONFIG = {
    "default": env("LOG_CHANNEL", "stack"),
    "level": env("LOG_LEVEL", "info"),
    "channels": {
        "stack": {
            "driver": "stack",
            "channels": ["daily"],
        },
        "single": {
            "driver": "single",
            "path": "storage/logs/swiftpy.log",
            "level": env("LOG_LEVEL", "info"),
        },
        "daily": {
            "driver": "daily",
            "path": "storage/logs/swiftpy.log",
            "level": env("LOG_LEVEL", "info"),
            "days": 14,
        },
    },
}
