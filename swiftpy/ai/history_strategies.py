from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable

from swiftpy.ai.conversation import Conversation
from swiftpy.ai.providers.base import Message


def _trim_orphaned_leading_turns(
    dialogue: list[Message],
    *,
    min_remaining: int = 0,
) -> list[Message]:
    """Remove leading orphaned assistant/tool messages so dialogue starts cleanly with user."""
    while len(dialogue) > min_remaining and dialogue[0].role in ("assistant", "tool"):
        dialogue.pop(0)
    return dialogue


def default_token_estimator(text: str) -> int:
    """
    Heuristic token estimator (~4 characters per token).
    Provides consistent, fast token estimation without external dependencies.
    """
    if not text:
        return 0
    return max(1, len(text) // 4 + 1)


class HistoryStrategy(ABC):
    """Abstract base class for conversation history pruning strategies."""

    @abstractmethod
    def apply(self, conversation: Conversation) -> Conversation:
        """Apply the pruning strategy to a conversation, returning a new trimmed Conversation."""


class SlidingWindowStrategy(HistoryStrategy):
    """
    Sliding window history strategy retaining the last N dialogue messages.

    INVARIANTS:
        1. All system prompt messages are strictly preserved at the start.
        2. Pruning maintains turn integrity: if pruning leaves an orphaned
           assistant response at the beginning of dialogue, it is removed so
           the retained dialogue cleanly begins with a user turn.
        3. The input conversation is never mutated.
    """

    def __init__(self, max_messages: int = 10) -> None:
        if max_messages <= 0:
            raise ValueError(f"max_messages must be a positive integer, got {max_messages}")
        self.max_messages = max_messages

    def apply(self, conversation: Conversation) -> Conversation:
        all_msgs = conversation.messages
        if len(all_msgs) <= self.max_messages:
            return conversation.clone()

        system_msgs = conversation.system_messages
        dialogue_msgs = conversation.dialogue_messages

        allowed_dialogue = max(0, self.max_messages - len(system_msgs))
        trimmed_dialogue = dialogue_msgs[-allowed_dialogue:] if allowed_dialogue > 0 else []

        # Turn integrity: do not start dialogue with orphaned assistant or tool messages
        trimmed_dialogue = _trim_orphaned_leading_turns(trimmed_dialogue, min_remaining=0)

        retained: list[Message] = []
        retained.extend(system_msgs)
        retained.extend(trimmed_dialogue)

        return Conversation(messages=retained)


class TokenBudgetStrategy(HistoryStrategy):
    """
    Token budget history strategy strictly enforcing a maximum token capacity.

    INVARIANTS:
        1. All system prompt messages are strictly preserved.
        2. If system prompt(s) alone exceed max_tokens, raises ValueError.
        3. If the newest message alone exceeds the available budget, raises ValueError.
        4. Dialogue turns are pruned from oldest to newest.
        5. Returned conversation total tokens NEVER exceed max_tokens.
        6. The input conversation is never mutated.
    """

    def __init__(
        self,
        max_tokens: int = 2000,
        estimator: Callable[[str], int] | None = None,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be a positive integer, got {max_tokens}")
        self.max_tokens = max_tokens
        self.estimator = estimator or default_token_estimator

    def estimate_message(self, message: Message) -> int:
        """Estimate token consumption of a single message including framing overhead."""
        base = self.estimator(message.content) + 4
        if message.tool_calls:
            base += self.estimator(json.dumps([tc.to_dict() for tc in message.tool_calls]))
        return base

    def estimate_conversation(self, conversation: Conversation) -> int:
        """Calculate total estimated token consumption of a conversation."""
        return sum(self.estimate_message(m) for m in conversation.messages)

    def apply(self, conversation: Conversation) -> Conversation:
        system_msgs = conversation.system_messages
        dialogue_msgs = conversation.dialogue_messages

        system_tokens = sum(self.estimate_message(m) for m in system_msgs)
        if system_tokens > self.max_tokens:
            raise ValueError(
                f"System prompt consumes {system_tokens} tokens, exceeding total budget of {self.max_tokens} tokens."
            )

        remaining_budget = self.max_tokens - system_tokens

        if not dialogue_msgs:
            return Conversation(messages=system_msgs)

        newest_msg = dialogue_msgs[-1]
        newest_tokens = self.estimate_message(newest_msg)
        if newest_tokens > remaining_budget:
            raise ValueError(
                f"Newest message ({newest_tokens} tokens) exceeds remaining budget of {remaining_budget} tokens."
            )

        # Select dialogue messages from most recent backward
        retained_dialogue_reversed: list[Message] = []
        accumulated_tokens = 0

        for msg in reversed(dialogue_msgs):
            msg_tokens = self.estimate_message(msg)
            if accumulated_tokens + msg_tokens <= remaining_budget:
                retained_dialogue_reversed.append(msg)
                accumulated_tokens += msg_tokens
            else:
                break

        retained_dialogue = list(reversed(retained_dialogue_reversed))

        # Turn integrity: ensure dialogue begins cleanly with user turn (drop orphaned assistant/tool)
        retained_dialogue = _trim_orphaned_leading_turns(retained_dialogue, min_remaining=1)

        retained: list[Message] = []
        retained.extend(system_msgs)
        retained.extend(retained_dialogue)

        result = Conversation(messages=retained)
        assert self.estimate_conversation(result) <= self.max_tokens, "Budget violated"
        return result
