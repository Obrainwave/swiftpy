from swiftpy.helpers.helper import env

CONFIG: dict[str, object] = {
    "defaults": {
        "guard": env("AUTH_GUARD", "jwt"),
    },
    "guards": {
        "jwt": {
            "driver": "jwt",
            "algorithm": env("JWT_ALGORITHM", "HS256"),
            "secret": env("JWT_SECRET", None),
            "access_token_ttl": int(env("JWT_ACCESS_TTL", "900")),
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
