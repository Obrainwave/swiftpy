from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


class Response:
    def __init__(
        self,
        content: bytes | str = b"",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    async def send(self, send: Any) -> None:
        headers = [
            (
                k.encode(),
                v.encode(),
            )
            for k, v in self.headers.items()
        ]

        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": headers,
            }
        )

        await send(
            {
                "type": "http.response.body",
                "body": self.content,
            }
        )


class StreamingResponse(Response):
    def __init__(
        self,
        iterator: AsyncIterator[bytes],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            content=b"",
            status_code=status_code,
            headers=headers,
        )

        self.iterator = iterator

    async def send(self, send: Any) -> None:
        headers = [
            (key.encode(), value.encode()) for key, value in self.headers.items()
        ]

        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": headers,
            }
        )

        async for chunk in self.iterator:
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                }
            )

        await send(
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            }
        )


def json_response(
    data: Any,
    status_code: int = 200,
) -> Response:
    return Response(
        content=json.dumps(data).encode(),
        status_code=status_code,
        headers={
            "content-type": "application/json",
        },
    )


def created(data: Any) -> Response:
    return json_response(
        data,
        status_code=201,
    )


def not_found(message: str = "Not Found") -> Response:
    return json_response(
        {"detail": message},
        status_code=404,
    )


def unprocessable(
    errors: Any,
) -> Response:
    return json_response(
        {"errors": errors},
        status_code=422,
    )


def stream(
    iterator: AsyncIterator[bytes],
    *,
    status_code: int = 200,
    content_type: str = "application/octet-stream",
) -> StreamingResponse:
    return StreamingResponse(
        iterator=iterator,
        status_code=status_code,
        headers={
            "content-type": content_type,
        },
    )
