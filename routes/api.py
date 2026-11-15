from __future__ import annotations

from app.schemas.user_schema import UserResponseSchema
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import Router

router = Router()


@router.get("/health")
async def health_check(request: Request) -> Response:
    """Health check endpoint for container orchestrators and monitoring."""
    return json_response({"status": "ok", "app": "SwiftPY"})


@router.get("/users")
async def list_users(request: Request) -> Response:
    """Example user listing endpoint returning typed schema response."""
    return json_response(
        [
            UserResponseSchema(
                id=1, name="Alice Doe", email="alice@example.com"
            ).to_dict()
        ]
    )
