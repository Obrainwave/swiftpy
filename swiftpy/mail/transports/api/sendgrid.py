from __future__ import annotations

import base64
from typing import Any

import httpx

from swiftpy.mail.exceptions import MailConfigurationError, MailValidationError
from swiftpy.mail.message import MailMessage, parse_mail_address
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.api.base import ApiMailTransport


class SendGridTransport(ApiMailTransport):
    """
    Email delivery transport using SendGrid v3 Mail Send API (https://sendgrid.com).
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = 30.0,
        endpoint: str = "https://api.sendgrid.com/v3/mail/send",
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
    ) -> SendGridTransport:
        api_key = str(config.get("api_key", ""))
        timeout = float(config.get("timeout", 30.0))
        endpoint = str(config.get("endpoint", "https://api.sendgrid.com/v3/mail/send"))
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

    def _parse_recipient(self, formatted: str) -> dict[str, str]:
        name, addr = parse_mail_address(formatted, field_name="recipient")
        rec = {"email": addr}
        if name:
            rec["name"] = name
        return rec

    async def send(self, message: MailMessage) -> MailSendResult:
        if not self.api_key:
            raise MailConfigurationError("SendGrid API key is not configured")

        if not message.to and not message.cc and not message.bcc:
            raise MailValidationError(
                "Email message must specify at least one recipient"
            )

        from_addr, from_name = self._resolve_from(
            message.from_address, message.from_name
        )
        parsed_from_name, clean_from_addr = parse_mail_address(
            from_addr, field_name="from"
        )
        from_payload: dict[str, str] = {"email": clean_from_addr}
        resolved_from_name = (
            from_name.strip()
            if from_name is not None and from_name.strip()
            else parsed_from_name
        )
        if resolved_from_name:
            from_payload["name"] = resolved_from_name

        personalization: dict[str, Any] = {
            "to": [self._parse_recipient(r) for r in message.to]
        }
        if message.cc:
            personalization["cc"] = [self._parse_recipient(r) for r in message.cc]
        if message.bcc:
            personalization["bcc"] = [self._parse_recipient(r) for r in message.bcc]

        content_list: list[dict[str, str]] = []
        if message.text_body:
            content_list.append({"type": "text/plain", "value": message.text_body})
        if message.html_body:
            content_list.append({"type": "text/html", "value": message.html_body})
        if not content_list:
            content_list.append({"type": "text/plain", "value": ""})

        payload: dict[str, Any] = {
            "personalizations": [personalization],
            "from": from_payload,
            "subject": message.subject,
            "content": content_list,
        }

        if message.reply_to:
            payload["reply_to"] = self._parse_recipient(message.reply_to)

        if message.headers:
            payload["headers"] = {str(k): str(v) for k, v in message.headers.items()}
        if message.tags:
            payload["categories"] = [str(tag) for tag in message.tags]
        if message.metadata:
            payload["custom_args"] = {
                str(k): str(v) for k, v in message.metadata.items()
            }

        loaded_attachments = await self._read_attachments_with_limits(message)
        if loaded_attachments:
            att_payload: list[dict[str, str]] = []
            for att, data in loaded_attachments:
                encoded = base64.b64encode(data).decode("ascii")
                item = {
                    "content": encoded,
                    "filename": att.filename,
                    "type": att.content_type,
                    "disposition": att.disposition,
                }
                if att.cid:
                    item["content_id"] = att.cid
                att_payload.append(item)
            payload["attachments"] = att_payload

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "SwiftPY-Mail/1.0",
        }

        client = await self._get_client()

        try:
            response = await client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
            msg_id = response.headers.get("X-Message-Id") or response.headers.get(
                "x-message-id"
            )
            return MailSendResult(
                success=True,
                message_id=msg_id,
                provider="sendgrid",
                accepted=message.all_recipient_addresses(),
                rejected=[],
                raw_response={"status_code": response.status_code},
            )
        except Exception as exc:
            self._handle_http_error(exc, "SendGrid")
            raise  # pragma: no cover


__all__ = ["SendGridTransport"]
