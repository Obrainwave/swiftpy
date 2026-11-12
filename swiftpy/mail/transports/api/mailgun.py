from __future__ import annotations

from typing import Any

import httpx

from swiftpy.mail.exceptions import MailConfigurationError, MailValidationError
from swiftpy.mail.message import MailMessage
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.api.base import ApiMailTransport


class MailgunTransport(ApiMailTransport):
    """
    Email delivery transport using Mailgun REST API (https://mailgun.com).
    """

    def __init__(
        self,
        api_key: str,
        domain: str,
        timeout: float = 30.0,
        endpoint: str = "https://api.mailgun.net/v3",
        client: httpx.AsyncClient | None = None,
        default_from: tuple[str, str | None] | None = None,
    ) -> None:
        super().__init__(timeout=timeout, client=client, default_from=default_from)
        self.api_key = api_key
        self.domain = domain
        self.endpoint = endpoint.rstrip("/")

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        default_from: tuple[str, str | None] | None = None,
    ) -> MailgunTransport:
        api_key = str(config.get("api_key", ""))
        domain = str(config.get("domain", ""))
        timeout = float(config.get("timeout", 30.0))
        endpoint = str(config.get("endpoint", "https://api.mailgun.net/v3"))
        return cls(
            api_key=api_key,
            domain=domain,
            timeout=timeout,
            endpoint=endpoint,
            default_from=default_from,
        )

    async def send(self, message: MailMessage) -> MailSendResult:
        if not self.api_key:
            raise MailConfigurationError("Mailgun API key is not configured")
        if not self.domain:
            raise MailConfigurationError("Mailgun domain is not configured")

        if not message.to and not message.cc and not message.bcc:
            raise MailValidationError(
                "Email message must specify at least one recipient"
            )

        from_addr, from_name = self._resolve_from(
            message.from_address, message.from_name
        )
        formatted_from = f"{from_name} <{from_addr}>" if from_name else from_addr

        form_data: dict[str, Any] = {
            "from": formatted_from,
            "subject": message.subject,
            "to": message.to,
        }
        if message.cc:
            form_data["cc"] = message.cc
        if message.bcc:
            form_data["bcc"] = message.bcc

        if message.reply_to:
            form_data["h:Reply-To"] = message.reply_to

        if message.text_body:
            form_data["text"] = message.text_body
        if message.html_body:
            form_data["html"] = message.html_body
        if not message.text_body and not message.html_body:
            form_data["text"] = ""

        for k, v in message.headers.items():
            form_data[f"h:{k}"] = v

        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for att in message.attachments:
            content = await att.read_bytes()
            field_name = "inline" if att.disposition == "inline" else "attachment"
            files.append((field_name, (att.filename, content, att.content_type)))

        url = f"{self.endpoint}/{self.domain}/messages"
        client = await self._get_client()

        try:
            if files:
                response = await client.post(
                    url,
                    data=form_data,
                    files=files,
                    auth=("api", self.api_key),
                )
            else:
                response = await client.post(
                    url,
                    data=form_data,
                    auth=("api", self.api_key),
                )
            response.raise_for_status()
            try:
                res_data = response.json()
            except (ValueError, KeyError, httpx.DecodingError):
                res_data = {"raw": response.text}
            msg_id = str(res_data.get("id", "")) if isinstance(res_data, dict) else ""
            return MailSendResult(
                success=True,
                message_id=msg_id,
                provider="mailgun",
                accepted=list(message.to),
                rejected=[],
                raw_response=res_data,
            )
        except Exception as exc:
            self._handle_http_error(exc, "Mailgun")
            raise  # pragma: no cover


__all__ = ["MailgunTransport"]
