from __future__ import annotations

from swiftpy.helpers.helper import env, env_bool

CONFIG: dict[str, object] = {
    "driver": env("SCHEDULE_DRIVER", "redis"),
    "timezone": env("SCHEDULE_TIMEZONE", env("APP_TIMEZONE", "UTC")),
    "auto_start": env_bool("SCHEDULE_AUTO_START", False),
    "poll_interval": float(env("SCHEDULE_POLL_INTERVAL", "1.0") or "1.0"),
    "grace_seconds": float(env("SCHEDULE_GRACE_SECONDS", "60.0") or "60.0"),
    "dispatch_lease_seconds": float(
        env("SCHEDULE_DISPATCH_LEASE_SECONDS", "30.0") or "30.0"
    ),
    "max_dispatch_attempts": int(
        env("SCHEDULE_MAX_DISPATCH_ATTEMPTS", "5") or "5"
    ),
    "default_lock_ttl": int(env("SCHEDULE_DEFAULT_LOCK_TTL", "900") or "900"),
    "prefix": env("SCHEDULE_PREFIX", "swiftpy_schedule:"),
    "retention_seconds": int(
        env("SCHEDULE_RETENTION_SECONDS", str(7 * 24 * 3600)) or str(7 * 24 * 3600)
    ),
    "max_occurrences_per_schedule": int(
        env("SCHEDULE_MAX_OCCURRENCES", "200") or "200"
    ),
}
