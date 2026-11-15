from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

_ANTHROPIC_HTTP_ERRORS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.RequestError)

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


def _coerce_tool_input(raw_args: Any) -> dict[str, Any]:
    """Ensure tool_use input sent to Anthropic is always a JSON object dictionary."""
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        return {"raw": raw_args}
    if raw_args is None:
        return {}
    return {"value": raw_args}


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
            self.config.get("model")
            or self.config.get("default_model")
            or "claude-3-5-sonnet-20241022"
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

    def _build_payload(
        self,
        request: LLMRequest,
        *,
        stream: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        model = request.model or self.get_default_model()

        system_parts: list[str] = []
        dialogue_messages: list[dict[str, Any]] = []
        for msg in request.messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            elif msg.role == "tool":
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id or "",
                    "content": msg.content,
                }
                if (
                    dialogue_messages
                    and dialogue_messages[-1]["role"] == "user"
                    and isinstance(dialogue_messages[-1]["content"], list)
                    and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in dialogue_messages[-1]["content"]
                    )
                ):
                    dialogue_messages[-1]["content"].append(tool_result_block)
                else:
                    dialogue_messages.append({
                        "role": "user",
                        "content": [tool_result_block],
                    })
            elif msg.role == "assistant" and msg.tool_calls:
                content_blocks: list[dict[str, Any]] = []
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": _coerce_tool_input(tc.arguments),
                    })
                dialogue_messages.append({"role": "assistant", "content": content_blocks})
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
        if stream:
            payload["stream"] = True

        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop_sequences"] = request.stop
        if request.tools is not None:
            anthropic_tools: list[dict[str, Any]] = []
            for t in request.tools:
                if "input_schema" in t:
                    anthropic_tools.append(t)
                elif "function" in t:
                    fn = t["function"]
                    anthropic_tools.append({
                        "name": fn.get("name"),
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters", {}),
                    })
                elif "parameters" in t:
                    anthropic_tools.append({
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "input_schema": t.get("parameters", {}),
                    })
                else:
                    anthropic_tools.append(t)
            payload["tools"] = anthropic_tools
        if request.extra:
            payload.update(strip_internal_extra(request.extra))

        return model, payload

    def _raise_for_http_error(self, response: httpx.Response, body_text: str, data: Any) -> None:
        if response.status_code in (401, 403):
            err_msg = extract_error_message(data, body_text) or "Authentication failed"
            raise AIAuthenticationError(
                f"Anthropic authentication failed ({response.status_code}): {err_msg}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )
        if response.status_code == 429:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            err_msg = extract_error_message(data, body_text) or "Rate limit reached"
            raise AIRateLimitError(
                f"Anthropic rate limit exceeded: {err_msg}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
                retry_after=retry_after,
            )
        err_msg = extract_error_message(data, body_text) or f"HTTP {response.status_code}"
        raise AIProviderError(
            f"Anthropic API error ({response.status_code}): {err_msg}",
            provider=self.name,
            status_code=response.status_code,
            response_body=body_text,
        )

    async def send(self, request: LLMRequest) -> LLMResponse:
        url = f"{self._base_url}/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "content-type": "application/json",
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
            except _ANTHROPIC_HTTP_ERRORS as exc:
                sanitized_exc = self.sanitize_error(str(exc))
                raise AIConnectionError(
                    f"Failed to connect to Anthropic ({type(exc).__name__}): {sanitized_exc}",
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
                f"Anthropic returned non-JSON response ({response.status_code}): {body_text[:200]}",
                provider=self.name,
                status_code=response.status_code,
                response_body=body_text,
            )

        raw_content = data.get("content")
        text_parts: list[str] = []
        extracted_tool_calls: list[ToolCall] = []
        if isinstance(raw_content, list):
            for block in raw_content:
                if isinstance(block, dict):
                    b_type = block.get("type")
                    if b_type == "text":
                        text_parts.append(str(block.get("text") or ""))
                    elif b_type == "tool_use":
                        tc_id = str(block.get("id") or "")
                        tc_name = str(block.get("name") or "")
                        tc_input = block.get("input")
                        tc_args = tc_input if isinstance(tc_input, dict) else {}
                        extracted_tool_calls.append(ToolCall(id=tc_id, name=tc_name, arguments=tc_args))
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
            tool_calls=extracted_tool_calls,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        url = f"{self._base_url}/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "content-type": "application/json",
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
            except _ANTHROPIC_HTTP_ERRORS as exc:
                sanitized_exc = self.sanitize_error(str(exc))
                raise AIConnectionError(
                    f"Failed to connect to Anthropic stream ({type(exc).__name__}): {sanitized_exc}",
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

