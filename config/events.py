from swiftpy.helpers.helper import env

CONFIG: dict[str, object] = {
    "default": env("EVENTS_DRIVER", "redis"),
    "redis": {
        "host": env("REDIS_HOST", "127.0.0.1"),
        "port": int(env("REDIS_PORT", "6379")),
        "password": env("REDIS_PASSWORD", None),
        "database": int(env("REDIS_EVENTS_DB", "2")),
        "prefix": env("EVENTS_PREFIX", "swiftpy_events:"),
    },
}
