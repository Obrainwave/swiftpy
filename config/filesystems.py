from __future__ import annotations

from swiftpy.helpers.helper import env

CONFIG: dict[str, object] = {
    "default": env("FILESYSTEM_DISK", "local"),
    "signing_key": env("FILESYSTEM_SIGNING_KEY", env("APP_KEY", "")),
    "disks": {
        "local": {
            "driver": "local",
            "root": env("STORAGE_LOCAL_ROOT", "storage/app/private"),
            "visibility": "private",
            "permissions": {
                "file": {
                    "public": 0o644,
                    "private": 0o600,
                },
                "dir": {
                    "public": 0o755,
                    "private": 0o700,
                },
            },
        },
        "public": {
            "driver": "local",
            "root": env("STORAGE_PUBLIC_ROOT", "storage/app/public"),
            "url": env("STORAGE_PUBLIC_URL", "/storage"),
            "visibility": "public",
        },
        "s3": {
            "driver": "s3",
            "key": env("AWS_ACCESS_KEY_ID", ""),
            "secret": env("AWS_SECRET_ACCESS_KEY", ""),
            "region": env("AWS_DEFAULT_REGION", "us-east-1"),
            "bucket": env("AWS_BUCKET", ""),
            "url": env("AWS_URL", None),
            "endpoint": env("AWS_ENDPOINT", None),
            "use_path_style_endpoint": env("AWS_USE_PATH_STYLE_ENDPOINT", "false").lower() == "true",
            "visibility": "private",
        },
    },
    "uploads": {
        "max_file_size": int(env("UPLOAD_MAX_FILE_SIZE", str(100 * 1024 * 1024))),
        "max_request_size": int(env("UPLOAD_MAX_REQUEST_SIZE", str(200 * 1024 * 1024))),
        "memory_limit": int(env("UPLOAD_MEMORY_LIMIT", str(1024 * 1024))),
        "temp_dir": env("UPLOAD_TEMP_DIR", None),
    },
}

