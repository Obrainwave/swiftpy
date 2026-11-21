from __future__ import annotations

import functools
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.http.request import Request
from swiftpy.http.response import encode_json
from swiftpy.testing import TestClient


def _build_multipart_body(
    boundary: str,
    *,
    fields: dict[str, str] | None = None,
    files: list[tuple[str, str, bytes, str]] | None = None,
) -> bytes:
    chunks: list[bytes] = []
    for name, value in (fields or {}).items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    for field_name, filename, content, content_type in files or []:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


async def test_405_method_not_allowed_mentions_route_instead_of_path() -> None:
    app = Application(debug=False)

    @app.get("/v1/admin/docs/groups/update")
    async def show_group() -> dict[str, str]:
        return {"ok": "true"}

    async with TestClient(app) as client:
        res = await client.put("/v1/admin/docs/groups/update")
        assert res.status_code == 405
        assert res.headers.get("Allow") == "GET, HEAD"
        assert (
            res.json()["detail"]
            == "Method 'PUT' not allowed for route '/v1/admin/docs/groups/update'. Allowed: GET, HEAD"
        )


async def test_request_has_file_sync_and_async_inside_routes() -> None:
    app = Application(debug=False)

    @app.post("/upload-sync")
    async def upload_sync(request: Request) -> dict[str, Any]:
        has_img = bool(request.has_file("image"))
        has_missing = bool(request.has_file("missing"))
        has_note = bool(request.has_file("note"))
        uploaded = await request.file("image") if request.has_file("image") else None
        return {
            "has_image": has_img,
            "has_missing": has_missing,
            "has_note": has_note,
            "filename": uploaded.filename if uploaded is not None else None,
            "note_value": request.get("note"),
        }

    @app.put("/upload-async")
    async def upload_async(request: Request) -> dict[str, Any]:
        has_img = await request.has_file("image")
        has_missing = await request.has_file("missing")
        has_note = await request.has_file("note")
        return {
            "has_image": has_img,
            "has_missing": has_missing,
            "has_note": has_note,
        }

    def mark_wrapper(
        fn: Callable[[Request], Awaitable[dict[str, Any]]],
    ) -> Callable[[Request], Awaitable[dict[str, Any]]]:
        @functools.wraps(fn)
        async def wrapper(request: Request) -> dict[str, Any]:
            return await fn(request)

        return wrapper

    @app.patch("/upload-decorated")
    @mark_wrapper
    async def upload_decorated(request: Request) -> dict[str, Any]:
        return {"has_image": bool(request.has_file("image"))}

    boundary = "----SwiftPYHasFileBoundary"
    with_file_body = _build_multipart_body(
        boundary,
        fields={"note": "avatar upload"},
        files=[("image", "avatar.png", b"\x89PNG\r\n", "image/png")],
    )
    empty_file_body = _build_multipart_body(
        boundary,
        fields={"note": "no file chosen"},
        files=[("image", "", b"", "application/octet-stream")],
    )
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}

    async with TestClient(app) as client:
        res1 = await client.post("/upload-sync", data=with_file_body, headers=headers)
        assert res1.status_code == 200
        assert res1.json() == {
            "has_image": True,
            "has_missing": False,
            "has_note": False,
            "filename": "avatar.png",
            "note_value": "avatar upload",
        }

        res2 = await client.post("/upload-sync", data=empty_file_body, headers=headers)
        assert res2.status_code == 200
        assert res2.json() == {
            "has_image": False,
            "has_missing": False,
            "has_note": False,
            "filename": None,
            "note_value": "no file chosen",
        }

        res3 = await client.put("/upload-async", data=with_file_body, headers=headers)
        assert res3.status_code == 200
        assert res3.json() == {
            "has_image": True,
            "has_missing": False,
            "has_note": False,
        }

        res4 = await client.patch(
            "/upload-decorated", data=with_file_body, headers=headers
        )
        assert res4.status_code == 200
        assert res4.json() == {"has_image": True}

        res_json = await client.post("/upload-sync", json={"image": "not-a-file"})
        assert res_json.status_code == 200
        assert res_json.json()["has_image"] is False


async def test_request_has_file_preserves_lazy_multipart_when_opt_out_or_unused() -> None:
    app_no_auto = Application(debug=False, auto_load_request_input=False)
    multipart_calls = 0

    @app_no_auto.post("/lazy-await")
    async def lazy_await(request: Request) -> dict[str, Any]:
        # Without auto_load, sync check is False until awaited; await loads multipart
        sync_before = bool(request.has_file("image"))
        async_after = await request.has_file("image")
        return {"sync_before": sync_before, "async_after": async_after}

    @app_no_auto.post("/unused-multipart")
    async def unused_multipart(request: Request) -> dict[str, Any]:
        return {"ok": True}

    boundary = "----SwiftPYLazyBoundary"
    body = _build_multipart_body(
        boundary,
        files=[("image", "doc.pdf", b"%PDF-1.4", "application/pdf")],
    )
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}

    async with TestClient(app_no_auto) as client:
        res = await client.post("/lazy-await", data=body, headers=headers)
        assert res.status_code == 200
        assert res.json() == {"sync_before": False, "async_after": True}

    # Verify default Application still skips eager multipart read when handler does not use has_file
    default_app = Application(debug=False)

    @default_app.post("/raw-upload")
    async def raw_upload(request: Request) -> dict[str, Any]:
        return {"form_loaded": request._form is not None, "ok": True}

    async def counting_receive() -> dict[str, Any]:
        nonlocal multipart_calls
        multipart_calls += 1
        return {"type": "http.request", "body": body, "more_body": False}

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/raw-upload",
            "headers": [
                (
                    b"content-type",
                    f"multipart/form-data; boundary={boundary}".encode(),
                )
            ],
        },
        counting_receive,
    )
    assert not req.has_file("image")
    assert req.has_file("image").__eq__(False) is True
    assert req.has_file("image").__eq__(True) is False
    assert multipart_calls == 0

    async with TestClient(default_app) as client:
        raw_res = await client.post("/raw-upload", data=body, headers=headers)
        assert raw_res.status_code == 200
        assert raw_res.json() == {"form_loaded": False, "ok": True}


class _StatusEnum(Enum):
    ACTIVE = "active"


async def test_encode_json_serializes_has_file_result_in_nested_dicts_and_preserves_existing_types() -> None:
    app = Application(debug=False)

    @app.post("/direct-json-serialize")
    async def direct_json_serialize(request: Request) -> dict[str, Any]:
        # Return _HasFileResult directly inside nested dicts/lists without bool() or await
        return {
            "files": {
                "image": request.has_file("image"),
                "avatar": request.has_file("avatar"),
            },
            "flags": [request.has_file("image"), request.has_file("missing")],
        }

    boundary = "----SwiftPYJsonSerializeBoundary"
    body = _build_multipart_body(
        boundary,
        files=[("image", "photo.jpg", b"\xff\xd8\xff", "image/jpeg")],
    )
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}

    async with TestClient(app) as client:
        res = await client.post("/direct-json-serialize", data=body, headers=headers)
        assert res.status_code == 200
        assert res.json() == {
            "files": {"image": True, "avatar": False},
            "flags": [True, False],
        }

    # Verify existing encode_json behavior (datetime, UUID, Decimal, Enum, and rejecting bytes)
    sample_id = uuid.uuid4()
    ts = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    encoded = encode_json(
        {
            "id": sample_id,
            "ts": ts,
            "amount": Decimal("19.99"),
            "status": _StatusEnum.ACTIVE,
        }
    )
    decoded = json.loads(encoded.decode("utf-8"))
    assert decoded == {
        "id": str(sample_id),
        "ts": ts.isoformat(),
        "amount": "19.99",
        "status": "active",
    }

    with pytest.raises(TypeError, match="not JSON serializable"):
        encode_json({"raw": b"binary"})

