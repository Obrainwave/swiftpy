from __future__ import annotations

from collections.abc import Callable

try:
    import httpx
except ImportError:
    httpx = None

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from swiftpy.ai.client import AIClient
from swiftpy.ai.conversation import Conversation
from swiftpy.ai.exceptions import AIConnectionError
from swiftpy.ai.history_strategies import SlidingWindowStrategy, TokenBudgetStrategy
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.redis_store import (
    RedisConversationStore,
)
from swiftpy.core.config import Config

# ---------------------------------------------------------------------------
# Conversation Data Structure & Serialization Tests
# ---------------------------------------------------------------------------


def test_conversation_append_and_filtering() -> None:
    conv = Conversation()
    conv.add_system("Sys 1")
    conv.add_system("Sys 2")
    conv.add_user("User 1")
    conv.add_assistant("Assistant 1")
    conv.add_user("User 2")

    assert len(conv) == 5
    assert len(conv.system_messages) == 2
    assert conv.system_messages[0].content == "Sys 1"
    assert conv.system_messages[1].content == "Sys 2"

    assert len(conv.dialogue_messages) == 3
    assert conv.dialogue_messages[0].content == "User 1"
    assert conv.dialogue_messages[1].content == "Assistant 1"
    assert conv.dialogue_messages[2].content == "User 2"


def test_conversation_empty_serialization_roundtrip() -> None:
    conv = Conversation()
    json_str = conv.to_json()
    assert json_str == "[]"

    restored = Conversation.from_json(json_str)
    assert len(restored) == 0


def test_conversation_large_history_roundtrip() -> None:
    conv = Conversation()
    conv.add_system("System instructions")
    for i in range(50):
        conv.add_user(f"Question {i}")
        conv.add_assistant(f"Answer {i}")

    assert len(conv) == 101
    restored = Conversation.from_json(conv.to_json())
    assert len(restored) == 101
    assert restored[-1].content == "Answer 49"


def test_conversation_clone_is_deeply_isolated() -> None:
    conv1 = Conversation()
    conv1.add_system("Sys")
    conv1.add_user("User original")

    conv2 = conv1.clone()
    conv2.add_assistant("Assistant in clone")

    assert len(conv1) == 2
    assert len(conv2) == 3
    assert conv1[-1].role == "user"
    assert conv2[-1].role == "assistant"


def test_conversation_invalid_json_handling() -> None:
    with pytest.raises(ValueError, match="Invalid JSON string"):
        Conversation.from_json("{this is not json}")

    with pytest.raises(TypeError, match="Expected a JSON list"):
        Conversation.from_json('{"key": "not a list"}')


# ---------------------------------------------------------------------------
# Conversation Lifecycle Integration with AIClient
# ---------------------------------------------------------------------------


async def test_send_conversation_saves_both_user_and_assistant(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "Assistant reply"}}]},
        )

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    conv = Conversation()
    conv.add_system("You are helpful.")

    ai_client = (
        AIClient(config=Config(), http_pool=pool, default_provider="openai")
        .conversation(conv)
        .user("New question from user")
    )

    resp = await ai_client.send()
    assert resp.content == "Assistant reply"

    # Verify conversation now contains: System prompt, User message, and Assistant response
    assert len(conv) == 3
    assert conv[0].role == "system"
    assert conv[1].role == "user"
    assert conv[1].content == "New question from user"
    assert conv[2].role == "assistant"
    assert conv[2].content == "Assistant reply"


async def test_streaming_conversation_saves_assistant_response_after_completion(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"Chunk1 "}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"Chunk2"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body, headers={"Content-Type": "text/event-stream"})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    conv = Conversation()
    conv.add_system("You are helpful.")

    ai_client = (
        AIClient(config=Config(), http_pool=pool, default_provider="openai")
        .conversation(conv)
        .user("Stream question")
    )

    # Stream to completion
    chunks: list[str] = []
    async for chunk in ai_client.stream():
        chunks.append(chunk)

    assert "".join(chunks) == "Chunk1 Chunk2"

    # Full assistant response was saved upon successful stream completion
    assert len(conv) == 3
    assert conv[1].role == "user"
    assert conv[1].content == "Stream question"
    assert conv[2].role == "assistant"
    assert conv[2].content == "Chunk1 Chunk2"


async def test_streaming_conversation_does_not_save_partial_response(
    mock_client_factory: Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient],
) -> None:
    """
    CRITICAL: Verify that if a stream is cancelled or broken midway,
    partial assistant output is NOT saved to the conversation.
    """
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"Partial"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":" NEVER RECEIVED"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body, headers={"Content-Type": "text/event-stream"})

    client = mock_client_factory(handler)
    pool = AIHttpPool(client=client)

    conv = Conversation()
    conv.add_system("You are helpful.")

    ai_client = (
        AIClient(config=Config(), http_pool=pool, default_provider="openai")
        .conversation(conv)
        .user("Aborted stream question")
    )

    gen = ai_client.stream()
    first_chunk = await anext(gen)
    assert first_chunk == "Partial"

    # Cancel consumer midway before completion
    if hasattr(gen, "aclose"):
        await gen.aclose()

    # User message was recorded, but partial assistant message must NOT be in conv
    assert len(conv) == 2
    assert conv[0].role == "system"
    assert conv[1].role == "user"
    assert conv[1].content == "Aborted stream question"


# ---------------------------------------------------------------------------
# Sliding Window Turn Integrity Tests
# ---------------------------------------------------------------------------


def test_sliding_window_preserves_complete_turns() -> None:
    """
    CRITICAL: Verify that the sliding window strategy trims dialogue turns cleanly
    and avoids starting with an orphaned assistant message.
    """
    conv = Conversation()
    conv.add_system("Sys prompt.")
    conv.add_user("User 1")
    conv.add_assistant("Assistant 1")
    conv.add_user("User 2")
    conv.add_assistant("Assistant 2")
    conv.add_user("User 3")
    conv.add_assistant("Assistant 3")

    # Total: 1 system + 6 dialogue = 7 messages
    # If max_messages = 4, raw slice would be: [Sys, Assistant 2, User 3, Assistant 3]
    # Pruning Assistant 2 leaves complete turns: [Sys, User 3, Assistant 3]
    strategy = SlidingWindowStrategy(max_messages=4)
    trimmed = strategy.apply(conv)

    assert len(trimmed) == 3
    assert trimmed[0].role == "system"
    assert trimmed[1].role == "user"
    assert trimmed[1].content == "User 3"
    assert trimmed[2].role == "assistant"
    assert trimmed[2].content == "Assistant 3"


def test_sliding_window_original_conversation_not_mutated() -> None:
    conv = Conversation()
    conv.add_system("Sys")
    conv.add_user("U1").add_assistant("A1").add_user("U2").add_assistant("A2")
    original_len = len(conv)

    strategy = SlidingWindowStrategy(max_messages=2)
    _ = strategy.apply(conv)

    assert len(conv) == original_len


# ---------------------------------------------------------------------------
# Strict Token Budget Strategy Tests
# ---------------------------------------------------------------------------


def test_token_budget_never_returns_over_budget_history() -> None:
    conv = Conversation()
    conv.add_system("System instructions.")
    for i in range(10):
        conv.add_user(f"This is a longer user prompt number {i}")
        conv.add_assistant(f"This is a longer assistant answer number {i}")

    strategy = TokenBudgetStrategy(max_tokens=60)
    trimmed = strategy.apply(conv)

    estimated = strategy.estimate_conversation(trimmed)
    assert estimated <= 60
    assert trimmed[0].role == "system"
    assert trimmed[1].role == "user"


def test_token_budget_raises_when_system_prompt_exceeds_budget() -> None:
    conv = Conversation()
    conv.add_system("This is an extremely long system prompt that will definitely exceed thirty tokens.")
    conv.add_user("Hi")

    strategy = TokenBudgetStrategy(max_tokens=20)
    with pytest.raises(ValueError, match="System prompt consumes .* exceeding total budget"):
        strategy.apply(conv)


def test_token_budget_raises_when_newest_message_exceeds_budget() -> None:
    conv = Conversation()
    conv.add_system("Short sys.")
    conv.add_user("This single user message is excessively large and cannot fit into the available token budget at all.")

    strategy = TokenBudgetStrategy(max_tokens=25)
    with pytest.raises(ValueError, match="Newest message .* exceeds remaining budget"):
        strategy.apply(conv)


def test_token_budget_handles_exact_boundary() -> None:
    conv = Conversation()
    conv.add_system("Sys")  # 3 chars -> 1 + 4 = 5 tokens
    conv.add_user("Hi")     # 2 chars -> 1 + 4 = 5 tokens
    # Total = 10 tokens

    strategy = TokenBudgetStrategy(max_tokens=10)
    trimmed = strategy.apply(conv)
    assert len(trimmed) == 2
    assert strategy.estimate_conversation(trimmed) == 10


def test_token_budget_one_above_boundary_drops_oldest_turn() -> None:
    conv = Conversation()
    conv.add_system("Sys")
    conv.add_user("Question 1")
    conv.add_assistant("Answer 1")
    conv.add_user("Question 2")

    strategy = TokenBudgetStrategy(max_tokens=25)
    trimmed = strategy.apply(conv)

    assert strategy.estimate_conversation(trimmed) <= 25
    assert trimmed[0].role == "system"
    assert trimmed[-1].content == "Question 2"


# ---------------------------------------------------------------------------
# Redis Persistence Tests (TTL, Integrity, Error Handling)
# ---------------------------------------------------------------------------


class SimulatedRedisClient:
    """In-memory Redis fake with TTL tracking and error injection."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.fail_ops: set[str] = set()

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        if "set" in self.fail_ops:
            raise RedisConnectionError("Redis set failed")
        self.data[key] = value
        self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        if "get" in self.fail_ops:
            raise RedisConnectionError("Redis get failed")
        return self.data.get(key)

    async def delete(self, key: str) -> int:
        if "delete" in self.fail_ops:
            raise RedisConnectionError("Redis delete failed")
        if key in self.data:
            del self.data[key]
            self.ttls.pop(key, None)
            return 1
        return 0

    async def exists(self, key: str) -> int:
        if "exists" in self.fail_ops:
            raise RedisConnectionError("Redis exists failed")
        return 1 if key in self.data else 0

    async def aclose(self) -> None:
        pass


async def test_redis_store_default_and_custom_ttl() -> None:
    fake = SimulatedRedisClient()
    store = RedisConversationStore(redis_client=fake, default_ttl=3600)  # type: ignore[arg-type]

    conv = Conversation().add_system("Sys").add_user("U1")

    # 1. Default TTL
    await store.save("session_default", conv)
    assert fake.ttls["ai:conversation:session_default"] == 3600

    # 2. Explicit TTL override
    await store.save("session_custom", conv, ttl=7200)
    assert fake.ttls["ai:conversation:session_custom"] == 7200

    # 3. No expiration (ttl=0)
    await store.save("session_permanent", conv, ttl=0)
    assert fake.ttls["ai:conversation:session_permanent"] is None


async def test_redis_store_handles_corrupted_json() -> None:
    fake = SimulatedRedisClient()
    store = RedisConversationStore(redis_client=fake)  # type: ignore[arg-type]

    fake.data["ai:conversation:corrupt_session"] = "{bad json corrupted"

    with pytest.raises(ValueError, match="Corrupted conversation data in Redis"):
        await store.load("corrupt_session")


async def test_redis_store_translates_exists_and_delete_errors() -> None:
    fake = SimulatedRedisClient()
    store = RedisConversationStore(redis_client=fake)  # type: ignore[arg-type]

    fake.fail_ops.add("exists")
    with pytest.raises(AIConnectionError, match="Failed to check conversation existence"):
        await store.exists("session_fail")

    fake.fail_ops.clear()
    fake.fail_ops.add("delete")
    with pytest.raises(AIConnectionError, match="Failed to delete conversation"):
        await store.delete("session_fail")


async def test_redis_store_session_isolation_and_replacement() -> None:
    fake = SimulatedRedisClient()
    store = RedisConversationStore(redis_client=fake)  # type: ignore[arg-type]

    conv1 = Conversation().add_user("User A")
    conv2 = Conversation().add_user("User B")

    await store.save("user_1", conv1)
    await store.save("user_2", conv2)

    loaded1 = await store.load("user_1")
    loaded2 = await store.load("user_2")
    assert loaded1[0].content == "User A"
    assert loaded2[0].content == "User B"

    # Replacement test
    conv1_updated = Conversation().add_user("User A Updated")
    await store.save("user_1", conv1_updated)
    loaded1_new = await store.load("user_1")
    assert len(loaded1_new) == 1
    assert loaded1_new[0].content == "User A Updated"
