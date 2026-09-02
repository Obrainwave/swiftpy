from swiftpy import env

CONFIG: dict[str, object] = {
    "default": env("DB_CONNECTION", "postgresql"),    
    "connections": {
        "postgresql": {
            "host": env("DB_HOST", "127.0.0.1"),
            "port": int(env("DB_PORT", "5432")),
            "database": env("DB_DATABASE", "swiftpy"),
            "user": env("DB_USERNAME", "postgres"),
            "password": env("DB_PASSWORD", ""),
            "ssl": env("DB_SSL", "false").lower() == "true",
        },
    },
    "pool": {
        "min_size": int(env("DB_POOL_MIN_SIZE", "5")),
        "max_size": int(env("DB_POOL_MAX_SIZE", "50")),
        "max_queries": int(env("DB_POOL_MAX_QUERIES", "50000")),
        "max_size": int(env("DB_POOL_MAX_SIZE", "50")),
        "max_queries": int(env("DB_POOL_MAX_QUERIES", "50000")),
        "max_inactive_connection_lifetime": float(env("DB_POOL_MAX_INACTIVE_LIFETIME", "300.0")),
        "command_timeout": float(env("DB_COMMAND_TIMEOUT", "60.0")),
    },
    "timeouts": {
        "connect": float(env("DB_CONNECT_TIMEOUT", "10.0")),
        "statement": float(env("DB_STATEMENT_TIMEOUT", "30.0")),
        "transaction": float(env("DB_TRANSACTION_TIMEOUT", "60.0")),  
    },
}
