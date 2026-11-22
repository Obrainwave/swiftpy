"""Named prompt template loaded via Prompt.load("summarise", ...)."""

from __future__ import annotations

messages: list[dict[str, str]] = [
    {
        "role": "system",
        "content": (
            "You are a precise technical editor. Summarise the provided content "
            "clearly in {{ style }} style."
        ),
    },
    {
        "role": "user",
        "content": "{{ text }}",
    },
]
