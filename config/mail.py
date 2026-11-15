from __future__ import annotations

from swiftpy.helpers.helper import env

CONFIG: dict[str, object] = {
    # Default mailer connection name
    "default": env("MAIL_MAILER", "console"),
    # Global sender identity
    "from": {
        "address": env("MAIL_FROM_ADDRESS", "hello@example.com"),
        "name": env("MAIL_FROM_NAME", "SwiftPY Application"),
    },
    # Reply-To fallback
    "reply_to": {
        "address": env("MAIL_REPLY_TO_ADDRESS", None),
        "name": env("MAIL_REPLY_TO_NAME", None),
    },
    # Configured mailers
    "mailers": {
        "smtp": {
            "transport": "smtp",
            "host": env("MAIL_HOST", "127.0.0.1"),
            "port": int(env("MAIL_PORT", "587")),
            "username": env("MAIL_USERNAME", None),
            "password": env("MAIL_PASSWORD", None),
            "encryption": env("MAIL_ENCRYPTION", "starttls"),
            "timeout": float(env("MAIL_TIMEOUT", "30.0")),
            "validate_certs": env("MAIL_VALIDATE_CERTS", "true").lower() == "true",
        },
        "resend": {
            "transport": "resend",
            "api_key": env("RESEND_API_KEY", ""),
            "timeout": float(env("RESEND_TIMEOUT", "30.0")),
        },
        "sendgrid": {
            "transport": "sendgrid",
            "api_key": env("SENDGRID_API_KEY", ""),
            "timeout": float(env("SENDGRID_TIMEOUT", "30.0")),
        },
        "mailgun": {
            "transport": "mailgun",
            "api_key": env("MAILGUN_API_KEY", ""),
            "domain": env("MAILGUN_DOMAIN", ""),
            "endpoint": env("MAILGUN_ENDPOINT", "https://api.mailgun.net/v3"),
            "timeout": float(env("MAILGUN_TIMEOUT", "30.0")),
        },
        "sendmail": {
            "transport": "sendmail",
            "path": env("SENDMAIL_PATH", "/usr/sbin/sendmail"),
        },
        "console": {
            "transport": "console",
        },
        "log": {
            "transport": "console",
            "channel": env("MAIL_LOG_CHANNEL", "mail"),
        },
    },
    # Global limits
    "limits": {
        "max_attachment_size_bytes": 25 * 1024 * 1024,
        "max_message_size_bytes": 35 * 1024 * 1024,
    },
}
