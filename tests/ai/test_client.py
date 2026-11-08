from __future__ import annotations

from collections.abc import AsyncIterator, Callable

try:
    import httpx
except ImportError:
    httpx = None

import pytest

from swiftpy.ai.client import AIClient, ai
from swiftpy.ai.exceptions import (
    AIConfigurationError,
    AIModuleDisabledError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.providers.base import (
    LLMRequest,
    LLMResponse,
    Message,
    ProviderInterface,
    TokenUsage,
)
from swiftpy.core.config import Config


class DummyTestProvider(ProviderInterface):
    @property
    def name(self) -> str:
        return "dummy"

    def get_default_model(self) -> str:
        return "dummy-model-v1"

    async def send(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=f"Echo: {request.messages[-1].content if request.messages else ''}",
            role="assistant",
            model=request.model or self.get_default_model(),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=12.5,
            finish_reason="stop",
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        words = ["Echo:", request.messages[-1].content if request.messages else ""]
        for w in words:
            yield w


@pytest.fixture
def mock_config() -> Config:
    return Config(
        {
            "ai": {
                "enabled": True,
                "default": "openai",
                "providers": {
                    "openai": {
                        "api_key": "sk-test-openai",
                        "model": "gpt-4o",
                        "base_url": "https://api.openai.com/v1",
                    },
                    "anthropic": {
                        "api_key": "sk-ant-test",
                        "model": "claude-3-5-sonnet-20241022",
                        "base_url": "https://api.anthropic.com/v1",
                    },
                    "dummy": {
                        "api_key": "dummy-key",
                    },
                },
                "http": {
                    "max_connections": 50,
                    "max_keepalive_connections": 10,
                    "keepalive_expiry": 15.0,
                    "timeout": 30.0,
                },
            }
        }
    )


# ---------------------------------------------------------------------------
# Fluent Builder Tests
# ---------------------------------------------------------------------------


async def test_fluent_builder_chaining(mock_config: Config) -> None:
    client = (
        AIClient(config=mock_config)
        .system("System instructions")
        .user("User question")
        .assistant("Assistant answer")
        .temperature(0.5)
        .max_tokens(256)
        .top_p(0.9)
        .stop(["\n", "END"])
        .model("custom-model")
        .extra(custom_param="value")
    )

    assert len(client._messages) == 3
    assert client._messages[0] == Message(role="system", content="System instructions")
    assert client._messages[1] == Message(role="user", content="User question")
    assert client._messages[2] == Message(role="assistant", content="Assistant answer")
    assert client._temperature == 0.5
    assert client._max_tokens == 256
    assert client._top_p == 0.9
    assert client._stop == ["\n", "END"]
    assert client._model == "custom-model"
    assert client._extra == {"custom_param": "value"}


# ---------------------------------------------------------------------------
# Builder Parameter Validation Tests
# ---------------------------------------------------------------------------


def test_temperature_validation(mock_config: Config) -> None:
    client = AIClient(config=mock_config)
    client.temperature(0.0)
    client.temperature(2.0)
    client.temperature(1.5)

    with pytest.raises(ValueError, match="Temperature must be between 0.0 and 2.0"):
        client.temperature(-0.1)

    with pytest.raises(ValueError, match="Temperature must be between 0.0 and 2.0"):
        client.temperature(2.1)


def test_max_tokens_validation(mock_config: Config) -> None:
    client = AIClient(config=mock_config)
    client.max_tokens(1)
    client.max_tokens(4096)

    with pytest.raises(ValueError, match="max_tokens must be a positive integer"):
        client.max_tokens(0)

    with pytest.raises(ValueError, match="max_tokens must be a positive integer"):
        client.max_tokens(-100)


def test_top_p_validation(mock_config: Config) -> None:
    client = AIClient(config=mock_config)
    client.top_p(0.0)
    client.top_p(1.0)
    client.top_p(0.5)

    with pytest.raises(ValueError, match="top_p must be between 0.0 and 1.0"):
        client.top_p(-0.01)

    with pytest.raises(ValueError, match="top_p must be between 0.0 and 1.0"):
        client.top_p(1.01)


def test_empty_message_content_rejected(mock_config: Config) -> None:
    client = AIClient(config=mock_config)
    with pytest.raises(ValueError, match="Message content cannot be empty"):
        client.user("")

    with pytest.raises(ValueError, match="Message content cannot be empty"):
        client.system("   ")

    with pytest.raises(ValueError, match="Message content cannot be empty"):
        client.assistant("\n\t")


def test_invalid_message_role_rejected(mock_config: Config) -> None:
    client = AIClient(config=mock_config)
    with pytest.raises(ValueError, match="Invalid message role 'developer'"):
        client.history([{"role": "developer", "content": "You are a bot"}])

    with pytest.raises(ValueError, match="Invalid message role 'tool'"):
        Message(role="tool", content="tool output")


def test_stop_sequence_validation(mock_config: Config) -> None:
    client = AIClient(config=mock_config)
    client.stop("STOP")
    assert client._stop == ["STOP"]

    client.stop(["STOP1", "STOP2"])
    assert client._stop == ["STOP1", "STOP2"]

    with pytest.raises(ValueError, match="Stop sequence string cannot be empty"):
        client.stop("")

    with pytest.raises(ValueError, match="Stop sequence list cannot be empty"):
        client.stop([])

    with pytest.raises(ValueError, match="All stop sequence items must be non-empty strings"):
        client.stop(["VALID", ""])


def test_history_appends_and_preserves_order(mock_config: Config) -> None:
    client = AIClient(config=mock_config)
    client.system("Initial system prompt")
    client.user("First question")

    # Calling .history() must append rather than replace existing messages
    client.history(
        [
            Message(role="assistant", content="First answer"),
            {"role": "user", "content": "Follow-up question"},
        ]
    )

    assert len(client._messages) == 4
    assert client._messages[0].role == "system"
    assert client._messages[0].content == "Initial system prompt"
    assert client._messages[1].role == "user"
    assert client._messages[1].content == "First question"
    assert client._messages[2].role == "assistant"
    assert client._messages[2].content == "First answer"
    assert client._messages[3].role == "user"
    assert client._messages[3].content == "Follow-up question"


def test_empty_history_sequence_is_noop(mock_config: Config) -> None:
    client = AIClient(config=mock_config).user("Hello")
    client.history([])
    assert len(client._messages) == 1
    assert client._messages[0].content == "Hello"


def test_invalid_history_type_rejected(mock_config: Config) -> None:
    client = AIClient(config=mock_config)
    with pytest.raises(TypeError, match="Expected Message or dict in history"):
        client.history(["not a valid message"])  # type: ignore[list-item]


async def test_send_without_messages_rejected(mock_config: Config) -> None:
    client = AIClient(config=mock_config)
    with pytest.raises(ValueError, match="Cannot send request: at least one message is required"):
        await client.send()


# ---------------------------------------------------------------------------
# Configuration & Provider Resolution Tests
# ---------------------------------------------------------------------------


async def test_ai_module_disabled_raises_error() -> None:
    disabled_config = Config({"ai": {"enabled": False, "default": "openai"}})
    client = AIClient(config=disabled_config).user("Hello")

    with pytest.raises(AIModuleDisabledError, match="AI module is disabled"):
        await client.send()


async def test_unknown_provider_raises_configuration_error(mock_config: Config) -> None:
    client = AIClient(config=mock_config).provider("non_existent_provider").user("Hello")

    with pytest.raises(AIConfigurationError, match="Unknown LLM provider"):
        await client.send()


async def test_missing_api_key_raises_configuration_error() -> None:
    cfg = Config(
        {
            "ai": {
                "enabled": True,
                "default": "openai",
                "providers": {
                    "openai": {"api_key": ""},
                },
            }
        }
    )
    client = AIClient(config=cfg).user("Hello")

    with pytest.raises(AIConfigurationError, match="Missing API key for provider 'openai'"):
        await client.send()


async def test_custom_provider_registration(mock_config: Config) -> None:
    AIClient.register_provider("dummy", DummyTestProvider)

    response = await (
        AIClient(config=mock_config)
        .provider("dummy")
        .user("Test registration")
        .send()
    )

    assert response.content == "Echo: Test registration"
    assert response.model == "dummy-model-v1"
    assert response.usage.total_tokens == 15


async def test_provider_swap_via_config_only(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    """
    Verify swapping providers via config requires ZERO application code changes.
    """
    openai_calls: list[httpx.Request] = []
    anthropic_calls: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "openai.com" in str(request.url):
            openai_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Response from OpenAI"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23},
                },
            )
        elif "anthropic.com" in str(request.url):
            anthropic_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "msg-test",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-5-sonnet-20241022",
                    "content": [{"type": "text", "text": "Response from Anthropic"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 14, "output_tokens": 9},
                },
            )
        return httpx.Response(404)

    mock_http_client = mock_client_factory(mock_handler)
    pool = AIHttpPool(client=mock_http_client)

    # 1. Configured with OpenAI as default
    cfg_openai = Config(
        {
            "ai": {
                "enabled": True,
                "default": "openai",
                "providers": {
                    "openai": {"api_key": "sk-openai", "base_url": "https://api.openai.com/v1"},
                    "anthropic": {"api_key": "sk-anthropic", "base_url": "https://api.anthropic.com/v1"},
                },
            }
        }
    )

    # Application code:
    resp1 = await (
        ai(config=cfg_openai, http_pool=pool)
        .system("Be concise.")
        .user("What is the speed of light?")
        .send()
    )
    assert resp1.content == "Response from OpenAI"
    assert resp1.model == "gpt-4o"
    assert len(openai_calls) == 1
    assert len(anthropic_calls) == 0

    # 2. Configured with Anthropic as default (Zero application code changes)
    cfg_anthropic = Config(
        {
            "ai": {
                "enabled": True,
                "default": "anthropic",
                "providers": {
                    "openai": {"api_key": "sk-openai", "base_url": "https://api.openai.com/v1"},
                    "anthropic": {"api_key": "sk-anthropic", "base_url": "https://api.anthropic.com/v1"},
                },
            }
        }
    )

    # Identical application code:
    resp2 = await (
        ai(config=cfg_anthropic, http_pool=pool)
        .system("Be concise.")
        .user("What is the speed of light?")
        .send()
    )
    assert resp2.content == "Response from Anthropic"
    assert resp2.model == "claude-3-5-sonnet-20241022"
    assert len(openai_calls) == 1
    assert len(anthropic_calls) == 1
