from __future__ import annotations

import re

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


def snake_case(name: str) -> str:
    """
    Convert PascalCase, camelCase, or mixed acronyms to snake_case.

    Examples:
        snake_case("UserProfile") -> "user_profile"
        snake_case("APIKey") -> "api_key"
        snake_case("HTTPServer") -> "http_server"
        snake_case("HTTPRequest") -> "http_request"
        snake_case("UserAPIKey") -> "user_api_key"
        snake_case("Oauth2Client") -> "oauth2_client"
    """
    if not name:
        return name

    # 1. Separate consecutive uppercase runs followed by titlecase/lowercase
    # e.g., 'APIKey' -> 'API_Key', 'HTTPRequest' -> 'HTTP_Request'
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)

    # 2. Separate lowercase/digit followed by uppercase
    # e.g., 'UserAPI_Key' -> 'User_API_Key', 'oauth2Client' -> 'oauth2_Client'
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)

    return s2.lower()


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


__all__ = ["pluralize", "snake_case"]
