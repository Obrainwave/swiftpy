from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from swiftpy.ai.exceptions import AIProviderError


async def parse_sse_events(
    lines: AsyncIterator[str],
) -> AsyncIterator[dict[str, Any]]:
    """
    Parse an asynchronous stream of Server-Sent Event (SSE) lines into event dictionaries.

    Supports:
        - CRLF (\\r\\n) and LF (\\n) line endings
        - Multiple data: lines within a single event (joined by newline per SSE specification)
        - Event type identifiers (event: <name>)
        - SSE comments (: ping/keepalive)
        - Provider error detection
        - Clean [DONE] termination
    """
    current_event = "message"
    data_buffer: list[str] = []

    async for raw_line in lines:
        line = raw_line.rstrip("\r\n")

        if not line:
            # Empty line indicates event boundary
            if data_buffer:
                full_data = "\n".join(data_buffer)
                data_buffer.clear()
                if full_data == "[DONE]":
                    break

                try:
                    parsed_json = json.loads(full_data)
                    # Check for provider stream error payload
                    if isinstance(parsed_json, dict) and (
                        parsed_json.get("type") == "error" or "error" in parsed_json
                    ):
                        err_obj = parsed_json.get("error", parsed_json)
                        err_msg = (
                            err_obj.get("message")
                            if isinstance(err_obj, dict)
                            else str(err_obj)
                        )
                        raise AIProviderError(f"Stream error: {err_msg}")
                    yield {"event": current_event, "data": parsed_json}
                except (json.JSONDecodeError, ValueError):
                    yield {"event": current_event, "data": full_data}
                current_event = "message"
            continue

        if line.startswith(":"):
            # SSE comment line, ignore
            continue

        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            # Strip leading space after data: if present per spec
            val = line[len("data:") :]
            val = val.removeprefix(" ")
            data_buffer.append(val)

    # Flush remaining data if stream closed without trailing empty line
    if data_buffer:
        full_data = "\n".join(data_buffer)
        if full_data != "[DONE]":
            try:
                parsed_json = json.loads(full_data)
                if isinstance(parsed_json, dict) and (
                    parsed_json.get("type") == "error" or "error" in parsed_json
                ):
                    err_obj = parsed_json.get("error", parsed_json)
                    err_msg = (
                        err_obj.get("message")
                        if isinstance(err_obj, dict)
                        else str(err_obj)
                    )
                    raise AIProviderError(f"Stream error: {err_msg}")
                yield {"event": current_event, "data": parsed_json}
            except (json.JSONDecodeError, ValueError):
                yield {"event": current_event, "data": full_data}


async def split_chunk_lines(
    chunks: AsyncIterator[str | bytes],
) -> AsyncIterator[str]:
    """
    Split arbitrary incoming network chunks into individual lines, buffering
    partial lines that cross chunk boundaries.
    """
    buffer = ""
    async for chunk in chunks:
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    if buffer:
        yield buffer
