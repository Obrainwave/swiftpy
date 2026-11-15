from __future__ import annotations

import base64
from typing import Any

import httpx

from swiftpy.mail.exceptions import MailConfigurationError, MailValidationError
from swiftpy.mail.message import MailMessage, format_mail_address
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.api.base import ApiMailTransport


class ResendTransport(ApiMailTransport):
    """
    Email delivery transport using the Resend REST API (https://resend.com).
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = 30.0,
        endpoint: str = "https://api.resend.com/emails",
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
        self.endpoint = endpoint

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        default_from: tuple[str, str | None] | None = None,
        max_attachment_size_bytes: int | None = None,
        max_message_size_bytes: int | None = None,
    ) -> ResendTransport:
        api_key = str(config.get("api_key", ""))
        timeout = float(config.get("timeout", 30.0))
        endpoint = str(config.get("endpoint", "https://api.resend.com/emails"))
        return cls(
            api_key=api_key,
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
            raise MailConfigurationError("Resend API key is not configured")

        if not message.to and not message.cc and not message.bcc:
            raise MailValidationError(
                "Email message must specify at least one recipient"
            )

        from_addr, from_name = self._resolve_from(
            message.from_address, message.from_name
        )
        formatted_from = format_mail_address(from_addr, from_name, field_name="from")

        payload: dict[str, Any] = {
            "from": formatted_from,
            "to": [format_mail_address(r, field_name="to") for r in message.to],
            "subject": message.subject,
        }

        if message.cc:
            payload["cc"] = [
                format_mail_address(r, field_name="cc") for r in message.cc
            ]
        if message.bcc:
            payload["bcc"] = [
                format_mail_address(r, field_name="bcc") for r in message.bcc
            ]
        if message.reply_to:
            payload["reply_to"] = format_mail_address(
                message.reply_to, field_name="reply_to"
            )

        if message.html_body:
            payload["html"] = message.html_body
        if message.text_body:
            payload["text"] = message.text_body
        if not message.html_body and not message.text_body:
            payload["text"] = ""

        if message.headers:
            payload["headers"] = {str(k): str(v) for k, v in message.headers.items()}

        loaded_attachments = await self._read_attachments_with_limits(message)
        if loaded_attachments:
            att_payload: list[dict[str, str]] = []
            for att, data in loaded_attachments:
                encoded = base64.b64encode(data).decode("ascii")
                att_payload.append(
                    {
                        "filename": att.filename,
                        "content": encoded,
                    }
                )
            payload["attachments"] = att_payload

        resend_tags: list[dict[str, str]] = [
            {"name": tag, "value": "true"} for tag in message.tags
        ]
        for meta_key, meta_val in message.metadata.items():
            resend_tags.append({"name": str(meta_key), "value": str(meta_val)})
        if resend_tags:
            payload["tags"] = resend_tags

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "SwiftPY-Mail/1.0",
        }

        client = await self._get_client()

        try:
            response = await client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
            try:
                res_json = response.json()
            except (ValueError, KeyError, httpx.DecodingError):
                res_json = {"raw": response.text}
            msg_id = str(res_json.get("id", "")) if isinstance(res_json, dict) else ""
            return MailSendResult(
                success=True,
                message_id=msg_id,
                provider="resend",
                accepted=message.all_recipient_addresses(),
                rejected=[],
                raw_response=res_json,
            )
        except Exception as exc:
            self._handle_http_error(exc, "Resend")
            raise  # pragma: no cover


__all__ = ["ResendTransport"]
