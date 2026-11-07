from __future__ import annotations

from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.http.exceptions import ClientDisconnect
from swiftpy.http.request import DEFAULT_MAX_BODY_SIZE, Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient


def test_default_max_body_size_is_2_mib() -> None:
    assert DEFAULT_MAX_BODY_SIZE == 2 * 1024 * 1024


def test_latin1_headers_cached_case_insensitive_and_duplicate_getlist() -> None:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/headers",
        "headers": [
            (b"x-cafe", "café".encode("latin-1")),
            (b"x-multi", b"first"),
            (b"X-Multi", b"second"),
            (b"x-raw-byte", b"\xff"),
        ],
    }

    async def dummy_receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request(scope, dummy_receive)

    # Cached object identity on repeated property access
    headers_first = req.headers
    headers_second = req.headers
    assert headers_first is headers_second

    # Latin-1 decoding works without UnicodeDecodeError
    assert req.headers["x-cafe"] == "café"
    assert req.headers["X-CAFE"] == "café"
    assert req.headers["x-raw-byte"] == "\xff"

    # Duplicate headers: getlist returns all in order, dict access is last-wins
    assert req.headers.getlist("x-multi") == ["first", "second"]
    assert req.headers.getlist("X-MULTI") == ["first", "second"]
    assert req.headers["x-multi"] == "second"
    assert req.headers.get("X-Multi") == "second"


def test_query_params_utf8_with_replace_and_cached() -> None:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/search",
        "query_string": b"tag=a&tag=b&bad=\xff&utf8=%C3%A9",
    }

    async def dummy_receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request(scope, dummy_receive)

    qp1 = req.query_params
    qp2 = req.query_params
    assert qp1 is qp2
    assert qp1["tag"] == "a"
    assert qp1["utf8"] == "é"
    assert "\ufffd" in qp1["bad"]

    qpl1 = req.query_param_list
    qpl2 = req.query_param_list
    assert qpl1 is qpl2
    assert qpl1["tag"] == ["a", "b"]


@pytest.mark.asyncio
async def test_body_size_limit_with_content_length_and_chunked_streaming() -> None:
    app = Application(auto_discover_providers=False, max_body_size=100)
    client = TestClient(app)

    @app.post("/echo-size")
    async def echo_size(request: Request) -> Response:
        body = await request.body()
        return json_response({"size": len(body)})

    @app.post("/small-route", max_body_size=10)
    async def small_route(request: Request) -> Response:
        body = await request.body()
        return json_response({"size": len(body)})

    # 1. Just under/at the 100-byte app limit succeeds
    res_ok = await client.post("/echo-size", data=b"a" * 100)
    res_ok.assert_ok()
    assert res_ok.json() == {"size": 100}

    # 2. Over the 100-byte app limit with Content-Length -> 413
    res_over_cl = await client.post("/echo-size", data=b"a" * 101)
    res_over_cl.assert_status(413)

    # 3. Per-route override (max_body_size=10) rejects 11 bytes with 413
    res_route_ok = await client.post("/small-route", data=b"0123456789")
    res_route_ok.assert_ok()
    res_route_over = await client.post("/small-route", data=b"0123456789X")
    res_route_over.assert_status(413)

    # 4. Chunked streaming without Content-Length header over 100 bytes -> 413
    chunks = [b"x" * 40, b"y" * 40, b"z" * 25]
    chunk_idx = 0

    async def chunked_receive() -> dict[str, Any]:
        nonlocal chunk_idx
        if chunk_idx < len(chunks):
            chunk = chunks[chunk_idx]
            chunk_idx += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": chunk_idx < len(chunks),
            }
        return {"type": "http.disconnect"}

    sent_messages: list[dict[str, Any]] = []

    async def capture_send(msg: dict[str, Any]) -> None:
        sent_messages.append(msg)

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/echo-size",
        "headers": [],
        "query_string": b"",
    }
    await app(scope, chunked_receive, capture_send)
    assert sent_messages[0]["type"] == "http.response.start"
    assert sent_messages[0]["status"] == 413


@pytest.mark.asyncio
async def test_disconnect_mid_body_raises_client_disconnect_and_sends_no_500() -> None:
    app = Application(auto_discover_providers=False)
    handler_finished = False

    @app.post("/upload")
    async def upload(request: Request) -> Response:
        nonlocal handler_finished
        await request.body()
        handler_finished = True
        return json_response({"ok": True})

    step = 0

    async def disconnecting_receive() -> dict[str, Any]:
        nonlocal step
        step += 1
        if step == 1:
            return {"type": "http.request", "body": b"partial-", "more_body": True}
        return {"type": "http.disconnect"}

    # Direct Request.body() raises ClientDisconnect
    req = Request(
        {"type": "http", "method": "POST", "path": "/upload", "headers": []},
        disconnecting_receive,
    )
    with pytest.raises(ClientDisconnect):
        await req.body()

    # Full Application dispatch catches ClientDisconnect cleanly without sending a 500
    step = 0
    sent_messages: list[dict[str, Any]] = []

    async def capture_send(msg: dict[str, Any]) -> None:
        sent_messages.append(msg)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/upload",
            "headers": [],
            "query_string": b"",
        },
        disconnecting_receive,
        capture_send,
    )
    assert not handler_finished
    assert sent_messages == []


@pytest.mark.asyncio
async def test_deeply_nested_and_invalid_json_returns_400() -> None:
    app = Application(auto_discover_providers=False)
    client = TestClient(app)

    @app.post("/parse-json")
    async def parse_json(request: Request) -> Response:
        data = await request.json()
        return json_response({"parsed": data})

    # Deeply nested JSON triggers RecursionError -> 400 Bad Request (not 500)
    deep_payload = b"[" * 100000 + b"]" * 100000
    res_deep = await client.post("/parse-json", data=deep_payload)
    res_deep.assert_bad_request()

    # Malformed JSON -> 400 Bad Request
    res_malformed = await client.post("/parse-json", data=b"{not-valid-json")
    res_malformed.assert_bad_request()

