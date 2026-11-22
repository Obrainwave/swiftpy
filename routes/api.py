from __future__ import annotations

from app.http.controllers.user_controller import UserController
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import Router

router = Router()


@router.get("/health")
async def health_check(request: Request) -> Response:
    """Health check endpoint for container orchestrators and monitoring."""
    return json_response({"status": "ok", "app": "SwiftPY"})


router.get("/users", UserController.index)
