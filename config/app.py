from __future__ import annotations

import os

CONFIG = {
    "name": os.getenv("APP_NAME", "SwiftPY"),
    "env": os.getenv("APP_ENV", "local"),
    "debug": os.getenv("APP_DEBUG", "true").lower() in ("true", "1", "yes"),
    "key": os.getenv("APP_KEY", ""),
    "url": os.getenv("APP_URL", "http://localhost:8000"),
    "cipher": "AES-256-GCM",
}
