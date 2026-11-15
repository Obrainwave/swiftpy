from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from swiftpy.mail.exceptions import MailValidationError


class _Jinja2Module(Protocol):
    FileSystemLoader: Any
    select_autoescape: Any


_jinja_sandbox_env_cls: Callable[..., Any] | None
jinja2: _Jinja2Module | None
try:
    import jinja2 as _jinja2
    from jinja2.sandbox import SandboxedEnvironment as _SandboxedEnvironment

    jinja2 = _jinja2
    _jinja_sandbox_env_cls = _SandboxedEnvironment
except ImportError:
    jinja2 = None
    _jinja_sandbox_env_cls = None

logger = logging.getLogger("swiftpy.mail.templates")


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """Represents the rendered output of an email template."""

    html: str | None = None
    text: str | None = None


_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_\.]+)\s*\}\}")


class MailTemplateRenderer:
    """
    Renders dual HTML and plain-text email templates with automatic escaping.
    Uses Jinja2 if available; otherwise falls back to a safe built-in formatter.
    Protects against path traversal, handles nested variables, and escapes quotes.
    """

    _instance: MailTemplateRenderer | None = None

    def __init__(self, template_dir: str | Path = "views") -> None:
        self.template_dir = Path(template_dir)
        self._jinja_env: Any = None
        if jinja2 is not None and _jinja_sandbox_env_cls is not None:
            self._jinja_env = _jinja_sandbox_env_cls(
                loader=jinja2.FileSystemLoader(str(self.template_dir)),
                autoescape=jinja2.select_autoescape(["html", "xml"]),
                enable_async=True,
            )

    @classmethod
    def get_instance(cls) -> MailTemplateRenderer:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _resolve_template_path(self, base_name: str, ext: str) -> Path:
        """Resolve template file path and prevent directory traversal attacks."""
        candidate = self.template_dir / f"{base_name}.{ext}"
        try:
            resolved_candidate = candidate.resolve()
            resolved_dir = self.template_dir.resolve()
            if not resolved_candidate.is_relative_to(resolved_dir):
                raise MailValidationError(
                    f"Template path '{base_name}' traverses outside template directory"
                )
            return resolved_candidate
        except (ValueError, RuntimeError) as exc:
            raise MailValidationError(
                f"Invalid template path '{base_name}': {exc}"
            ) from exc

    async def render(
        self,
        template_name: str,
        context: dict[str, Any],
    ) -> RenderedEmail:
        """Render both HTML and plain-text templates if present."""
        base_name = template_name
        if base_name.endswith((".html", ".txt")):
            base_name = base_name.rsplit(".", 1)[0]

        # Verify traversal safety
        html_path = self._resolve_template_path(base_name, "html")
        text_path = self._resolve_template_path(base_name, "txt")

        html_content: str | None = None
        text_content: str | None = None

        if self._jinja_env is not None:
            html_template_file = f"{base_name}.html"
            text_template_file = f"{base_name}.txt"

            if await asyncio.to_thread(html_path.is_file):
                template = await asyncio.to_thread(
                    self._jinja_env.get_template, html_template_file
                )
                html_content = await template.render_async(context)

            if await asyncio.to_thread(text_path.is_file):
                template = await asyncio.to_thread(
                    self._jinja_env.get_template, text_template_file
                )
                text_content = await template.render_async(context)
        else:
            if await asyncio.to_thread(html_path.is_file):
                raw = await asyncio.to_thread(html_path.read_text, encoding="utf-8")
                html_content = self._render_string(raw, context, escape_html=True)

            if await asyncio.to_thread(text_path.is_file):
                raw = await asyncio.to_thread(text_path.read_text, encoding="utf-8")
                text_content = self._render_string(raw, context, escape_html=False)

        return RenderedEmail(html=html_content, text=text_content)

    def render_string(
        self,
        template_str: str,
        context: dict[str, Any],
        escape_html: bool = True,
    ) -> str:
        """Render a raw template string with context values."""
        return self._render_string(template_str, context, escape_html=escape_html)

    def _render_string(
        self,
        template_str: str,
        context: dict[str, Any],
        escape_html: bool,
    ) -> str:
        def _resolve_nested(path: str) -> Any:
            parts = path.split(".")
            curr: Any = context
            for part in parts:
                if part.startswith("_"):
                    raise MailValidationError(
                        f"Access to private or dunder template attribute '{part}' is forbidden"
                    )
                if isinstance(curr, dict) and part in curr:
                    curr = curr[part]
                elif hasattr(curr, part):
                    curr = getattr(curr, part)
                else:
                    return None
            return curr

        def _replace(match: re.Match[str]) -> str:
            var_path = match.group(1)
            val = _resolve_nested(var_path)
            if val is not None:
                val_str = str(val)
                return html.escape(val_str, quote=True) if escape_html else val_str
            # Missing variable renders as empty string to avoid leaking placeholders
            return ""

        return _VAR_PATTERN.sub(_replace, template_str)


__all__ = ["MailTemplateRenderer", "RenderedEmail"]
