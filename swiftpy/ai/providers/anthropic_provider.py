from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

try:
    import httpx
    _ANTHROPIC_HTTP_ERRORS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.RequestError)
except ImportError:
    httpx = None
    _ANTHROPIC_HTTP_ERRORS = (TimeoutError, OSError)

from swiftpy.ai.exceptions import (
    AIAuthenticationError,
    AIConnectionError,
    AIProviderError,
    AIRateLimitError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.providers.base import (
    LLMRequest,
    LLMResponse,
    ProviderInterface,
    TokenUsage,
)
from swiftpy.ai.streaming import parse_sse_events


class AnthropicProvider(ProviderInterface):
    """
    Anthropic provider adapter mapping unified requests to the /v1/messages endpoint.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        http_pool: AIHttpPool | None = None,
    ) -> None:
        super().__init__(config=config, http_pool=http_pool)
        self._api_key: str = str(self.config.get("api_key", ""))
        self._default_model: str = str(
            self.config.get("model", "claude-3-5-sonnet-20241022")
        )
        self._base_url: str = str(
            self.config.get("base_url", "https://api.anthropic.com/v1")
        ).rstrip("/")
        self._timeout: float = float(self.config.get("timeout", 60.0))
        self._anthropic_version: str = str(
            self.config.get("anthropic_version", "2023-06-01")
        )

    @property
    def name(self) -> str:
        return "anthropic"

    def get_default_model(self) -> str:
        return self._default_model

    async def send(self, request: LLMRequest) -> LLMResponse:
        url = f"{self._base_url}/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "content-type": "application/json",
        }

        model = request.model or self.get_default_model()

        # Separate system messages from user/assistant conversation history
        system_parts: list[str] = []
        dialogue_messages: list[dict[str, str]] = []
        for msg in request.messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            else:
                dialogue_messages.append({"role": msg.role, "content": msg.content})

        max_tokens = (
            request.max_tokens
            if request.max_tokens is not None
            else int(self.config.get("max_tokens", 1024))
        )

        payload: dict[str, Any] = {
            "model": model,
            "messages": dialogue_messages,
            "max_tokens": max_tokens,
        }

        # Omit optional parameters if not explicitly provided
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop_sequences"] = request.stop
        if request.extra:
            payload.update(request.extra)

        client = self._http_pool.get_client()
        start_time = time.perf_counter()

        try:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        except _ANTHROPIC_HTTP_ERRORS as exc:
            sanitized_exc = self.sanitize_error(str(exc))
            raise AIConnectionError(
                f"Failed to connect to Anthropic ({type(exc).__name__}): {sanitized_exc}",
                provider=self.name,
            ) from exc

        latency_ms = (time.perf_counter() - start_time) * 1000
        body_text = self.sanitize_error(response.text)

        # Parse JSON safely (handling non-JSON error or HTML responses)
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError):
            data = None

        if response.status_code == 401 or response.status_code == 403:
            err_msg = (
                data.get("error", {}).get("message")
                if isinstance(data, dict)
                else body_text
            ) or "Authentication failed"
            raise AIAuthenticationError(
                f"Anthropic authentication failed ({response.status_code}): {err_msg}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )

        if response.status_code == 429:
            retry_after_hdr = response.headers.get("Retry-After")
            retry_after = float(retry_after_hdr) if retry_after_hdr else None
            err_msg = (
                data.get("error", {}).get("message")
                if isinstance(data, dict)
                else body_text
            ) or "Rate limit reached"
            raise AIRateLimitError(
                f"Anthropic rate limit exceeded: {err_msg}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
                retry_after=retry_after,
            )

        if response.status_code >= 400:
            err_msg = (
                data.get("error", {}).get("message")
                if isinstance(data, dict)
                else body_text
            ) or f"HTTP {response.status_code}"
            raise AIProviderError(
                f"Anthropic API error ({response.status_code}): {err_msg}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )

        if not isinstance(data, dict):
            raise AIProviderError(
                f"Anthropic returned non-JSON response ({response.status_code}): {body_text[:200]}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )

        raw_content = data.get("content")
        text_parts: list[str] = []
        if isinstance(raw_content, list):
            for block in raw_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text") or ""))
        content = "".join(text_parts)

        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            input_tokens = int(raw_usage.get("input_tokens") or 0)
            output_tokens = int(raw_usage.get("output_tokens") or 0)
            usage = TokenUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )
        else:
            usage = TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

        finish_reason = data.get("stop_reason")
        model_reported = str(data.get("model") or model)

        return LLMResponse(
            content=content,
            role=str(data.get("role") or "assistant"),
            model=model_reported,
            usage=usage,
            raw=data,
            latency_ms=round(latency_ms, 2),
            finish_reason=str(finish_reason) if finish_reason else None,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        url = f"{self._base_url}/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "content-type": "application/json",
            "Accept": "text/event-stream",
        }

        model = request.model or self.get_default_model()

        system_parts: list[str] = []
        dialogue_messages: list[dict[str, str]] = []
        for msg in request.messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            else:
                dialogue_messages.append({"role": msg.role, "content": msg.content})

        max_tokens = (
            request.max_tokens
            if request.max_tokens is not None
            else int(self.config.get("max_tokens", 1024))
        )

        payload: dict[str, Any] = {
            "model": model,
            "messages": dialogue_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }

        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop_sequences"] = request.stop
        if request.extra:
            payload.update(request.extra)

        client = self._http_pool.get_client()

        try:
            req = client.build_request(
                "POST",
                url,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
            response = await client.send(req, stream=True)
        except _ANTHROPIC_HTTP_ERRORS as exc:
            sanitized_exc = self.sanitize_error(str(exc))
            raise AIConnectionError(
                f"Failed to connect to Anthropic stream ({type(exc).__name__}): {sanitized_exc}",
                provider=self.name,
            ) from exc

        if response.status_code >= 400:
            raw_bytes = await response.aread()
            body_text = self.sanitize_error(raw_bytes.decode("utf-8", errors="replace"))
            await response.aclose()

            try:
                err_data = json.loads(body_text)
                err_msg = err_data.get("error", {}).get("message") or body_text
            except (json.JSONDecodeError, ValueError):
                err_msg = body_text

            if response.status_code in (401, 403):
                raise AIAuthenticationError(
                    f"Anthropic authentication failed ({response.status_code}): {err_msg}",
                    provider=self.name,
                    status_code=response.status_code,
                    response_body=body_text,
                )
            if response.status_code == 429:
                retry_after_hdr = response.headers.get("Retry-After")
                retry_after = float(retry_after_hdr) if retry_after_hdr else None
                raise AIRateLimitError(
                    f"Anthropic rate limit exceeded: {err_msg}",
                    provider=self.name,
                    status_code=response.status_code,
                    response_body=body_text,
                    retry_after=retry_after,
                )
            raise AIProviderError(
                f"Anthropic API error ({response.status_code}): {err_msg}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )

        try:
            async for event in parse_sse_events(response.aiter_lines()):
                event_type = event.get("event")
                data = event.get("data")
                if isinstance(data, dict):
                    # Check for content_block_delta
                    data_type = data.get("type")
                    if event_type == "content_block_delta" or data_type == "content_block_delta":
                        delta = data.get("delta")
                        if isinstance(delta, dict) and delta.get("type") == "text_delta":
                            text = delta.get("text")
                            if text:
                                yield str(text)
                    elif event_type == "message_stop" or data_type == "message_stop":
                        break
        finally:
            await response.aclose()

