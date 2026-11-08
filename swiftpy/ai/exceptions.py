from __future__ import annotations


class AIError(Exception):
    """Base exception for all SwiftPY AI module errors."""


class AIModuleDisabledError(AIError):
    """Raised when an AI operation is attempted but the AI module is disabled in configuration."""


class AIConfigurationError(AIError):
    """Raised when AI configuration is missing, incomplete, or invalid."""


class AIProviderError(AIError):
    """Raised when an LLM provider encounters an error during request execution."""

    def __init__(
        self,
        message: str,
        provider: str = "",
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.response_body = response_body


class AIAuthenticationError(AIProviderError):
    """Raised when provider authentication fails (e.g. invalid or missing API key)."""


class AIRateLimitError(AIProviderError):
    """Raised when a provider rejects a request due to rate or quota limits."""

    def __init__(
        self,
        message: str,
        provider: str = "",
        status_code: int | None = 429,
        response_body: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message=message,
            provider=provider,
            status_code=status_code,
            response_body=response_body,
        )
        self.retry_after = retry_after


class AIConnectionError(AIProviderError):
    """Raised when an HTTP connection or network timeout error occurs while contacting a provider."""

