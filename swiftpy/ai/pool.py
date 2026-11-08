from __future__ import annotations

try:
    import httpx
except ImportError:
    httpx = None

from swiftpy.core.config import Config
from swiftpy.core.container import get_container


class AIHttpPool:
    """
    Shared HTTP client connection pool for outbound LLM API requests.

    Maintains a long-lived httpx.AsyncClient across requests to avoid
    re-establishing TCP connections and SSL/TLS handshakes on every call.
    """

    _instance: AIHttpPool | None = None

    def __init__(
        self,
        config: Config | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config is None:
            try:
                self._config = Config.get_instance()
            except RuntimeError:
                self._config = Config()
        else:
            self._config = config

        self._custom_client = client
        self._client: httpx.AsyncClient | None = client

    @property
    def max_connections(self) -> int:
        return int(self._config.get("ai.http.max_connections", 100))

    @property
    def max_keepalive_connections(self) -> int:
        return int(self._config.get("ai.http.max_keepalive_connections", 20))

    @property
    def keepalive_expiry(self) -> float:
        return float(self._config.get("ai.http.keepalive_expiry", 30.0))

    @property
    def timeout(self) -> float:
        return float(self._config.get("ai.http.timeout", 60.0))

    @classmethod
    def get_instance(cls) -> AIHttpPool:
        """
        Retrieve or create the shared AIHttpPool singleton instance.
        """
        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
        except RuntimeError:
            pass

        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, pool: AIHttpPool | None) -> None:
        """Set or clear the shared AIHttpPool singleton (primarily for testing)."""
        cls._instance = pool

    def _build_client(self) -> httpx.AsyncClient:
        if httpx is None:
            raise RuntimeError(
                "The 'httpx' package is required for outbound AI HTTP calls. "
                "Ensure dependencies are installed."
            )
        limits = httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive_connections,
            keepalive_expiry=self.keepalive_expiry,
        )
        return httpx.AsyncClient(
            limits=limits,
            timeout=httpx.Timeout(self.timeout),
        )

    def get_client(self) -> httpx.AsyncClient:
        """
        Retrieve the shared httpx.AsyncClient.
        If the previous client was closed or not yet initialized, creates a fresh client.
        """
        if self._client is None or self._client.is_closed:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        """Close the shared client connection pool and release resources safely."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
