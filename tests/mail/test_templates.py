from __future__ import annotations

from pathlib import Path

import pytest

from swiftpy.mail.exceptions import MailValidationError
from swiftpy.mail.templates.renderer import MailTemplateRenderer


@pytest.mark.asyncio
async def test_mail_template_renderer_string() -> None:
    renderer = MailTemplateRenderer()
    raw = "Hello {{ name }}, welcome to {{ app }}! Special: {{ special }}"
    context = {
        "name": "Bob",
        "app": "SwiftPY",
        "special": "<script>alert(1)</script>",
    }

    rendered_html = renderer.render_string(raw, context, escape_html=True)
    assert "Hello Bob, welcome to SwiftPY!" in rendered_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered_html

    rendered_text = renderer.render_string(raw, context, escape_html=False)
    assert "<script>alert(1)</script>" in rendered_text


@pytest.mark.asyncio
async def test_mail_template_renderer_files(tmp_path: Path) -> None:
    template_dir = tmp_path / "emails"
    template_dir.mkdir()

    html_file = template_dir / "welcome.html"
    html_file.write_text("<h1>Welcome, {{ user }}!</h1>", encoding="utf-8")

    text_file = template_dir / "welcome.txt"
    text_file.write_text("Welcome, {{ user }}!", encoding="utf-8")

    renderer = MailTemplateRenderer(template_dir=template_dir)
    rendered = await renderer.render("welcome", {"user": "Alice"})

    assert rendered.html == "<h1>Welcome, Alice!</h1>"
    assert rendered.text == "Welcome, Alice!"


@pytest.mark.asyncio
async def test_mail_template_renderer_nested_and_missing_variables() -> None:
    renderer = MailTemplateRenderer()
    raw = "User: {{ user.profile.name }}, Role: {{ user.role }}, Missing: {{ unknown.field }}"
    context = {
        "user": {
            "profile": {"name": "Charlie"},
            "role": "Admin",
        }
    }

    rendered = renderer.render_string(raw, context, escape_html=True)
    assert "User: Charlie" in rendered
    assert "Role: Admin" in rendered
    assert "Missing: " in rendered


@pytest.mark.asyncio
async def test_mail_template_renderer_quote_escaping() -> None:
    renderer = MailTemplateRenderer()
    raw = "<input value=\"{{ val }}\" data-user='{{ user_data }}'>"
    context = {
        "val": 'malicious"payload',
        "user_data": "user's data",
    }

    rendered = renderer.render_string(raw, context, escape_html=True)
    assert "malicious&quot;payload" in rendered
    assert "user&#x27;s data" in rendered


@pytest.mark.asyncio
async def test_mail_template_renderer_path_traversal(tmp_path: Path) -> None:
    template_dir = tmp_path / "views"
    template_dir.mkdir()

    renderer = MailTemplateRenderer(template_dir=template_dir)

    with pytest.raises(
        MailValidationError, match="traverses outside template directory"
    ):
        await renderer.render("../../../etc/passwd", {})

