from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from swiftpy import Prompt as RootPrompt
from swiftpy import PromptLoader as RootPromptLoader
from swiftpy import aload_prompt as root_aload_prompt
from swiftpy import load_prompt as root_load_prompt
from swiftpy.ai import Prompt as AIPrompt
from swiftpy.ai.client import AI, AIClient
from swiftpy.ai.prompts import Prompt, aload_prompt
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


def test_prompt_public_exports() -> None:
    assert RootPrompt is Prompt
    assert AIPrompt is Prompt
    assert RootPromptLoader is PromptLoader
    assert root_load_prompt is load_prompt
    assert root_aload_prompt is aload_prompt


def test_load_python_messages_prompt_and_versioned_prompt(prompts_dir: Path) -> None:
    (prompts_dir / "summarise.py").write_text(
        'messages = [\n'
        '    {"role": "system", "content": "Summarise concisely in {{ language }}."},\n'
        '    {"role": "user", "content": "Text:\\n{{ document }}"},\n'
        ']\n',
        encoding="utf-8",
    )
    (prompts_dir / "summarise.v2.py").write_text(
        'messages = [\n'
        '    {"role": "system", "content": "Produce {{ bullets }} bullets for {{ audience }}."},\n'
        '    {"role": "user", "content": "{{ document }}"},\n'
        ']\n',
        encoding="utf-8",
    )
    (prompts_dir / "classify_fn.py").write_text(
        'def messages() -> list[dict[str, str]]:\n'
        '    return [\n'
        '        {"role": "system", "content": "Classify into {{ labels }}."},\n'
        '        {"role": "user", "content": "{{ text }}", "name": "input_msg"},\n'
        '    ]\n',
        encoding="utf-8",
    )

    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    PromptLoader._instance = loader
    try:
        v1 = loader.load("summarise", language="English", document="Hello world")
        assert v1 == [
            {"role": "system", "content": "Summarise concisely in English."},
            {"role": "user", "content": "Text:\nHello world"},
        ]

        v2 = Prompt.load("summarise.v2", bullets=3, audience="engineers", document="Release notes")
        assert v2 == [
            {"role": "system", "content": "Produce 3 bullets for engineers."},
            {"role": "user", "content": "Release notes"},
        ]

        v2_typed = Prompt.load_messages(
            "summarise.v2",
            bullets=5,
            audience="PMs",
            document="Roadmap",
        )
        assert v2_typed == [
            {"role": "system", "content": "Produce 5 bullets for PMs."},
            {"role": "user", "content": "Roadmap"},
        ]

        fn_msgs = Prompt.load_messages("classify_fn", labels="bug, feature", text="Crash on boot")
        assert fn_msgs == [
            {"role": "system", "content": "Classify into bug, feature."},
            {"role": "user", "content": "Crash on boot", "name": "input_msg"},
        ]
    finally:
        PromptLoader.reset_instance()


@pytest.mark.asyncio
async def test_aload_versioned_messages_and_ai_client_multimessage(prompts_dir: Path) -> None:
    (prompts_dir / "summarise.v2.py").write_text(
        'messages = [\n'
        '    {"role": "system", "content": "You summarise in {{ tone }} tone."},\n'
        '    {"role": "user", "content": "{{ body }}"},\n'
        ']\n',
        encoding="utf-8",
    )

    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    PromptLoader._instance = loader
    try:
        rendered = await Prompt.aload("summarise.v2", tone="neutral", body="Details")
        assert rendered == [
            {"role": "system", "content": "You summarise in neutral tone."},
            {"role": "user", "content": "Details"},
        ]

        rendered_msgs = await Prompt.aload_messages(
            "summarise.v2",
            tone="formal",
            body="Report",
        )
        assert rendered_msgs == [
            {"role": "system", "content": "You summarise in formal tone."},
            {"role": "user", "content": "Report"},
        ]

        sync_client = AIClient().prompt("summarise.v2", tone="brief", body="Sync body")
        assert [m.role for m in sync_client._messages] == ["system", "user"]
        assert [m.content for m in sync_client._messages] == [
            "You summarise in brief tone.",
            "Sync body",
        ]

        async_client = await AIClient().aprompt("summarise.v2", tone="direct", body="Async body")
        assert [m.role for m in async_client._messages] == ["system", "user"]
        assert [m.content for m in async_client._messages] == [
            "You summarise in direct tone.",
            "Async body",
        ]

        facade_client = AI.prompt("summarise.v2", model="gpt-4o-mini", tone="crisp", body="Facade body")
        assert facade_client._model == "gpt-4o-mini"
        assert [m.role for m in facade_client._messages] == ["system", "user"]
        assert [m.content for m in facade_client._messages] == [
            "You summarise in crisp tone.",
            "Facade body",
        ]
    finally:
        PromptLoader.reset_instance()


def test_messages_prompt_validation_missing_vars_and_escaping(prompts_dir: Path) -> None:
    (prompts_dir / "escaped_msgs.py").write_text(
        'messages = [\n'
        '    {"role": "system", "content": r"Literal \\{{ keep \\}} and {{ dyn }}"},\n'
        '    {"role": "user", "content": "Input: {{ user_input }}"},\n'
        ']\n',
        encoding="utf-8",
    )
    (prompts_dir / "empty_msgs.py").write_text("messages = []\n", encoding="utf-8")
    (prompts_dir / "bad_type_msgs.py").write_text('messages = "not-a-list"\n', encoding="utf-8")
    (prompts_dir / "bad_content_msgs.py").write_text(
        'messages = [{"role": "user", "content": 42}]\n',
        encoding="utf-8",
    )

    loader = PromptLoader(base_dir=prompts_dir, cache=False)
    assert loader.load("escaped_msgs", dyn="X", user_input="Y") == [
        {"role": "system", "content": "Literal {{ keep }} and X"},
        {"role": "user", "content": "Input: Y"},
    ]

    with pytest.raises(KeyError):
        loader.load("escaped_msgs", dyn="X")

    with pytest.raises(ValueError):
        loader.load("empty_msgs")

    with pytest.raises(TypeError):
        loader.load("bad_type_msgs")

    with pytest.raises(TypeError):
        loader.load("bad_content_msgs")


def test_prompt_loader_uses_ai_prompts_path_from_config(tmp_path: Path) -> None:
    custom_dir = tmp_path / "custom_prompts"
    custom_dir.mkdir(parents=True, exist_ok=True)
    (custom_dir / "classify.py").write_text(
        'messages = [{"role": "user", "content": "Classify: {{ topic }}"}]\n',
        encoding="utf-8",
    )

    cfg = Config()
    cfg.set("ai.prompts.path", str(custom_dir))
    cfg.set("ai.prompts.cache", False)

    loader = PromptLoader(config=cfg)
    assert loader.base_dir == custom_dir
    assert loader.load("classify", topic="billing") == [
        {"role": "user", "content": "Classify: billing"},
    ]


def test_prompt_compiled_cache_roundtrip_for_messages_and_versioned_files(
    prompts_dir: Path, tmp_path: Path
) -> None:
    (prompts_dir / "summarise.v2.py").write_text(
        'messages = [\n'
        '    {"role": "system", "content": "V2 for {{ user }}"},\n'
        '    {"role": "user", "content": "{{ content }}"},\n'
        ']\n',
        encoding="utf-8",
    )

    cfg: dict[str, Any] = {"app": {"env": "production"}, "ai": {"prompts": {"cache": True}}}
    config = Config()
    config.set("app.env", cfg["app"]["env"])
    config.set("ai.prompts.cache", True)

    cache_file = tmp_path / "prompts_cache.json"
    writer = PromptLoader(base_dir=prompts_dir, config=config)
    written = writer.dump_cache(cache_file)
    assert written == cache_file

    # Remove source file so reader must serve from compiled cache
    (prompts_dir / "summarise.v2.py").unlink()

    reader = PromptLoader(base_dir=prompts_dir, config=config)
    reader._load_compiled_cache(cache_file)
    assert reader.load("summarise.v2", user="Alice", content="Cached doc") == [
        {"role": "system", "content": "V2 for Alice"},
        {"role": "user", "content": "Cached doc"},
    ]


@pytest.mark.asyncio
async def test_multiple_ai_clients_with_independent_prompt_configs_and_singleton_order(
    tmp_path: Path,
) -> None:
    """Fix 5: Multiple AIClient instances with distinct Configs use their own prompt directories independently."""
    dir_default = tmp_path / "prompts_default"
    dir_a = tmp_path / "prompts_a"
    dir_b = tmp_path / "prompts_b"
    for d, tag in ((dir_default, "DEFAULT"), (dir_a, "CLIENT_A"), (dir_b, "CLIENT_B")):
        d.mkdir(parents=True, exist_ok=True)
        (d / "greeting.v2.py").write_text(
            f'messages = [{{"role": "user", "content": "{tag}: {{{{ who }}}}"}}]\n',
            encoding="utf-8",
        )

    PromptLoader.reset_instance()
    try:
        # Even if PromptLoader singleton is initialized first with dir_default
        PromptLoader._instance = PromptLoader(base_dir=dir_default, cache=False)

        cfg_a = Config({"ai": {"prompts": {"path": str(dir_a), "cache": False}}})
        cfg_b = Config({"ai": {"prompts": {"path": str(dir_b), "cache": False}}})

        client_a = AIClient(config=cfg_a).prompt("greeting.v2", who="Alice")
        client_b = AIClient(config=cfg_b).prompt("greeting.v2", who="Bob")
        client_default = AIClient().prompt("greeting.v2", who="Charlie")

        assert client_a._messages[0].content == "CLIENT_A: Alice"
        assert client_b._messages[0].content == "CLIENT_B: Bob"
        assert client_default._messages[0].content == "DEFAULT: Charlie"

        # Cloning preserves isolated custom prompt config
        cloned_a = client_a.clone(include_messages=False)
        await cloned_a.aprompt("greeting.v2", who="AsyncAlice")
        assert cloned_a._messages[0].content == "CLIENT_A: AsyncAlice"

        # Concurrent async loads across independent clients remain isolated
        async def _load_pair(idx: int) -> tuple[str, str]:
            ca = await AIClient(config=cfg_a).aprompt("greeting.v2", who=f"A{idx}")
            cb = await AIClient(config=cfg_b).aprompt("greeting.v2", who=f"B{idx}")
            return ca._messages[0].content, cb._messages[0].content

        pairs = await asyncio.gather(*(_load_pair(i) for i in range(10)))
        for i, (res_a, res_b) in enumerate(pairs):
            assert res_a == f"CLIENT_A: A{i}"
            assert res_b == f"CLIENT_B: B{i}"
    finally:
        PromptLoader.reset_instance()



