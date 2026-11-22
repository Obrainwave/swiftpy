from __future__ import annotations

from app.http.resources.user_resource import UserResource
from app.services.user_service import UserService
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response


class UserController:
    """
    HTTP controller for user API endpoints.

    Resolves constructor dependencies via the SwiftPY DI container and delegates
    domain logic to UserService before formatting responses with UserResource.
    """

    def __init__(self, user_service: UserService) -> None:
        self.user_service = user_service

    async def index(self, request: Request) -> Response:
        """List users serialized through UserResource."""
        users = await self.user_service.list_users()
        return json_response(UserResource.collection(users))
