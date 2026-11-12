from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from swiftpy.mail.exceptions import MailConfigurationError


@dataclass(frozen=True, slots=True)
class MailConfig:
    """Strongly-typed, validated mail subsystem configuration."""

    default: str = "console"
    from_address: str = "hello@example.com"
    from_name: str | None = "SwiftPY Application"
    reply_to_address: str | None = None
    reply_to_name: str | None = None
    mailers: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_attachment_size_bytes: int = 25 * 1024 * 1024
    max_message_size_bytes: int = 35 * 1024 * 1024

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MailConfig:
        """Construct MailConfig from a raw dictionary (e.g. from Config repository)."""
        if not isinstance(data, dict):
            raise MailConfigurationError("Mail configuration must be a dictionary")

        default_mailer = str(data.get("default", "console"))

        from_cfg = data.get("from", {})
        if isinstance(from_cfg, dict):
            from_addr = str(from_cfg.get("address", "hello@example.com"))
            from_name = from_cfg.get("name")
        else:
            from_addr = "hello@example.com"
            from_name = None

        reply_cfg = data.get("reply_to", {})
        if isinstance(reply_cfg, dict):
            reply_addr = reply_cfg.get("address")
            reply_name = reply_cfg.get("name")
        else:
            reply_addr = None
            reply_name = None

        mailers = data.get("mailers", {})
        if not isinstance(mailers, dict):
            mailers = {}

        limits = data.get("limits", {})
        max_att_size = 25 * 1024 * 1024
        max_msg_size = 35 * 1024 * 1024
        if isinstance(limits, dict):
            if "max_attachment_size_bytes" in limits:
                max_att_size = int(limits["max_attachment_size_bytes"])
            if "max_message_size_bytes" in limits:
                max_msg_size = int(limits["max_message_size_bytes"])

        return cls(
            default=default_mailer,
            from_address=from_addr,
            from_name=str(from_name) if from_name is not None else None,
            reply_to_address=str(reply_addr) if reply_addr is not None else None,
            reply_to_name=str(reply_name) if reply_name is not None else None,
            mailers=mailers,
            max_attachment_size_bytes=max_att_size,
            max_message_size_bytes=max_msg_size,
        )


__all__ = ["MailConfig"]
