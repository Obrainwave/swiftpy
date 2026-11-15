from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Sequence
from typing import Any, Self

from swiftpy.ai.providers.base import Message, ToolCall


def _clone_message(m: Message) -> Message:
    """Create a deep clone of a Message preserving tool_calls and tool_call_id."""
    cloned_tools: list[ToolCall] | None = None
    if m.tool_calls:
        cloned_tools = [
            ToolCall(id=tc.id, name=tc.name, arguments=copy.deepcopy(tc.arguments))
            for tc in m.tool_calls
        ]
    return Message(
        role=m.role,
        content=m.content,
        name=m.name,
        tool_calls=cloned_tools,
        tool_call_id=m.tool_call_id,
    )


def _deserialize_tool_call(raw_tc: Any) -> ToolCall | None:
    """Deserialize an OpenAI-formatted or flat tool call dict into ToolCall."""
    if not isinstance(raw_tc, dict):
        return None
    tc_id = str(raw_tc.get("id") or "")
    fn = raw_tc.get("function")
    if isinstance(fn, dict):
        name = str(fn.get("name") or "")
        raw_args = fn.get("arguments")
    else:
        name = str(raw_tc.get("name") or "")
        raw_args = raw_tc.get("arguments")

    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            args: dict[str, Any] = parsed if isinstance(parsed, dict) else {"raw": raw_args}
        except (json.JSONDecodeError, TypeError):
            args = {"raw": raw_args} if raw_args else {}
    elif isinstance(raw_args, dict):
        args = copy.deepcopy(raw_args)
    else:
        args = {}
    return ToolCall(id=tc_id, name=name, arguments=args)


class Conversation:
    """
    Multi-turn conversation history container.

    Manages ordered messages across dialogue turns with serialization
    support for persistence and integration with AIClient.
    """

    def __init__(self, messages: Sequence[Message] | None = None) -> None:
        self._messages: list[Message] = (
            [_clone_message(m) for m in messages] if messages else []
        )

    def add_system(self, content: str) -> Self:
        """Append a system prompt message."""
        self._messages.append(Message(role="system", content=content))
        return self

    def add_user(self, content: str) -> Self:
        """Append a user dialogue message."""
        self._messages.append(Message(role="user", content=content))
        return self

    def add_assistant(self, content: str) -> Self:
        """Append an assistant response message."""
        self._messages.append(Message(role="assistant", content=content))
        return self

    def add_message(self, message: Message) -> Self:
        """Append a pre-constructed Message instance."""
        if not isinstance(message, Message):
            raise TypeError(f"Expected Message instance, got {type(message).__name__}")
        self._messages.append(message)
        return self

    @property
    def messages(self) -> list[Message]:
        """Return a defensive copy of the ordered messages list."""
        return list(self._messages)

    @property
    def system_messages(self) -> list[Message]:
        """Return all system prompt messages in order."""
        return [m for m in self._messages if m.role == "system"]

    @property
    def dialogue_messages(self) -> list[Message]:
        """Return non-system dialogue messages (user and assistant) in order."""
        return [m for m in self._messages if m.role != "system"]

    def clear(self) -> None:
        """Remove all messages from the conversation."""
        self._messages.clear()

    def clone(self) -> Conversation:
        """Create an independent deep copy of this conversation."""
        return Conversation(messages=[_clone_message(m) for m in self._messages])

    def to_dict_list(self) -> list[dict[str, Any]]:
        """Serialize messages to a list of dictionaries."""
        return [m.to_dict() for m in self._messages]

    def to_json(self) -> str:
        """Serialize conversation to JSON string."""
        return json.dumps(self.to_dict_list())

    @classmethod
    def from_dict_list(cls, data: Sequence[dict[str, Any]]) -> Conversation:
        """Construct a Conversation instance from a sequence of message dictionaries."""
        messages: list[Message] = []
        for item in data:
            if not isinstance(item, dict):
                raise TypeError(f"Expected dictionary for message, got {type(item).__name__}")
            role = str(item.get("role", "user"))
            content = str(item.get("content") or "")
            name = item.get("name")
            tool_call_id = item.get("tool_call_id")
            raw_tool_calls = item.get("tool_calls")
            tool_calls: list[ToolCall] | None = None
            if isinstance(raw_tool_calls, list):
                parsed_tcs = [
                    tc for raw_tc in raw_tool_calls if (tc := _deserialize_tool_call(raw_tc)) is not None
                ]
                if parsed_tcs:
                    tool_calls = parsed_tcs
            messages.append(
                Message(
                    role=role,
                    content=content,
                    name=str(name) if name else None,
                    tool_calls=tool_calls,
                    tool_call_id=str(tool_call_id) if tool_call_id else None,
                )
            )
        return cls(messages=messages)

    @classmethod
    def from_json(cls, json_str: str) -> Conversation:
        """Deserialize a Conversation instance from a JSON string."""
        if not json_str or not json_str.strip():
            return cls()
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Invalid JSON string for conversation: {exc}") from exc

        if not isinstance(data, list):
            raise TypeError(f"Expected a JSON list of messages, got {type(data).__name__}")
        return cls.from_dict_list(data)

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[Message]:
        return iter(self._messages)

    def __getitem__(self, index: int) -> Message:
        return self._messages[index]
