from __future__ import annotations

from swiftpy.helpers.helper import env, env_bool

CONFIG = {
    "name": env("APP_NAME", "SwiftPY"),
    "env": env("APP_ENV", "local"),
    "debug": env_bool("APP_DEBUG", False),
    "enable_benchmarks": env_bool("APP_ENABLE_BENCHMARKS", False),
    "key": env("APP_KEY", ""),
    "url": env("APP_URL", "http://localhost:8000"),
    "cipher": "AES-256-GCM",
}
