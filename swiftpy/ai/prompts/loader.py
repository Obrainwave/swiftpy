from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from swiftpy.core.config import Config

logger = logging.getLogger("swiftpy.ai.prompts")

VARIABLE_PATTERN = re.compile(r"(?<!\\)\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
ESCAPED_OPEN_PATTERN = re.compile(r"\\\{\{")
ESCAPED_CLOSE_PATTERN = re.compile(r"\\\}\}")


class PromptLoader:
    """
    Prompt manager responsible for loading prompt templates from disk,
    interpolating {{ variable }} placeholders, and caching in production.
    """

    _instance: PromptLoader | None = None

    def __init__(
        self,
        base_dir: str | Path | None = None,
        cache: bool | None = None,
        config: Config | None = None,
    ) -> None:
        if base_dir is None:
            self._base_dir = Path("prompts")
        else:
            self._base_dir = Path(base_dir)

        self._cache_override = cache
        if config is None:
            try:
                self._config = Config.get_instance()
            except RuntimeError:
                self._config = Config()
        else:
            self._config = config

        self._cache: dict[str, str] = {}
        self._load_compiled_cache()

    def _load_compiled_cache(
        self, cache_path: str | Path = "bootstrap/cache/prompts.json"
    ) -> None:
        p = Path(cache_path)
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, dict):
                    self._cache.update(cached)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not read prompt cache: %s", exc)

    def dump_cache(
        self, cache_path: str | Path = "bootstrap/cache/prompts.json"
    ) -> Path:
        """Scan base prompts directory and precompile templates into a serialized cache."""
        target = Path(cache_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, str] = {}
        base = self._base_dir.resolve()
        if base.is_dir():
            for p in sorted(base.glob("*")):
                if (
                    p.is_file()
                    and p.suffix in (".md", ".py")
                    and not p.name.startswith(("_", "."))
                ):
                    name = p.stem
                    if name not in data:
                        try:
                            data[name] = self.get_template(name)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("Skipping prompt '%s': %s", name, exc)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return target

    @classmethod
    def clear_compiled_cache(
        cls, cache_path: str | Path = "bootstrap/cache/prompts.json"
    ) -> bool:
        """Remove serialized prompt cache file if present."""
        target = Path(cache_path)
        if target.is_file():
            target.unlink()
            return True
        return False

    @classmethod
    def get_instance(cls) -> PromptLoader:
        """Return or create a singleton instance of PromptLoader."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (useful in tests)."""
        cls._instance = None

    @property
    def base_dir(self) -> Path:
        """Return the base prompts directory."""
        return self._base_dir

    def is_cache_enabled(self) -> bool:
        """
        Check if prompt caching is active.

        Caching is enabled when explicitly configured or when running in production
        (APP_ENV == 'production').
        """
        if self._cache_override is not None:
            return self._cache_override

        cfg_cache = self._config.get("ai.prompts.cache")
        if cfg_cache is not None:
            return bool(cfg_cache)

        app_env = str(
            self._config.get("app.env", os.environ.get("APP_ENV", "local"))
        ).lower()
        return app_env == "production"

    def clear_cache(self) -> None:
        """Clear all cached prompt templates."""
        self._cache.clear()

    def get_template(self, name: str, /) -> str:
        """
        Load raw template string for the given prompt name without variable substitution.

        Searches for {name}.md then {name}.py in the base directory (explicit precedence: .md preferred).
        Guards against directory traversal by verifying resolved path containment.
        Caches the template string when caching is enabled.
        """
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Prompt name cannot be empty.")

        # Guard against path traversal
        base_resolved = self._base_dir.resolve()
        path_md = (self._base_dir / f"{clean_name}.md").resolve()
        path_py = (self._base_dir / f"{clean_name}.py").resolve()

        if not path_md.is_relative_to(base_resolved) or not path_py.is_relative_to(base_resolved):
            raise ValueError(f"Path traversal detected in prompt name: '{clean_name}'")

        if self.is_cache_enabled() and clean_name in self._cache:
            return self._cache[clean_name]

        template_str: str | None = None

        if path_md.is_file():
            template_str = path_md.read_text(encoding="utf-8")
        elif path_py.is_file():
            template_str = self._load_from_python_file(path_py)
        else:
            raise FileNotFoundError(
                f"Prompt '{clean_name}' not found. Searched {path_md} and {path_py}."
            )

        if self.is_cache_enabled():
            self._cache[clean_name] = template_str

        return template_str

    def _load_from_python_file(self, path: Path) -> str:
        """Load prompt template string from a Python script."""
        module_name = f"_swiftpy_prompt_{path.stem}_{abs(hash(str(path)))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load Python prompt specification from {path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for attr in ("PROMPT", "prompt", "TEMPLATE", "template"):
            if hasattr(module, attr):
                val = getattr(module, attr)
                if isinstance(val, str):
                    return val
                if callable(val):
                    res = val()
                    if isinstance(res, str):
                        return res

        if module.__doc__ and module.__doc__.strip():
            return module.__doc__.strip()

        raise ValueError(
            f"Python prompt file '{path}' does not define 'PROMPT', 'prompt', or 'TEMPLATE' (str or callable)."
        )

    def render(self, template: str, /, **variables: Any) -> str:
        """
        Interpolate {{ variable }} placeholders in the template string with supplied variables.

        Supports escaped \\{{ ... \\}} placeholders to preserve literal braces without substitution.
        Raises KeyError if any referenced placeholder is missing from variables.
        """
        def replacer(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in variables:
                raise KeyError(
                    f"Missing prompt variable: '{key}'. Provided variables: {list(variables.keys())}"
                )
            return str(variables[key])

        rendered = VARIABLE_PATTERN.sub(replacer, template)
        rendered = ESCAPED_OPEN_PATTERN.sub("{{", rendered)
        return ESCAPED_CLOSE_PATTERN.sub("}}", rendered)

    def load(self, name: str, /, **variables: Any) -> str:
        """
        Load prompt by name, interpolate {{ variable }} placeholders, and return rendered prompt.
        """
        template = self.get_template(name)
        if not variables:
            # If no variables passed, check if there are remaining placeholders
            remaining = VARIABLE_PATTERN.findall(template)
            if remaining:
                missing = ", ".join(sorted(set(remaining)))
                raise KeyError(
                    f"Missing required prompt variables: {missing} for prompt '{name}'"
                )
            return template
        return self.render(template, **variables)


def load_prompt(name: str, /, **variables: Any) -> str:
    """
    Convenience function to load and interpolate a prompt using the default PromptLoader.
    """
    return PromptLoader.get_instance().load(name, **variables)
