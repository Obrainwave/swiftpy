import asyncio
import json
from typing import Any


def json_response(
    status: int,
    data: dict[str, Any],
) -> bytes:
    """
    Create a JSON response body.

    The ASGI send() call expects bytes.
    """

    return json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


async def send_response(
    send,
    status: int,
    payload: dict[str, Any],
) -> None:
    """
    Send an HTTP response according to the ASGI spec.
    """

    body = json_response(status, payload)

    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )

    await send(
        {
            "type": "http.response.body",
            "body": body,
        }
    )


async def read_request_body(receive) -> bytes:
    """
    Read the complete request body.

    ASGI may deliver the body in chunks.
    """

    chunks: list[bytes] = []

    while True:
        message = await receive()

        if message["type"] != "http.request":
            continue

        chunks.append(message.get("body", b""))

        if not message.get("more_body", False):
            break

    return b"".join(chunks)


async def app(scope, receive, send):
    """
    Minimal ASGI application.

    Supports:
    - GET /ping
    - GET /slow
    - POST /echo
    """

    if scope["type"] != "http":
        return

    method = scope["method"]
    path = scope["path"]

    # GET /ping

    if method == "GET" and path == "/ping":
        await send_response(
            send,
            200,
            {
                "status": "ok",
                "message": "pong",
            },
        )
        return

    # GET /slow

    if method == "GET" and path == "/slow":
        await asyncio.sleep(1)

        await send_response(
            send,
            200,
            {
                "status": "ok",
                "message": "slow complete",
            },
        )
        return

    # POST /echo

    if method == "POST" and path == "/echo":
        body = await read_request_body(receive)

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            await send_response(
                send,
                400,
                {
                    "error": "Invalid JSON",
                },
            )
            return

        await send_response(
            send,
            200,
            {
                "received": payload,
            },
        )
        return

    # 404

    await send_response(
        send,
        404,
        {
            "error": "Not Found",
        },
    )
