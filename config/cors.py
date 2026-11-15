from __future__ import annotations

from swiftpy.helpers.helper import env, env_bool

CONFIG: dict[str, object] = {
    "allowed_origins": [
        origin.strip()
        for origin in str(env("CORS_ALLOWED_ORIGINS", "") or "").split(",")
        if origin.strip()
    ],
    "allowed_methods": ["GET", "HEAD", "POST"],
    "allowed_headers": ["Authorization", "Content-Type", "X-Requested-With"],
    "expose_headers": [],
    "allow_credentials": env_bool("CORS_ALLOW_CREDENTIALS", False),
    "max_age": int(env("CORS_MAX_AGE", "600") or 600),
}
