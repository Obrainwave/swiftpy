from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self

from swiftpy.core.application import Application
from swiftpy.testing.http_client import TestClient, TestResponse


class TestCase:
    """
    Base test case for SwiftPY applications.
    Provides an in-process TestClient, lifecycle helpers, and HTTP convenience methods.
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

    async def startup(self) -> None:
        """Start the underlying ASGI lifespan on the test client."""
        await self.client.startup()

    async def shutdown(self) -> None:
        """Shut down the underlying ASGI lifespan on the test client if created."""
        if self._client is not None:
            await self._client.shutdown()

    def reset(self) -> None:
        """Reset cached application and TestClient state between test runs."""
        self._client = None
        self.app = None

    async def __aenter__(self) -> Self:
        await self.startup()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.shutdown()

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
        data: bytes | str | Mapping[str, Any] | None = None,
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
        data: bytes | str | Mapping[str, Any] | None = None,
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
        data: bytes | str | Mapping[str, Any] | None = None,
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
        json: Any = None,
        data: bytes | str | Mapping[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.client.delete(
            path, json=json, data=data, headers=headers, query=query
        )


__all__ = ["TestCase"]
