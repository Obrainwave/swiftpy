from __future__ import annotations

from typing import Any

import httpx

from swiftpy.mail.exceptions import MailConfigurationError, MailValidationError
from swiftpy.mail.message import MailMessage, format_mail_address
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.api.base import ApiMailTransport

type _MultipartFileValue = tuple[str | None, str] | tuple[str | None, bytes, str]


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
        max_attachment_size_bytes: int | None = None,
        max_message_size_bytes: int | None = None,
    ) -> None:
        super().__init__(
            timeout=timeout,
            client=client,
            default_from=default_from,
            max_attachment_size_bytes=max_attachment_size_bytes,
            max_message_size_bytes=max_message_size_bytes,
        )
        self.api_key = api_key
        self.domain = domain
        self.endpoint = endpoint.rstrip("/")

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        default_from: tuple[str, str | None] | None = None,
        max_attachment_size_bytes: int | None = None,
        max_message_size_bytes: int | None = None,
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
            max_attachment_size_bytes=cls._resolve_size_limit(
                max_attachment_size_bytes, config, "max_attachment_size_bytes"
            ),
            max_message_size_bytes=cls._resolve_size_limit(
                max_message_size_bytes, config, "max_message_size_bytes"
            ),
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
        formatted_from = format_mail_address(from_addr, from_name, field_name="from")

        multipart_fields: list[tuple[str, _MultipartFileValue]] = [
            ("from", (None, formatted_from)),
            ("subject", (None, message.subject)),
        ]
        for recipient in message.to:
            multipart_fields.append(
                ("to", (None, format_mail_address(recipient, field_name="to")))
            )
        for recipient in message.cc:
            multipart_fields.append(
                ("cc", (None, format_mail_address(recipient, field_name="cc")))
            )
        for recipient in message.bcc:
            multipart_fields.append(
                ("bcc", (None, format_mail_address(recipient, field_name="bcc")))
            )

        if message.reply_to:
            multipart_fields.append(
                (
                    "h:Reply-To",
                    (None, format_mail_address(message.reply_to, field_name="reply_to")),
                )
            )

        if message.text_body:
            multipart_fields.append(("text", (None, message.text_body)))
        if message.html_body:
            multipart_fields.append(("html", (None, message.html_body)))
        if not message.text_body and not message.html_body:
            multipart_fields.append(("text", (None, "")))

        for k, v in message.headers.items():
            multipart_fields.append((f"h:{k}", (None, str(v))))
        for tag in message.tags:
            multipart_fields.append(("o:tag", (None, str(tag))))
        for k, v in message.metadata.items():
            multipart_fields.append((f"v:{k}", (None, str(v))))

        loaded_attachments = await self._read_attachments_with_limits(message)
        for att, content in loaded_attachments:
            field_name = "inline" if att.disposition == "inline" else "attachment"
            multipart_fields.append(
                (field_name, (att.filename, content, att.content_type))
            )

        url = f"{self.endpoint}/{self.domain}/messages"
        client = await self._get_client()

        try:
            response = await client.post(
                url,
                files=multipart_fields,
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
                accepted=message.all_recipient_addresses(),
                rejected=[],
                raw_response=res_data,
            )
        except Exception as exc:
            self._handle_http_error(exc, "Mailgun")
            raise  # pragma: no cover


__all__ = ["MailgunTransport"]
