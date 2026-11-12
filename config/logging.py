from __future__ import annotations

import os

CONFIG = {
    "default": os.getenv("LOG_CHANNEL", "stack"),
    "level": os.getenv("LOG_LEVEL", "info"),
    "channels": {
        "stack": {
            "driver": "stack",
            "channels": ["daily"],
        },
        "single": {
            "driver": "single",
            "path": "storage/logs/swiftpy.log",
            "level": os.getenv("LOG_LEVEL", "info"),
        },
        "daily": {
            "driver": "daily",
            "path": "storage/logs/swiftpy.log",
            "level": os.getenv("LOG_LEVEL", "info"),
            "days": 14,
        },
    },
}
