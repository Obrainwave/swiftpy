from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from swiftpy.ai.prompts.loader import PromptLoader
from swiftpy.console.loader import load_application
from swiftpy.core.config import Config
from swiftpy.http.route_cache import RouteCache


def _paint(text: str, code: str) -> str:
    """Format text with ANSI escape codes only if stdout is a terminal and NO_COLOR is unset."""
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        return f"\033[{code}m{text}\033[0m"
    return text


class CacheCommand:
    """Console commands for building and clearing precompiled application caches."""

    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        # 1. `swiftpy cache:build`
        build_parser = subparsers.add_parser(
            "cache:build",
            help="Precompile routes, configuration, and prompt templates into serialized cache files",
        )
        CacheCommand._add_build_arguments(build_parser)
        build_parser.set_defaults(_handler=CacheCommand.handle_build)

        # 2. `swiftpy cache:clear`
        clear_parser = subparsers.add_parser(
            "cache:clear",
            help="Clear precompiled route, configuration, and prompt caches",
        )
        CacheCommand._add_clear_arguments(clear_parser)
        clear_parser.set_defaults(_handler=CacheCommand.handle_clear)

        # 3. `swiftpy cache` group with `build` and `clear` subcommands
        cache_group_parser = subparsers.add_parser(
            "cache",
            help="Manage application caches",
        )
        cache_subparsers = cache_group_parser.add_subparsers(
            dest="cache_subcommand",
            required=True,
        )

        sub_build = cache_subparsers.add_parser(
            "build",
            help="Precompile routes, configuration, and prompt templates",
        )
        CacheCommand._add_build_arguments(sub_build)
        sub_build.set_defaults(_handler=CacheCommand.handle_build)

        sub_clear = cache_subparsers.add_parser(
            "clear",
            help="Clear precompiled caches",
        )
        CacheCommand._add_clear_arguments(sub_clear)
        sub_clear.set_defaults(_handler=CacheCommand.handle_clear)

    @staticmethod
    def _add_build_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--app",
            default="main:app",
            help="Application ASGI target (default: 'main:app')",
        )
        parser.add_argument(
            "--cache-dir",
            default="bootstrap/cache",
            help="Cache directory path (default: 'bootstrap/cache')",
        )

    @staticmethod
    def _add_clear_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--cache-dir",
            default="bootstrap/cache",
            help="Cache directory path (default: 'bootstrap/cache')",
        )

    @staticmethod
    def handle_build(args: argparse.Namespace) -> None:
        cache_dir = Path(getattr(args, "cache_dir", "bootstrap/cache"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        target_app = getattr(args, "app", "main:app")

        # Clean prior cache to ensure clean compilation
        for f in (
            cache_dir / "routes.json",
            cache_dir / "config.json",
            cache_dir / "prompts.json",
        ):
            if f.is_file():
                f.unlink()

        sys.stdout.write(
            _paint(f"Building application cache into '{cache_dir}'...\n", "1;34")
        )

        # 1. Load application instance
        try:
            app = load_application(target_app)
        except Exception as exc:
            sys.stderr.write(
                _paint(f"Error loading application '{target_app}': {exc}\n", "1;31")
            )
            raise SystemExit(1) from exc

        # 2. Precompile Routes
        routes_cache_file = cache_dir / "routes.json"
        route_data = RouteCache.build(app, cache_path=routes_cache_file)
        sys.stdout.write(
            f"  {_paint('✓', '1;32')} Routes precompiled: {_paint(str(route_data['count']), '1;33')} routes -> {routes_cache_file}\n"
        )

        # 3. Precompile Config
        config_cache_file = cache_dir / "config.json"
        app.config.dump_cache(cache_path=config_cache_file)
        sys.stdout.write(
            f"  {_paint('✓', '1;32')} Configuration precompiled -> {config_cache_file}\n"
        )

        # 4. Precompile Prompts
        prompts_cache_file = cache_dir / "prompts.json"
        prompt_loader = PromptLoader.get_instance()
        prompt_loader.dump_cache(cache_path=prompts_cache_file)
        sys.stdout.write(
            f"  {_paint('✓', '1;32')} Prompt templates precompiled -> {prompts_cache_file}\n"
        )

        sys.stdout.write(
            _paint(
                "✨ Application cache built successfully for production deployment.\n",
                "1;32",
            )
        )

    @staticmethod
    def handle_clear(args: argparse.Namespace) -> None:
        cache_dir = Path(getattr(args, "cache_dir", "bootstrap/cache"))

        routes_file = cache_dir / "routes.json"
        config_file = cache_dir / "config.json"
        prompts_file = cache_dir / "prompts.json"

        RouteCache.clear(routes_file)
        Config.clear_cache(config_file)
        PromptLoader.clear_compiled_cache(prompts_file)

        sys.stdout.write(
            _paint(
                f"✨ Application cache cleared successfully from '{cache_dir}'.\n",
                "1;32",
            )
        )


__all__ = ["CacheCommand"]
