from __future__ import annotations

from pathlib import Path

import pytest

from swiftpy.ai.client import AI, AIClient
from swiftpy.ai.prompts.loader import PromptLoader, load_prompt
from swiftpy.core.config import Config


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    p_dir = tmp_path / "prompts"
    p_dir.mkdir(parents=True, exist_ok=True)
    return p_dir


def test_load_markdown_prompt(prompts_dir: Path) -> None:
    md_file = prompts_dir / "greeting.md"
    md_file.write_text("Hello, {{ name }}! Welcome to {{ app }}.", encoding="utf-8")

    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    rendered = loader.load("greeting", name="Alice", app="SwiftPY")
    assert rendered == "Hello, Alice! Welcome to SwiftPY."


def test_load_markdown_prompt_with_whitespace_in_tags(prompts_dir: Path) -> None:
    md_file = prompts_dir / "user_info.md"
    md_file.write_text("User: {{username}}, Role: {{   role   }}", encoding="utf-8")

    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    rendered = loader.load("user_info", username="johndoe", role="admin")
    assert rendered == "User: johndoe, Role: admin"


def test_load_python_prompt_string(prompts_dir: Path) -> None:
    py_file = prompts_dir / "assistant_code.py"
    py_file.write_text(
        'PROMPT = "You are a senior {{ language }} engineer assisting with {{ task }}."',
        encoding="utf-8",
    )

    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    rendered = loader.load("assistant_code", language="Python", task="refactoring")
    assert rendered == "You are a senior Python engineer assisting with refactoring."


def test_load_python_prompt_callable(prompts_dir: Path) -> None:
    py_file = prompts_dir / "system_builder.py"
    py_file.write_text(
        'def prompt():\n    return "System prompt for {{ domain }} version {{ version }}."',
        encoding="utf-8",
    )

    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    rendered = loader.load("system_builder", domain="finance", version="2.0")
    assert rendered == "System prompt for finance version 2.0."


def test_missing_prompt_variable_raises_key_error(prompts_dir: Path) -> None:
    md_file = prompts_dir / "invoice.md"
    md_file.write_text("Invoice #{{ id }} for {{ client }} total ${{ amount }}", encoding="utf-8")

    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    with pytest.raises(KeyError) as exc_info:
        loader.load("invoice", id="123", client="Acme Corp")
    assert "amount" in str(exc_info.value)


def test_prompt_file_not_found(prompts_dir: Path) -> None:
    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    with pytest.raises(FileNotFoundError) as exc_info:
        loader.load("non_existent_prompt")
    assert "non_existent_prompt" in str(exc_info.value)


def test_prompt_caching_in_production(prompts_dir: Path) -> None:
    cfg = Config()
    cfg.set("app.env", "production")

    md_file = prompts_dir / "cached_prompt.md"
    md_file.write_text("Version 1: {{ val }}", encoding="utf-8")

    loader = PromptLoader(base_dir=prompts_dir, config=cfg)
    assert loader.is_cache_enabled() is True

    # First load
    first_render = loader.load("cached_prompt", val="Initial")
    assert first_render == "Version 1: Initial"

    # Mutate file on disk
    md_file.write_text("Version 2: {{ val }}", encoding="utf-8")

    # Second load returns cached template (Version 1)
    second_render = loader.load("cached_prompt", val="Subsequent")
    assert second_render == "Version 1: Subsequent"

    # Clearing cache picks up new file content
    loader.clear_cache()
    third_render = loader.load("cached_prompt", val="Refreshed")
    assert third_render == "Version 2: Refreshed"


def test_prompt_without_caching_in_development(prompts_dir: Path) -> None:
    cfg = Config()
    cfg.set("app.env", "local")

    md_file = prompts_dir / "dev_prompt.md"
    md_file.write_text("Original: {{ val }}", encoding="utf-8")

    loader = PromptLoader(base_dir=prompts_dir, config=cfg)
    assert loader.is_cache_enabled() is False

    assert loader.load("dev_prompt", val="A") == "Original: A"

    # Mutate file on disk
    md_file.write_text("Updated: {{ val }}", encoding="utf-8")

    # In development, picks up file modification immediately
    assert loader.load("dev_prompt", val="B") == "Updated: B"


def test_ai_client_prompt_builder_integration(prompts_dir: Path) -> None:
    md_file = prompts_dir / "welcome_agent.md"
    md_file.write_text("Welcome {{ user }} to our platform.", encoding="utf-8")

    # Temporarily point global PromptLoader to prompts_dir
    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    PromptLoader._instance = loader
    try:
        assert load_prompt("welcome_agent", user="Charlie") == "Welcome Charlie to our platform."

        client = AIClient().prompt("welcome_agent", role="system", user="Dave")
        assert len(client._messages) == 1
        assert client._messages[0].role == "system"
        assert client._messages[0].content == "Welcome Dave to our platform."

        # Test through AI facade
        facade_client = AI.prompt("welcome_agent", model="gpt-4o", user="Eve")
        assert facade_client._model == "gpt-4o"
        assert len(facade_client._messages) == 1
        assert facade_client._messages[0].role == "user"
        assert facade_client._messages[0].content == "Welcome Eve to our platform."
    finally:
        PromptLoader.reset_instance()

