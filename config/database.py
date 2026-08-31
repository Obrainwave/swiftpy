from __future__ import annotations

import os

CONFIG: dict[str, object] = {
    "default": os.getenv(
        "DB_CONNECTION",
        "postgresql",
    ),
    "connections": {
        "postgresql": {
            "host": os.getenv(
                "DB_HOST",
                "127.0.0.1",
            ),
            "port": int(
                os.getenv(
                    "DB_PORT",
                    "5432",
                )
            ),
            "database": os.getenv(
                "DB_DATABASE",
                "swiftpy",
            ),
            "user": os.getenv(
                "DB_USERNAME",
                "postgres",
            ),
            "password": os.getenv(
                "DB_PASSWORD",
                "",
            ),
            "ssl": os.getenv(
                "DB_SSL",
                "false",
            ).lower()
            == "true",
        },
    },
    "pool": {
        "min_size": int(
            os.getenv(
                "DB_POOL_MIN_SIZE",
                "5",
            )
        ),
        "max_size": int(
            os.getenv(
                "DB_POOL_MAX_SIZE",
                "20",
            )
        ),
        "max_queries": int(
            os.getenv(
                "DB_POOL_MAX_QUERIES",
                "50000",
            )
        ),
        "max_inactive_connection_lifetime": float(
            os.getenv(
                "DB_POOL_MAX_INACTIVE_LIFETIME",
                "300.0",
            )
        ),
        "command_timeout": float(
            os.getenv(
                "DB_COMMAND_TIMEOUT",
                "60.0",
            )
        ),
    },
    "timeouts": {
        "connect": float(
            os.getenv(
                "DB_CONNECT_TIMEOUT",
                "10.0",
            )
        ),
        "statement": float(
            os.getenv(
                "DB_STATEMENT_TIMEOUT",
                "30.0",
            )
        ),
        "transaction": float(
            os.getenv(
                "DB_TRANSACTION_TIMEOUT",
                "60.0",
            )
        ),
    },
}
