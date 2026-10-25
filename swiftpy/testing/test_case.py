from __future__ import annotations

from typing import Any

from swiftpy.core.application import Application
from swiftpy.testing.http_client import TestClient, TestResponse


class TestCase:
    """
    Base test case for SwiftPY applications.
    Provides an in-process TestClient and convenient HTTP assertion methods.
    """

    __test__ = False
    app: Application | None = None
    _client: TestClient | None = None

    def create_app(self) -> Application:
        """Create and return an Application instance for testing."""
        return Application()

    def get_app(self) -> Application:
        """Retrieve the test application instance."""
        if self.app is None:
            self.app = self.create_app()
        return self.app

    @property
    def client(self) -> TestClient:
        """Retrieve or create the in-process TestClient."""
        if self._client is None:
            self._client = TestClient(self.get_app())
        return self._client

    async def get(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.client.get(path, headers=headers, query=query)

    async def post(
        self,
        path: str,
        *,
        json: Any = None,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.client.post(
            path, json=json, data=data, headers=headers, query=query
        )

    async def put(
        self,
        path: str,
        *,
        json: Any = None,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.client.put(
            path, json=json, data=data, headers=headers, query=query
        )

    async def patch(
        self,
        path: str,
        *,
        json: Any = None,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.client.patch(
            path, json=json, data=data, headers=headers, query=query
        )

    async def delete(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.client.delete(path, headers=headers, query=query)


__all__ = ["TestCase"]
