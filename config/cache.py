from swiftpy.helpers.helper import env

CONFIG: dict[str, object] = {
    "default": env("CACHE_DRIVER", "redis"),
    "prefix": env("CACHE_PREFIX", "swiftpy_cache:"),
    "default_ttl": int(env("CACHE_DEFAULT_TTL", "3600")),
    "stores": {
        "redis": {
            "driver": "redis",
            "host": env("REDIS_HOST", "127.0.0.1"),
            "port": int(env("REDIS_PORT", "6379")),
            "password": env("REDIS_PASSWORD", None),
            "database": int(env("REDIS_CACHE_DB", "0")),
            "prefix": env("CACHE_PREFIX", "swiftpy_cache:"),
            "ttl": int(env("CACHE_DEFAULT_TTL", "3600")),
        },
        "memory": {
            "driver": "memory",
            "prefix": env("CACHE_PREFIX", "swiftpy_cache:"),
            "ttl": int(env("CACHE_DEFAULT_TTL", "3600")),
        },
    },
}
