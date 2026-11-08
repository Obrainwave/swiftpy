from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

try:
    import httpx
    _OPENAI_HTTP_ERRORS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.RequestError)
except ImportError:
    httpx = None
    _OPENAI_HTTP_ERRORS = (TimeoutError, OSError)

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


class OpenAIProvider(ProviderInterface):
    """
    OpenAI provider adapter mapping unified requests to the /v1/chat/completions endpoint.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        http_pool: AIHttpPool | None = None,
    ) -> None:
        super().__init__(config=config, http_pool=http_pool)
        self._api_key: str = str(self.config.get("api_key", ""))
        self._default_model: str = str(self.config.get("model", "gpt-4o"))
        self._base_url: str = str(
            self.config.get("base_url", "https://api.openai.com/v1")
        ).rstrip("/")
        self._timeout: float = float(self.config.get("timeout", 60.0))

    @property
    def name(self) -> str:
        return "openai"

    def get_default_model(self) -> str:
        return self._default_model

    async def send(self, request: LLMRequest) -> LLMResponse:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        model = request.model or self.get_default_model()
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in request.messages],
        }

        # Omit optional parameters if not explicitly provided
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop"] = request.stop
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
        except _OPENAI_HTTP_ERRORS as exc:
            sanitized_exc = self.sanitize_error(str(exc))
            raise AIConnectionError(
                f"Failed to connect to OpenAI ({type(exc).__name__}): {sanitized_exc}",
                provider=self.name,
            ) from exc

        latency_ms = (time.perf_counter() - start_time) * 1000
        body_text = self.sanitize_error(response.text)

        # Parse JSON safely (handling non-JSON error or HTML responses like 502/503)
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
                f"OpenAI authentication failed ({response.status_code}): {err_msg}",
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
                f"OpenAI rate limit exceeded: {err_msg}",
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
                f"OpenAI API error ({response.status_code}): {err_msg}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )

        if not isinstance(data, dict):
            raise AIProviderError(
                f"OpenAI returned non-JSON response ({response.status_code}): {body_text[:200]}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )

        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) == 0:
            raise AIProviderError(
                "OpenAI returned a response with no choices",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )

        choice = choices[0]
        if not isinstance(choice, dict):
            raise AIProviderError(
                "OpenAI returned malformed choice object",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )

        msg = choice.get("message") or {}
        content = str(msg.get("content") or "") if isinstance(msg, dict) else ""
        role = str(msg.get("role") or "assistant") if isinstance(msg, dict) else "assistant"
        finish_reason = choice.get("finish_reason")

        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            usage = TokenUsage(
                prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                completion_tokens=int(raw_usage.get("completion_tokens") or 0),
                total_tokens=int(raw_usage.get("total_tokens") or 0),
            )
        else:
            usage = TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

        model_reported = str(data.get("model") or model)

        return LLMResponse(
            content=content,
            role=role,
            model=model_reported,
            usage=usage,
            raw=data,
            latency_ms=round(latency_ms, 2),
            finish_reason=str(finish_reason) if finish_reason else None,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        model = request.model or self.get_default_model()
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in request.messages],
            "stream": True,
        }

        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop"] = request.stop
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
        except _OPENAI_HTTP_ERRORS as exc:
            sanitized_exc = self.sanitize_error(str(exc))
            raise AIConnectionError(
                f"Failed to connect to OpenAI stream ({type(exc).__name__}): {sanitized_exc}",
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
                    f"OpenAI authentication failed ({response.status_code}): {err_msg}",
                    provider=self.name,
                    status_code=response.status_code,
                    response_body=body_text,
                )
            if response.status_code == 429:
                retry_after_hdr = response.headers.get("Retry-After")
                retry_after = float(retry_after_hdr) if retry_after_hdr else None
                raise AIRateLimitError(
                    f"OpenAI rate limit exceeded: {err_msg}",
                    provider=self.name,
                    status_code=response.status_code,
                    response_body=body_text,
                    retry_after=retry_after,
                )
            raise AIProviderError(
                f"OpenAI API error ({response.status_code}): {err_msg}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )

        try:
            async for event in parse_sse_events(response.aiter_lines()):
                data = event.get("data")
                if isinstance(data, dict):
                    choices = data.get("choices")
                    if isinstance(choices, list) and choices:
                        delta = choices[0].get("delta")
                        if isinstance(delta, dict):
                            content = delta.get("content")
                            if content and isinstance(content, str):
                                yield content
        finally:
            await response.aclose()

