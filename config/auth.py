from swiftpy.helpers.helper import env, env_bool

CONFIG: dict[str, object] = {
    "blacklist_enabled": env_bool("AUTH_BLACKLIST_ENABLED", True),
    "hydrate_user": env_bool("AUTH_HYDRATE_USER", False),
    "passive_resolution": env_bool("AUTH_PASSIVE_RESOLUTION", False),
    "defaults": {
        "guard": env("AUTH_GUARD", "jwt"),
    },
    "hash": {
        "rounds": int(env("BCRYPT_ROUNDS", "12")),
    },
    "guards": {
        "jwt": {
            "driver": "jwt",
            "algorithm": env("JWT_ALGORITHM", "HS256"),
            "secret": env("JWT_SECRET", None),
            "access_token_ttl": int(env("JWT_ACCESS_TTL", "3600")),
            "refresh_token_ttl": int(env("JWT_REFRESH_TTL", "604800")),
            "issuer": env("JWT_ISSUER", None),
            "audience": env("JWT_AUDIENCE", None),
            "leeway": int(env("JWT_LEEWAY", "0")),
            "private_key": env("JWT_PRIVATE_KEY", None),
            "public_key": env("JWT_PUBLIC_KEY", None),
        },
        "api_key": {
            "driver": "api_key",
            "table": env("AUTH_API_KEY_TABLE", "api_keys"),
            "prefix_column": env("AUTH_API_KEY_PREFIX_COLUMN", "key_prefix"),
            "hash_column": env("AUTH_API_KEY_HASH_COLUMN", "key_hash"),
            "header": env("AUTH_API_KEY_HEADER", "x-api-key"),
            "pepper": env("AUTH_KEY_PEPPER", None),
        },
    },
    "redis": {
        "host": env("REDIS_HOST", "127.0.0.1"),
        "port": int(env("REDIS_PORT", "6379")),
        "password": env("REDIS_PASSWORD", None),
    },
}
