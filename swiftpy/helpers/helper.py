from __future__ import annotations

import os
import re
from typing import Any

from swiftpy.core.bootstrap import get_container
from swiftpy.core.config import Config

_UNCOUNTABLES = {
    "aircraft",
    "deer",
    "equipment",
    "fish",
    "information",
    "money",
    "police",
    "rice",
    "series",
    "sheep",
    "species",
}

_IRREGULARS = {
    "child": "children",
    "foot": "feet",
    "goose": "geese",
    "man": "men",
    "mouse": "mice",
    "ox": "oxen",
    "person": "people",
    "quiz": "quizzes",
    "tooth": "teeth",
    "woman": "women",
}

_PLURAL_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(quiz)$", re.IGNORECASE), r"\1zes"),
    (re.compile(r"^(ox)$", re.IGNORECASE), r"\1en"),
    (re.compile(r"([ml])ouse$", re.IGNORECASE), r"\1ice"),
    (re.compile(r"(matr|vert|ind)(?:ix|ex)$", re.IGNORECASE), r"\1ices"),
    (re.compile(r"(ax|test)is$", re.IGNORECASE), r"\1es"),
    (re.compile(r"sis$", re.IGNORECASE), "ses"),
    (re.compile(r"(x|ch|ss|sh)$", re.IGNORECASE), r"\1es"),
    (re.compile(r"([^aeiouy]|qu)y$", re.IGNORECASE), r"\1ies"),
    (re.compile(r"(?:([^f])fe|([lr])f)$", re.IGNORECASE), r"\1\2ves"),
    (re.compile(r"([ti])um$", re.IGNORECASE), r"\1a"),
    (re.compile(r"(buffal|tomat)o$", re.IGNORECASE), r"\1oes"),
    (re.compile(r"(bu)s$", re.IGNORECASE), r"\1ses"),
    (re.compile(r"(alias|status)$", re.IGNORECASE), r"\1es"),
    (re.compile(r"(octop|vir)us$", re.IGNORECASE), r"\1uses"),
    (re.compile(r"s$", re.IGNORECASE), "s"),
    (re.compile(r"$"), "s"),
]


def app() -> Any:
    """Return the application container."""
    return get_container()


def config(key: str | None = None, default: Any = None) -> Any:
    """Retrieve configuration values."""
    cfg = get_container().resolve(Config)

    if key is None:
        return cfg

    return cfg.get(key, default)


def container() -> Any:
    """Return the IoC container."""
    return get_container()


def resolve(target: type[Any]) -> Any:
    """Resolve a dependency from the container."""
    return get_container().resolve(target)


def env(key: str, default: Any = None) -> Any:
    """Retrieve environment variables."""
    return os.getenv(key, default)


def snake_case(name: str) -> str:
    """Convert PascalCase or camelCase to snake_case."""
    value = re.sub(
        r"(?<!^)(?=[A-Z])",
        "_",
        name,
    )

    return value.lower()


def pluralize(word: str) -> str:
    """Pluralize an English word or snake_cased identifier, preserving capitalization."""
    if not word:
        return word

    parts = word.split("_")
    last = parts[-1]
    last_lower = last.lower()

    if last_lower in _UNCOUNTABLES:
        plural_last = last
    elif last_lower in _IRREGULARS:
        replacement = _IRREGULARS[last_lower]
        if last.isupper():
            plural_last = replacement.upper()
        elif last.istitle():
            plural_last = replacement.title()
        else:
            plural_last = replacement
    else:
        for pattern, replacement in _PLURAL_RULES:
            if pattern.search(last):
                plural_last = pattern.sub(replacement, last)
                break
        else:
            plural_last = last + "s"

        if last.isupper():
            plural_last = plural_last.upper()
        elif last.istitle():
            plural_last = plural_last[0].upper() + plural_last[1:]

    parts[-1] = plural_last
    return "_".join(parts)


__all__ = ["app", "config", "container", "env", "pluralize", "resolve", "snake_case"]
