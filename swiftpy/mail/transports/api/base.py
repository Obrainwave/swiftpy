from __future__ import annotations

import httpx

from swiftpy.mail.exceptions import (
    MailAuthenticationError,
    MailConnectionError,
    MailRateLimitError,
    MailTimeoutError,
    MailTransportError,
    MailValidationError,
)
from swiftpy.mail.transports.base import MailTransport


class ApiMailTransport(MailTransport):
    """Base transport for HTTP API email service providers."""

    def __init__(
        self,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        default_from: tuple[str, str | None] | None = None,
    ) -> None:
        self.timeout = timeout
        self._client = client
        self._owns_client: bool = client is None
        self._closed: bool = False
        self.default_from = default_from

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
            text = exc.response.text
            if code in (401, 403):
                raise MailAuthenticationError(
                    f"{provider_name} API authentication failed ({code}): {text}"
                ) from exc
            if code in (400, 422):
                raise MailValidationError(
                    f"{provider_name} API validation error ({code}): {text}"
                ) from exc
            if code == 429:
                retry_after_hdr = exc.response.headers.get("Retry-After")
                retry_after_val: float | None = None
                if retry_after_hdr:
                    try:
                        retry_after_val = float(retry_after_hdr)
                    except ValueError:
                        retry_after_val = None
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
