from __future__ import annotations

from swiftpy.validation.form_request import FormRequest


class CreateUserRequest(FormRequest):
    """
    FormRequest validating inbound user registration payloads.
    Encapsulates field rules, sanitization, and custom error messages.
    """

    def rules(self) -> dict[str, str]:
        return {
            "name": "required|string|min:2|max:100",
            "email": "required|email|max:255",
            "password": "required|string|min:8",
        }

    def messages(self) -> dict[str, str]:
        return {
            "name.required": "Full name is required.",
            "email.email": "Please provide a valid email address.",
            "password.min": "Password must be at least 8 characters long.",
        }

