from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import ClassVar

import pytest

from swiftpy.core.application import Application
from swiftpy.database.orm.model import Model
from swiftpy.http.response import Response, StreamingResponse, json_response
from swiftpy.testing.http_client import TestClient


@pytest.mark.asyncio
async def test_response_str_body_utf8_and_default_content_type() -> None:
    app = Application()
    client = TestClient(app)

    @app.get("/hello")
    async def hello() -> Response:
        return Response("héllo世界")

    res = await client.get("/hello")
    res.assert_ok()
    assert res.body == "héllo世界".encode()
    assert res.text == "héllo世界"
    res.assert_header("content-type", "text/plain; charset=utf-8")
    res.assert_header("content-length", str(len("héllo世界".encode())))


def test_response_crlf_injection_rejected() -> None:
    with pytest.raises(ValueError, match="CR or LF"):
        Response("ok", headers={"X-Injected": "val\r\nSet-Cookie: evil=1"})

    with pytest.raises(ValueError, match="CR or LF"):
        Response("ok", headers={"X-Bad\nName": "val"})

    resp = Response("ok")
    with pytest.raises(ValueError, match="CR or LF"):
        resp.headers["X-Test"] = "line1\rline2"

    with pytest.raises(ValueError, match="CR or LF"):
        resp.headers.add("Set-Cookie", "a=1\nb=2")


@pytest.mark.asyncio
async def test_response_case_insensitive_headers_no_duplicate_content_type() -> None:
    app = Application()
    client = TestClient(app)

    @app.get("/custom-json")
    async def custom_json() -> Response:
        return json_response(
            {"ok": True},
            headers={"Content-Type": "application/problem+json"},
        )

    res = await client.get("/custom-json")
    res.assert_ok()
    content_types = res.headers.getlist("content-type")
    assert content_types == ["application/problem+json"]
    assert res.headers["CONTENT-TYPE"] == "application/problem+json"


@pytest.mark.asyncio
async def test_response_multiple_set_cookie_headers_on_wire() -> None:
    app = Application()
    client = TestClient(app)

    @app.get("/cookies")
    async def cookies() -> Response:
        resp = Response("cookies set")
        resp.headers.add("Set-Cookie", "session=abc; Path=/; HttpOnly")
        resp.headers.add("Set-Cookie", "theme=dark; Path=/")
        return resp

    res = await client.get("/cookies")
    res.assert_ok()
    cookie_headers = res.headers.getlist("set-cookie")
    assert cookie_headers == [
        "session=abc; Path=/; HttpOnly",
        "theme=dark; Path=/",
    ]


def test_json_response_rejects_nan_and_infinity() -> None:
    with pytest.raises(ValueError):
        json_response({"bad": float("nan")})

    with pytest.raises(ValueError):
        json_response({"bad": float("inf")})


@pytest.mark.asyncio
async def test_streaming_response_does_not_force_content_length() -> None:
    app = Application()
    client = TestClient(app)

    async def gen() -> AsyncIterator[bytes]:
        yield b"chunk1-"
        yield b"chunk2"

    @app.get("/stream")
    async def stream_endpoint() -> StreamingResponse:
        return StreamingResponse(gen(), headers={"Content-Type": "text/plain"})

    res = await client.get("/stream")
    res.assert_ok()
    assert res.body == b"chunk1-chunk2"
    assert "content-length" not in res.headers


class OrderStatus(Enum):
    PENDING = "pending"
    PAID = "paid"


class InvoiceModel(Model):
    __table__ = "p4h2_invoices"
    __hidden__: ClassVar[tuple[str, ...]] = ("secret_token",)
    __guarded__: ClassVar[list[str]] = []

    id: int
    code: uuid.UUID
    amount: Decimal
    created_at: datetime
    secret_token: str


def test_json_encoding_types_and_models() -> None:
    dt = datetime(2026, 11, 7, 8, 15, 30, tzinfo=UTC)
    d = date(2026, 11, 7)
    t = time(8, 15, 30)
    uid = uuid.uuid4()
    price = Decimal("149.95")

    inv = InvoiceModel(
        id=1,
        code=uid,
        amount=price,
        created_at=dt,
        secret_token="top-secret-value-123",
    )

    # Model.to_dict() keeps raw Python types and strips __hidden__
    raw_dict = inv.to_dict()
    assert raw_dict["code"] == uid
    assert isinstance(raw_dict["code"], uuid.UUID)
    assert isinstance(raw_dict["amount"], Decimal)
    assert isinstance(raw_dict["created_at"], datetime)
    assert "secret_token" not in raw_dict

    # json_response encodes all supported types and nested models
    resp = json_response(
        {
            "dt": dt,
            "date": d,
            "time": t,
            "uuid": uid,
            "decimal": price,
            "enum": OrderStatus.PAID,
            "model": inv,
            "models": [inv],
        }
    )

    decoded = json.loads(resp.content.decode("utf-8"))
    assert decoded["dt"] == dt.isoformat()
    assert decoded["date"] == "2026-11-07"
    assert decoded["time"] == "08:15:30"
    assert decoded["uuid"] == str(uid)
    assert decoded["decimal"] == "149.95"
    assert decoded["enum"] == "paid"
    assert decoded["model"] == {
        "id": 1,
        "code": str(uid),
        "amount": "149.95",
        "created_at": dt.isoformat(),
    }
    assert "secret_token" not in decoded["model"]
    assert len(decoded["models"]) == 1
    assert decoded["models"][0]["id"] == 1

    # bytes must be rejected
    with pytest.raises(TypeError, match="not JSON serializable"):
        json_response({"raw": b"binary-bytes"})


@pytest.mark.asyncio
async def test_unsupported_json_type_returns_500_without_leaking_repr() -> None:
    class UnserializableSecret:
        def __repr__(self) -> str:
            return "<UnserializableSecret master_key='SUPER_SECRET_REPR_999'>"

    app = Application(debug=False)
    client = TestClient(app)

    @app.get("/bad-json")
    async def bad_json() -> Response:
        return json_response({"obj": UnserializableSecret()})

    res = await client.get("/bad-json")
    res.assert_status(500)
    assert res.json() == {"detail": "Internal Server Error"}
    assert "SUPER_SECRET_REPR_999" not in res.text

