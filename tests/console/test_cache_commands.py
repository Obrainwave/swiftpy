from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from swiftpy.ai.prompts.loader import PromptLoader
from swiftpy.console.cli import CLI
from swiftpy.console.commands.cache import CacheCommand
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.http.route_cache import RouteCache


def test_route_cache_build_load_clear(tmp_path: Path) -> None:
    app = Application()

    @app.get("/test-route")
    async def sample_handler() -> str:
        return "ok"

    cache_file = tmp_path / "routes.json"

    # 1. Build
    data = RouteCache.build(app, cache_path=cache_file)
    assert cache_file.is_file()
    assert data["count"] >= 1
    assert any(r["path"] == "/test-route" for r in data["routes"])

    # 2. Load
    loaded = RouteCache.load(cache_path=cache_file)
    assert loaded is not None
    assert loaded["count"] == data["count"]

    # 3. Clear
    cleared = RouteCache.clear(cache_path=cache_file)
    assert cleared is True
    assert not cache_file.exists()
    assert RouteCache.load(cache_path=cache_file) is None


def test_config_dump_load_cache(tmp_path: Path) -> None:
    cfg = Config({"app": {"name": "TestApp", "env": "production"}})
    cache_file = tmp_path / "config.json"

    # 1. Dump
    dumped_path = cfg.dump_cache(cache_path=cache_file)
    assert dumped_path.is_file()

    # 2. Load
    with open(cache_file, "r", encoding="utf-8") as f:
        loaded_dict = json.load(f)
    assert loaded_dict["app"]["name"] == "TestApp"

    # 3. Clear
    assert Config.clear_cache(cache_path=cache_file) is True
    assert not cache_file.exists()


def test_prompt_loader_dump_and_precompiled_cache(tmp_path: Path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "greeting.md").write_text("Hello {{ name }}!", encoding="utf-8")

    loader = PromptLoader(base_dir=prompts_dir)
    cache_file = tmp_path / "prompts.json"

    # 1. Dump
    loader.dump_cache(cache_path=cache_file)
    assert cache_file.is_file()
    with open(cache_file, "r", encoding="utf-8") as f:
        cache_data = json.load(f)
    assert "greeting" in cache_data
    assert cache_data["greeting"] == "Hello {{ name }}!"

    # 2. Clear
    assert PromptLoader.clear_compiled_cache(cache_path=cache_file) is True
    assert not cache_file.exists()


def test_cli_cache_build_and_clear_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_app = Application()

    @test_app.get("/ping-test")
    async def ping_h() -> str:
        return "pong"

    # Mock load_application to return our test_app
    monkeypatch.setattr(
        "swiftpy.console.commands.cache.load_application", lambda _target: test_app
    )

    cache_dir = tmp_path / "cache"

    # Test handle_build
    args_build = argparse.Namespace(app="main:app", cache_dir=str(cache_dir))
    CacheCommand.handle_build(args_build)

    assert (cache_dir / "routes.json").is_file()
    assert (cache_dir / "config.json").is_file()
    assert (cache_dir / "prompts.json").is_file()

    # Test handle_clear
    args_clear = argparse.Namespace(cache_dir=str(cache_dir))
    CacheCommand.handle_clear(args_clear)

    assert not (cache_dir / "routes.json").exists()
    assert not (cache_dir / "config.json").exists()
    assert not (cache_dir / "prompts.json").exists()


def test_cli_parser_registration() -> None:
    cli = CLI()

    # Test cache:build
    args1 = cli.parser.parse_args(["cache:build", "--cache-dir", "custom/cache"])
    assert args1.command == "cache:build"
    assert args1.cache_dir == "custom/cache"

    # Test cache:clear
    args2 = cli.parser.parse_args(["cache:clear", "--cache-dir", "custom/cache"])
    assert args2.command == "cache:clear"
    assert args2.cache_dir == "custom/cache"

    # Test cache build
    args3 = cli.parser.parse_args(["cache", "build"])
    assert args3.command == "cache"
    assert args3.cache_subcommand == "build"

    # Test cache clear
    args4 = cli.parser.parse_args(["cache", "clear"])
    assert args4.command == "cache"
    assert args4.cache_subcommand == "clear"
