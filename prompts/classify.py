"""Named classification prompt template loaded via Prompt.load("classify", ...)."""

from __future__ import annotations

messages: list[dict[str, str]] = [
    {
        "role": "system",
        "content": (
            "Classify the user input into exactly one of the allowed categories: "
            "{{ categories }}. Return only the chosen category label."
        ),
    },
    {
        "role": "user",
        "content": "{{ text }}",
    },
]
