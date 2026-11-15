from __future__ import annotations

import email.utils
import html
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from swiftpy.mail.attachments import Attachment
from swiftpy.mail.exceptions import (
    AttachmentTooLargeError,
    MailAuthenticationError,
    MailConnectionError,
    MailRateLimitError,
    MailTimeoutError,
    MailTransportError,
    MailValidationError,
)
from swiftpy.mail.message import MailMessage
from swiftpy.mail.transports.base import MailTransport

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SECRET_TOKEN_RE = re.compile(
    r"(?i)(?:"
    r"bearer\s+[A-Za-z0-9._\-]+"
    r"|\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9_\-]{6,}"
    r"|\bkey-[A-Za-z0-9_\-]{6,}"
    r"|\bSG\.[A-Za-z0-9_.\-]{6,}"
    r"|(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^\s'\";,]+"
    r")"
)


def _sanitize_error_body(raw_text: str, *, max_len: int = 300) -> str:
    """Strip HTML tags, redact credentials/tokens, and truncate upstream error bodies."""
    plain = html.unescape(_HTML_TAG_RE.sub(" ", raw_text))
    redacted = _SECRET_TOKEN_RE.sub("[REDACTED]", plain)
    collapsed = re.sub(r"\s+", " ", redacted).strip()
    if len(collapsed) > max_len:
        return f"{collapsed[: max_len - 3]}..."
    return collapsed


def _parse_retry_after(raw_header: str | None) -> float | None:
    """Parse numeric or HTTP-date Retry-After header into seconds."""
    if not raw_header:
        return None
    stripped = raw_header.strip()
    if not stripped:
        return None
    try:
        return max(0.0, float(stripped))
    except ValueError:
        try:
            parsed_dt = email.utils.parsedate_to_datetime(stripped)
        except (TypeError, ValueError):
            return None
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=UTC)
        return max(0.0, (parsed_dt - datetime.now(UTC)).total_seconds())


class ApiMailTransport(MailTransport):
    """Base transport for HTTP API email service providers."""

    def __init__(
        self,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        default_from: tuple[str, str | None] | None = None,
        max_attachment_size_bytes: int | None = None,
        max_message_size_bytes: int | None = None,
    ) -> None:
        self.timeout = timeout
        self._client = client
        self._owns_client: bool = client is None
        self._closed: bool = False
        self.default_from = default_from
        self.max_attachment_size_bytes = max_attachment_size_bytes
        self.max_message_size_bytes = max_message_size_bytes

    @staticmethod
    def _resolve_size_limit(
        explicit: int | None, config: dict[str, Any], key: str
    ) -> int | None:
        if explicit is not None:
            return explicit
        raw = config.get(key)
        return int(raw) if raw is not None else None

    async def _read_attachments_with_limits(
        self, message: MailMessage
    ) -> list[tuple[Attachment, bytes]]:
        """Read attachments and enforce configured per-attachment and total size limits."""
        total_bytes = len((message.text_body or "").encode("utf-8")) + len(
            (message.html_body or "").encode("utf-8")
        )
        loaded: list[tuple[Attachment, bytes]] = []
        for att in message.attachments:
            content = await att.read_bytes(max_size=self.max_attachment_size_bytes)
            total_bytes += len(content)
            if (
                self.max_message_size_bytes is not None
                and total_bytes > self.max_message_size_bytes
            ):
                raise AttachmentTooLargeError(
                    f"Total email size ({total_bytes} bytes) exceeds limit ({self.max_message_size_bytes} bytes)"
                )
            loaded.append((att, content))

        if (
            self.max_message_size_bytes is not None
            and total_bytes > self.max_message_size_bytes
        ):
            raise AttachmentTooLargeError(
                f"Total email size ({total_bytes} bytes) exceeds limit ({self.max_message_size_bytes} bytes)"
            )
        return loaded

    async def _get_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise MailConnectionError("Transport has been closed")
        if self._client is None or (self._owns_client and self._client.is_closed):
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        elif not self._owns_client and self._client.is_closed:
            raise MailConnectionError(
                "External HTTP client provided to transport has been closed"
            )
        return self._client

    def _resolve_from(
        self,
        from_address: str | None,
        from_name: str | None,
    ) -> tuple[str, str | None]:
        if from_address:
            return from_address, from_name
        if self.default_from:
            return self.default_from[0], from_name or self.default_from[1]
        raise MailValidationError(
            "Email message must specify a from_address or have a default configured"
        )

    def _handle_http_error(self, exc: Exception, provider_name: str) -> None:
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            text = _sanitize_error_body(exc.response.text)
            if code in (401, 403):
                raise MailAuthenticationError(
                    f"{provider_name} API authentication failed ({code}): {text}"
                ) from exc
            if code in (400, 422):
                raise MailValidationError(
                    f"{provider_name} API validation error ({code}): {text}"
                ) from exc
            if code == 429:
                retry_after_val = _parse_retry_after(
                    exc.response.headers.get("Retry-After")
                )
                raise MailRateLimitError(
                    f"{provider_name} API rate limit exceeded ({code}): {text}",
                    retry_after=retry_after_val,
                ) from exc
            raise MailTransportError(
                f"{provider_name} API HTTP error ({code}): {text}"
            ) from exc
        if isinstance(exc, httpx.TimeoutException):
            raise MailTimeoutError(
                f"{provider_name} API request timed out: {exc}"
            ) from exc
        if isinstance(exc, httpx.RequestError):
            raise MailConnectionError(
                f"{provider_name} API network connection error: {exc}"
            ) from exc
        raise MailTransportError(
            f"Unexpected error communicating with {provider_name}: {exc}"
        ) from exc

    async def close(self) -> None:
        """Close the underlying HTTP client if this transport created and owns it."""
        self._closed = True
        if (
            self._owns_client
            and self._client is not None
            and not self._client.is_closed
        ):
            await self._client.aclose()
            self._client = None


__all__ = ["ApiMailTransport"]
