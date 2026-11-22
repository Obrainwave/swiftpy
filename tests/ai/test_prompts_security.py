from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from swiftpy.ai.prompts.loader import PromptLoader


def test_path_traversal_attempts_rejected_with_value_error() -> None:
    """Verify that path traversal attempts with ../ or absolute paths raise ValueError."""
    loader = PromptLoader(base_dir="prompts")

    traversal_names = [
        "../secret",
        "../../etc/passwd",
        "nested/../../secret",
        "/etc/shadow",
        "../prompts/../../root",
    ]

    for bad_name in traversal_names:
        with pytest.raises(ValueError, match="Path traversal detected"):
            loader.get_template(bad_name)


def test_empty_prompt_name_raises_value_error() -> None:
    """Verify empty or whitespace-only prompt names raise ValueError."""
    loader = PromptLoader(base_dir="prompts")
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="Prompt name cannot be empty"):
            loader.get_template(bad)


def test_md_file_takes_precedence_over_py_file() -> None:
    """Verify that when both {name}.md and {name}.py exist, {name}.md takes precedence."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Create both file formats
        md_file = tmp_path / "greeting.md"
        md_file.write_text("Hello from Markdown: {{ name }}", encoding="utf-8")

        py_file = tmp_path / "greeting.py"
        py_file.write_text('PROMPT = "Hello from Python: {{ name }}"', encoding="utf-8")

        loader = PromptLoader(base_dir=tmp_path, cache=False)
        rendered = loader.load("greeting", name="World")
        assert rendered == "Hello from Markdown: World"


def test_py_file_loaded_when_md_not_present() -> None:
    """Verify that when only {name}.py exists, it is loaded correctly."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        py_file = tmp_path / "python_prompt.py"
        py_file.write_text('TEMPLATE = "Calculated answer for {{ topic }}"', encoding="utf-8")

        loader = PromptLoader(base_dir=tmp_path, cache=False)
        rendered = loader.load("python_prompt", topic="AsyncIO")
        assert rendered == "Calculated answer for AsyncIO"


def test_escaped_literal_placeholders_preserved() -> None:
    """
    Verify that \\{{ literal \\}} placeholders are preserved as literal {{ literal }}
    without variable substitution and without requiring them in variables.
    """
    loader = PromptLoader()

    template = r"System instructions: use \{{ placeholder \}} format, but substitute {{ active }}."
    rendered = loader.render(template, active="SUCCESS")

    assert rendered == "System instructions: use {{ placeholder }} format, but substitute SUCCESS."


def test_keyword_collision_with_name_parameter() -> None:
    """
    Verify that because PromptLoader uses positional-only (name, /),
    passing name="Alice" as a template variable does not cause a Python argument collision.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        prompt_file = tmp_path / "welcome.md"
        prompt_file.write_text("Welcome {{ name }}, your role is {{ role }}.", encoding="utf-8")

        loader = PromptLoader(base_dir=tmp_path, cache=False)
        # Passes variable 'name' which matches prompt name parameter positionally
        rendered = loader.load("welcome", name="Alice", role="Developer")
        assert rendered == "Welcome Alice, your role is Developer."


def test_unicode_and_multiline_interpolation() -> None:
    """Verify templates support Unicode characters and multiline values."""
    loader = PromptLoader()
    template = "Message for {{ user }}:\n{{ details }}\nStatus: {{ status }} 🚀"

    multiline_details = "Line 1: 漢字 and accented characters éàù\nLine 2: Indented item\nLine 3: 🌟"
    rendered = loader.render(
        template,
        user="山田太郎",
        details=multiline_details,
        status="active",
    )

    assert "Message for 山田太郎:" in rendered
    assert "Line 1: 漢字 and accented characters éàù" in rendered
    assert "Line 3: 🌟" in rendered
    assert "Status: active 🚀" in rendered


def test_missing_variable_raises_key_error() -> None:
    """Verify that referencing an undefined placeholder raises KeyError with clear message."""
    loader = PromptLoader()
    template = "Hello {{ required_var }}, welcome to {{ system }}."

    with pytest.raises(KeyError, match=r"Missing prompt variable: 'system'"):
        loader.render(template, required_var="Alice")


def test_production_caching_behavior() -> None:
    """Verify that when cache is enabled, template file is only read once from disk."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        prompt_file = tmp_path / "cached_prompt.md"
        prompt_file.write_text("Version 1: {{ val }}", encoding="utf-8")

        loader = PromptLoader(base_dir=tmp_path, cache=True)

        # 1st load
        res1 = loader.load("cached_prompt", val="A")
        assert res1 == "Version 1: A"

        # Modify file on disk
        prompt_file.write_text("Version 2: {{ val }}", encoding="utf-8")

        # 2nd load must return cached version 1
        res2 = loader.load("cached_prompt", val="B")
        assert res2 == "Version 1: B"

        # Clear cache -> must now read Version 2
        loader.clear_cache()
        res3 = loader.load("cached_prompt", val="C")
        assert res3 == "Version 2: C"


def test_prompt_loader_validates_name_before_cache_and_avoids_resolve_on_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 6: Validate prompt names before cache lookup and avoid filesystem resolve() on cache hits."""
    prompt_file = tmp_path / "fast_cached.md"
    prompt_file.write_text("Fast {{ x }}", encoding="utf-8")

    loader = PromptLoader(base_dir=tmp_path, cache=True)
    assert loader.load("fast_cached", x="1") == "Fast 1"

    # Even if a malicious key were somehow in _cache, lexical validation rejects it first
    loader._cache["../poisoned"] = "poison"
    with pytest.raises(ValueError, match="Path traversal detected"):
        loader.get_template("../poisoned")

    # On a valid cache hit, Path.resolve must NOT be invoked
    def _fail_resolve(self: Path, strict: bool = False) -> Path:
        raise AssertionError(f"Unexpected Path.resolve() called on cache hit for {self}")

    monkeypatch.setattr(Path, "resolve", _fail_resolve)
    assert loader.load("fast_cached", x="2") == "Fast 2"


def test_prompt_loader_rejects_symlinks_and_sanitizes_versioned_module_names(
    tmp_path: Path,
) -> None:
    """Fix 6: Symlink prompt files/dirs are rejected and versioned .v2 Python files use dot-free module names."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("Outside secret", encoding="utf-8")
    inside_target = prompts_dir / "real.md"
    inside_target.write_text("Inside {{ val }}", encoding="utf-8")

    # Symlink to outside file
    (prompts_dir / "link_outside.md").symlink_to(outside_file)
    # Symlink to inside file
    (prompts_dir / "link_inside.md").symlink_to(inside_target)

    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    with pytest.raises(ValueError, match="Path traversal detected"):
        loader.get_template("link_outside")
    with pytest.raises(ValueError, match="Path traversal detected"):
        loader.get_template("link_inside")

    # Versioned Python prompt module __name__ must be a valid dot-free identifier
    v2_py = prompts_dir / "summarise.v2.py"
    v2_py.write_text(
        'assert "." not in __name__ and __name__.isidentifier()\n'
        'messages = [{"role": "user", "content": "OK {{ v }}"}]\n',
        encoding="utf-8",
    )
    assert loader.load("summarise.v2", v="2") == [{"role": "user", "content": "OK 2"}]

