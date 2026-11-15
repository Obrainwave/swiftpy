from __future__ import annotations

import codecs
import json
from collections.abc import AsyncIterator
from typing import Any

from swiftpy.ai.exceptions import AIProviderError


async def _iter_sse_lines(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Expand chunks containing embedded newlines into individual SSE lines."""
    async for chunk in lines:
        if not chunk:
            yield ""
            continue
        for line in chunk.splitlines():
            yield line


def _parse_sse_event_payload(full_data: str, current_event: str) -> dict[str, Any]:
    """Parse buffered SSE event data and raise AIProviderError on active provider errors."""
    try:
        parsed_json = json.loads(full_data)
    except (json.JSONDecodeError, ValueError):
        return {"event": current_event, "data": full_data}

    if isinstance(parsed_json, dict):
        err_obj = parsed_json.get("error")
        if parsed_json.get("type") == "error" or (
            err_obj is not None and err_obj is not False
        ):
            payload_err = err_obj if err_obj is not None else parsed_json
            err_msg = (
                payload_err.get("message")
                if isinstance(payload_err, dict)
                else str(payload_err)
            )
            raise AIProviderError(f"Stream error: {err_msg}")
    return {"event": current_event, "data": parsed_json}


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

    async for raw_line in _iter_sse_lines(lines):
        line = raw_line.rstrip("\r\n")

        if not line:
            # Empty line indicates event boundary
            if data_buffer:
                full_data = "\n".join(data_buffer)
                data_buffer.clear()
                if full_data == "[DONE]":
                    break
                yield _parse_sse_event_payload(full_data, current_event)
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
            yield _parse_sse_event_payload(full_data, current_event)


async def split_chunk_lines(
    chunks: AsyncIterator[str | bytes],
) -> AsyncIterator[str]:
    """
    Split arbitrary incoming network chunks into individual lines, buffering
    partial lines and multi-byte UTF-8 sequences across chunk boundaries.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    async for chunk in chunks:
        if isinstance(chunk, (bytes, bytearray)):
            text = decoder.decode(chunk, final=False)
        else:
            text = chunk
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    tail = decoder.decode(b"", final=True)
    if tail:
        buffer += tail
    if buffer:
        yield buffer
