from __future__ import annotations

from swiftpy.helpers.helper import env

CONFIG: dict[str, object] = {
    "default": env("QUEUE_DRIVER", "redis"),
    "connections": {
        "redis": {
            "driver": "redis",
            "host": env("REDIS_HOST", "127.0.0.1"),
            "port": int(env("REDIS_PORT", "6379")),
            "password": env("REDIS_PASSWORD", None),
            "database": int(env("REDIS_QUEUE_DB", "0")),
            "prefix": env("QUEUE_PREFIX", "swiftpy_queue:"),
            "queue": env("QUEUE_NAME", "default"),
        },
    },
}
