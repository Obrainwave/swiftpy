from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest

from swiftpy.core.application import Application
from swiftpy.core.container import Container
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.schemas import BaseSchema, Field, Schema
from swiftpy.testing.http_client import TestClient


class EggSpoilageCreateRequest(BaseSchema):
    movement_date: date
    flock_id: UUID | None = None
    spoiled_or_broken_eggs: int = Field(..., gt=0)
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = None


class EggCollectionCreateRequest(Schema):
    movement_date: date
    flock_id: UUID | None = None
    good_eggs: int = Field(..., ge=0)
    cracked_eggs: int = Field(default=0, ge=0)
    dirty_eggs: int = Field(default=0, ge=0)
    spoiled_eggs: int = Field(default=0, ge=0)
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = None


class EggCollectionResponse(BaseSchema):
    id: UUID
    organization_id: str
    movement_date: date
    good_eggs_delta: int
    total_damaged: int
    reference: str
    estimated_weight_kg: Decimal = Decimal("1.25")
    tags: set[str] = Field(default_factory=set)
    internal_trace: str | None = Field(default=None, exclude=True)


class _StrictStopFirstSchema(BaseSchema):
    stop_on_first_error = True
    extra = "forbid"

    sku: str = Field(..., min_length=3)
    qty: int = Field(..., gt=0)


class _EggLedgerService:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_collection(
        self,
        organization_id: str,
        payload: EggCollectionCreateRequest,
    ) -> EggCollectionResponse:
        ref = payload.reference or f"EGG-COL-{payload.movement_date.strftime('%Y%m%d')}"
        self.recorded.append(
            {
                "organization_id": organization_id,
                "good_eggs": payload.good_eggs,
                "reference": ref,
            }
        )
        return EggCollectionResponse(
            id=uuid4(),
            organization_id=organization_id,
            movement_date=payload.movement_date,
            good_eggs_delta=payload.good_eggs,
            total_damaged=(
                payload.cracked_eggs + payload.dirty_eggs + payload.spoiled_eggs
            ),
            reference=ref,
            tags={"daily", "verified"},
            internal_trace="should-never-serialize",
        )


class _EggCollectionController:
    def __init__(self, service: _EggLedgerService) -> None:
        self.service = service

    async def record(
        self,
        organization_id: str,
        payload: EggCollectionCreateRequest,
    ) -> EggCollectionResponse:
        return await self.service.record_collection(organization_id, payload)

    async def record_spoilage(
        self,
        payload: EggSpoilageCreateRequest,
    ) -> Response:
        return json_response(
            {
                "movement_date": payload.movement_date,
                "spoiled_or_broken_eggs": payload.spoiled_or_broken_eggs,
                "flock_id": payload.flock_id,
            },
            status_code=201,
        )


def test_container_never_auto_wires_unbound_schema() -> None:
    container = Container()
    assert container.can_resolve(EggCollectionCreateRequest) is False
    assert container.can_resolve(_EggLedgerService) is True

    explicit = EggCollectionCreateRequest(
        movement_date=date(2026, 11, 25),
        good_eggs=10,
    )
    container.instance(EggCollectionCreateRequest, explicit)
    assert container.can_resolve(EggCollectionCreateRequest) is True
    assert container.resolve(EggCollectionCreateRequest) is explicit


async def test_router_injects_schema_in_function_and_controller_handlers() -> None:
    app = Application(debug=False)
    app.container.singleton(_EggLedgerService, _EggLedgerService)

    @app.post("/spoilage")
    async def create_spoilage(payload: EggSpoilageCreateRequest) -> Response:
        return json_response(
            {
                "movement_date": payload.movement_date,
                "spoiled_or_broken_eggs": payload.spoiled_or_broken_eggs,
                "reference": payload.reference,
                "notes": payload.notes,
            },
            status_code=201,
        )

    @app.post("/batch-items")
    async def batch_items(payload: EggSpoilageCreateRequest) -> list[BaseSchema]:
        return [
            payload,
            EggCollectionResponse(
                id=uuid4(),
                organization_id="org-1",
                movement_date=payload.movement_date,
                good_eggs_delta=payload.spoiled_or_broken_eggs,
                total_damaged=0,
                reference=payload.reference or "REF-1",
            ),
        ]

    app.post(
        "/orgs/{organization_id}/collections",
        _EggCollectionController.record,
    )
    app.post("/controller-spoilage", _EggCollectionController.record_spoilage)

    async with TestClient(app) as client:
        res = await client.post(
            "/spoilage",
            json={
                "movement_date": "2026-11-25",
                "spoiled_or_broken_eggs": "12",
                "reference": "SP-001",
            },
        )
        assert res.status_code == 201
        assert res.json() == {
            "movement_date": "2026-11-25",
            "spoiled_or_broken_eggs": 12,
            "reference": "SP-001",
            "notes": None,
        }

        flock_uuid = str(uuid4())
        ctrl_res = await client.post(
            "/orgs/org-42/collections",
            json={
                "movement_date": "2026-11-25",
                "flock_id": flock_uuid,
                "good_eggs": "240",
                "cracked_eggs": 3,
                "dirty_eggs": "2",
            },
        )
        assert ctrl_res.status_code == 200
        ctrl_body = ctrl_res.json()
        assert ctrl_body["organization_id"] == "org-42"
        assert ctrl_body["movement_date"] == "2026-11-25"
        assert ctrl_body["good_eggs_delta"] == 240
        assert ctrl_body["total_damaged"] == 5
        assert ctrl_body["reference"] == "EGG-COL-20261125"
        assert ctrl_body["estimated_weight_kg"] == "1.25"
        assert sorted(ctrl_body["tags"]) == ["daily", "verified"]
        assert "internal_trace" not in ctrl_body

        ledger = app.container.resolve(_EggLedgerService)
        assert len(ledger.recorded) == 1
        assert ledger.recorded[0]["good_eggs"] == 240

        batch_res = await client.post(
            "/batch-items",
            json={"movement_date": "2026-11-25", "spoiled_or_broken_eggs": 4},
        )
        assert batch_res.status_code == 200
        batch_body = batch_res.json()
        assert len(batch_body) == 2
        assert batch_body[0]["spoiled_or_broken_eggs"] == 4
        assert batch_body[1]["reference"] == "REF-1"


async def test_router_schema_supports_form_and_multipart_bodies() -> None:
    app = Application(debug=False)

    @app.post("/spoilage-form")
    async def spoilage_form(
        payload: EggSpoilageCreateRequest,
    ) -> EggSpoilageCreateRequest:
        return payload

    async with TestClient(app) as client:
        encoded = urlencode(
            {
                "movement_date": "2026-11-25",
                "spoiled_or_broken_eggs": "7",
                "reference": "FORM-01",
            }
        ).encode()
        res = await client.post(
            "/spoilage-form",
            data=encoded,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert res.status_code == 200
        assert res.json()["spoiled_or_broken_eggs"] == 7
        assert res.json()["reference"] == "FORM-01"

        boundary = "----SwiftPYSchemaFormBoundary"
        multipart = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="movement_date"\r\n\r\n'
            "2026-11-25\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="spoiled_or_broken_eggs"\r\n\r\n'
            "9\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        mp_res = await client.post(
            "/spoilage-form",
            data=multipart,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        assert mp_res.status_code == 200
        assert mp_res.json()["spoiled_or_broken_eggs"] == 9


async def test_router_schema_returns_422_on_invalid_or_malformed_payloads() -> None:
    app = Application(debug=False)

    @app.post("/spoilage")
    async def create_spoilage(payload: EggSpoilageCreateRequest) -> dict[str, Any]:
        return payload.to_dict()

    @app.post("/strict")
    async def strict_endpoint(payload: _StrictStopFirstSchema) -> dict[str, Any]:
        return payload.to_dict()

    async with TestClient(app) as client:
        # Invalid field constraint + missing required field
        bad_res = await client.post(
            "/spoilage",
            json={"spoiled_or_broken_eggs": 0, "reference": "x" * 101},
        )
        assert bad_res.status_code == 422
        bad_body = bad_res.json()
        assert "errors" in bad_body
        assert "movement_date" in bad_body["errors"]
        assert "spoiled_or_broken_eggs" in bad_body["errors"]
        assert "reference" in bad_body["errors"]

        # Empty body
        empty_res = await client.post(
            "/spoilage",
            data=b"",
            headers={"content-type": "application/json"},
        )
        assert empty_res.status_code == 422
        assert "body" in empty_res.json()["errors"]

        # Malformed JSON
        malformed_res = await client.post(
            "/spoilage",
            data=b"{bad json",
            headers={"content-type": "application/json"},
        )
        assert malformed_res.status_code == 422
        assert "body" in malformed_res.json()["errors"]

        # Non-object JSON
        list_res = await client.post("/spoilage", json=[1, 2, 3])
        assert list_res.status_code == 422
        assert "body" in list_res.json()["errors"]

        # stop_on_first_error + extra = "forbid"
        strict_res = await client.post(
            "/strict",
            json={"unknown_key": 1, "sku": "X", "qty": 0},
        )
        assert strict_res.status_code == 422
        strict_body = strict_res.json()
        assert "message" in strict_body
        assert strict_body["field"] == "unknown_key"


async def test_router_schema_with_request_param_avoids_duplicate_input_preload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Application(debug=False)
    preload_calls = 0
    original_input = Request.input

    async def _counting_input(
        self: Request,
        key: str | None = None,
        default: Any = None,
        *,
        _preload: bool = False,
    ) -> Any:
        nonlocal preload_calls
        if _preload:
            preload_calls += 1
        return await original_input(self, key, default, _preload=_preload)

    monkeypatch.setattr(Request, "input", _counting_input)

    @app.post("/with-request")
    async def with_request(
        request: Request,
        payload: EggSpoilageCreateRequest,
    ) -> dict[str, Any]:
        return {
            "all": request.all(),
            "has_ref": request.has("reference"),
            "ref": request.get("reference"),
            "async_input_eggs": await request.input("spoiled_or_broken_eggs"),
            "payload_eggs": payload.spoiled_or_broken_eggs,
        }

    async with TestClient(app) as client:
        res = await client.post(
            "/with-request?source=api",
            json={
                "movement_date": "2026-11-25",
                "spoiled_or_broken_eggs": 6,
                "reference": "REQ-1",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["has_ref"] is True
        assert body["ref"] == "REQ-1"
        assert body["all"]["source"] == "api"
        assert body["all"]["spoiled_or_broken_eggs"] == 6
        assert body["async_input_eggs"] == 6
        assert body["payload_eggs"] == 6
        assert preload_calls == 0
