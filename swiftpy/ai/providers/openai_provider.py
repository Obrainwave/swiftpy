from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

_OPENAI_HTTP_ERRORS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.RequestError)

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
    ToolCall,
    extract_error_message,
    parse_retry_after,
    strip_internal_extra,
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
        self._default_model: str = str(
            self.config.get("model") or self.config.get("default_model") or "gpt-4o"
        )
        self._base_url: str = str(
            self.config.get("base_url", "https://api.openai.com/v1")
        ).rstrip("/")
        self._timeout: float = float(self.config.get("timeout", 60.0))

    @property
    def name(self) -> str:
        return "openai"

    def get_default_model(self) -> str:
        return self._default_model

    def _build_payload(self, request: LLMRequest, *, stream: bool = False) -> tuple[str, dict[str, Any]]:
        model = request.model or self.get_default_model()
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in request.messages],
        }
        if stream:
            payload["stream"] = True
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop"] = request.stop
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.extra:
            payload.update(strip_internal_extra(request.extra))
        return model, payload

    def _raise_for_http_error(self, response: httpx.Response, body_text: str, data: Any) -> None:
        if response.status_code in (401, 403):
            err_msg = extract_error_message(data, body_text) or "Authentication failed"
            raise AIAuthenticationError(
                f"OpenAI authentication failed ({response.status_code}): {err_msg}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )
        if response.status_code == 429:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            err_msg = extract_error_message(data, body_text) or "Rate limit reached"
            raise AIRateLimitError(
                f"OpenAI rate limit exceeded: {err_msg}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
                retry_after=retry_after,
            )
        err_msg = extract_error_message(data, body_text) or f"HTTP {response.status_code}"
        raise AIProviderError(
            f"OpenAI API error ({response.status_code}): {err_msg}",
            provider=self.name,
            status_code=response.status_code,
            response_body=body_text,
        )

    async def send(self, request: LLMRequest) -> LLMResponse:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        model, payload = self._build_payload(request, stream=False)
        client = self._http_pool.get_client()

        async def _attempt() -> tuple[httpx.Response, float, str, Any]:
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
            try:
                data: Any = response.json()
            except (json.JSONDecodeError, ValueError):
                data = None

            if response.status_code >= 400:
                self._raise_for_http_error(response, body_text, data)
            return response, latency_ms, body_text, data

        response, latency_ms, body_text, data = await self._call_with_retries(_attempt)

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

        extracted_tool_calls: list[ToolCall] = []
        raw_tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if isinstance(raw_tool_calls, list):
            for tc in raw_tool_calls:
                if isinstance(tc, dict):
                    tc_id = str(tc.get("id") or "")
                    fn = tc.get("function") or {}
                    fn_name = str(fn.get("name") or "") if isinstance(fn, dict) else ""
                    args_raw = fn.get("arguments") if isinstance(fn, dict) else {}
                    if isinstance(args_raw, str):
                        try:
                            args_parsed = json.loads(args_raw)
                        except (json.JSONDecodeError, TypeError):
                            args_parsed = {"raw": args_raw}
                    elif isinstance(args_raw, dict):
                        args_parsed = args_raw
                    else:
                        args_parsed = {}
                    extracted_tool_calls.append(
                        ToolCall(id=tc_id, name=fn_name, arguments=args_parsed)
                    )

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
            tool_calls=extracted_tool_calls,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        _, payload = self._build_payload(request, stream=True)
        client = self._http_pool.get_client()

        async def _open_stream() -> httpx.Response:
            try:
                req = client.build_request(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout,
                )
                resp = await client.send(req, stream=True)
            except _OPENAI_HTTP_ERRORS as exc:
                sanitized_exc = self.sanitize_error(str(exc))
                raise AIConnectionError(
                    f"Failed to connect to OpenAI stream ({type(exc).__name__}): {sanitized_exc}",
                    provider=self.name,
                ) from exc

            if resp.status_code >= 400:
                raw_bytes = await resp.aread()
                body_text = self.sanitize_error(raw_bytes.decode("utf-8", errors="replace"))
                await resp.aclose()
                try:
                    err_data: Any = json.loads(body_text)
                except (json.JSONDecodeError, ValueError):
                    err_data = None
                self._raise_for_http_error(resp, body_text, err_data)
            return resp

        response = await self._call_with_retries(_open_stream)

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

