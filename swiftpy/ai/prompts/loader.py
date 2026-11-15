from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from swiftpy.core.config import Config

logger = logging.getLogger("swiftpy.ai.prompts")

_DEFAULT_CACHE_DIR = Path("bootstrap/cache")

_TEMPLATE_TOKEN_PATTERN = re.compile(
    r"(?P<pair>(?:\\\\)+)(?=(?:\\?(?:\{\{|\}\})))"
    r"|\\(?P<escaped>\{\{|\}\})"
    r"|\{\{\s*(?P<var>[a-zA-Z0-9_]+)\s*\}\}"
)
VARIABLE_PATTERN = re.compile(r"(?<!\\)\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
ESCAPED_OPEN_PATTERN = re.compile(r"\\\{\{")
ESCAPED_CLOSE_PATTERN = re.compile(r"\\\}\}")


def _extract_variables(template: str) -> list[str]:
    """Extract unescaped {{ variable }} placeholder names in left-to-right order."""
    return [
        m.group("var")
        for m in _TEMPLATE_TOKEN_PATTERN.finditer(template)
        if m.group("var") is not None
    ]


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

        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._load_compiled_cache()

    def _default_cache_path(self) -> Path:
        """Compute deterministic precompiled cache path isolated to this loader's resolved base_dir."""
        base_hash = hashlib.sha256(
            str(self._base_dir.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        return _DEFAULT_CACHE_DIR / f"prompts_{base_hash}.json"

    @staticmethod
    def _sidecar_meta_path(cache_path: Path) -> Path:
        return cache_path.with_name(f"{cache_path.name}.meta.json")

    def _load_compiled_cache(self, cache_path: str | Path | None = None) -> None:
        p = Path(cache_path) if cache_path is not None else self._default_cache_path()
        if not p.is_file():
            return

        meta_path = self._sidecar_meta_path(p)
        expected_base = str(self._base_dir.resolve())
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(meta, dict) and meta.get("base_dir") != expected_base:
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not read prompt cache metadata: %s", exc)
                return
        elif cache_path is None:
            # Scoped default filename already hashes resolved base_dir
            pass

        try:
            with open(p, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict):
                with self._lock:
                    for key, value in cached.items():
                        if isinstance(key, str) and isinstance(value, str):
                            self._cache[key] = value
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read prompt cache: %s", exc)

    def dump_cache(self, cache_path: str | Path | None = None) -> Path:
        """Recursively scan base prompts directory and precompile templates into a serialized cache."""
        target = Path(cache_path) if cache_path is not None else self._default_cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, str] = {}
        base = self._base_dir.resolve()
        if base.is_dir():
            candidates = sorted(
                (
                    p
                    for p in base.rglob("*")
                    if p.is_file()
                    and p.suffix in (".md", ".py")
                    and not any(part.startswith(("_", ".")) for part in p.relative_to(base).parts)
                ),
                key=lambda p: (0 if p.suffix == ".md" else 1, p.relative_to(base).as_posix()),
            )
            for p in candidates:
                rel_name = p.relative_to(base).with_suffix("").as_posix()
                if rel_name not in data:
                    try:
                        data[rel_name] = self.get_template(rel_name)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Skipping prompt '%s': %s", rel_name, exc)

        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        meta_path = self._sidecar_meta_path(target)
        meta_path.write_text(
            json.dumps({"base_dir": str(base)}, indent=2),
            encoding="utf-8",
        )
        return target

    @classmethod
    def clear_compiled_cache(cls, cache_path: str | Path | None = None) -> bool:
        """Remove serialized prompt cache file(s) and metadata sidecars if present."""
        removed = False
        if cache_path is not None:
            target = Path(cache_path)
            if target.is_file():
                target.unlink()
                removed = True
            meta = cls._sidecar_meta_path(target)
            if meta.is_file():
                meta.unlink()
            return removed

        if _DEFAULT_CACHE_DIR.is_dir():
            for p in _DEFAULT_CACHE_DIR.glob("prompts*.json*"):
                if p.is_file():
                    p.unlink()
                    removed = True
        return removed

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

        app_env = str(self._config.get("app.env", "local")).lower()
        return app_env == "production"

    def clear_cache(self) -> None:
        """Clear all cached prompt templates."""
        with self._lock:
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

        with self._lock:
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

        with self._lock:
            if self.is_cache_enabled():
                self._cache[clean_name] = template_str

        return template_str

    async def aget_template(self, name: str, /) -> str:
        """Asynchronously load raw template string off the asyncio event loop."""
        return await asyncio.to_thread(self.get_template, name)

    def _load_from_python_file(self, path: Path) -> str:
        """Load prompt template string from a Python script."""
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
        module_name = f"_swiftpy_prompt_{path.stem}_{digest}"
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
        Interpolate {{ variable }} placeholders in a single pass.

        Supports escaped \\{{ ... \\}} placeholders without corrupting substituted
        variable values or preceding literal backslash pairs (e.g. C:\\\\{{ var }}).
        Raises KeyError if any referenced placeholder is missing from variables.
        """
        def replacer(match: re.Match[str]) -> str:
            pair = match.group("pair")
            if pair is not None:
                return "\\" * (len(pair) // 2)
            escaped = match.group("escaped")
            if escaped is not None:
                return escaped
            key = match.group("var")
            if key not in variables:
                raise KeyError(
                    f"Missing prompt variable: '{key}'. Provided variables: {list(variables.keys())}"
                )
            return str(variables[key])

        return _TEMPLATE_TOKEN_PATTERN.sub(replacer, template)

    def load(self, name: str, /, **variables: Any) -> str:
        """
        Load prompt by name, interpolate {{ variable }} placeholders, and return rendered prompt.
        """
        template = self.get_template(name)
        if not variables:
            remaining = _extract_variables(template)
            if remaining:
                missing = ", ".join(sorted(set(remaining)))
                raise KeyError(
                    f"Missing required prompt variables: {missing} for prompt '{name}'"
                )
        return self.render(template, **variables)

    async def aload(self, name: str, /, **variables: Any) -> str:
        """
        Asynchronously load prompt by name off the event loop, interpolate variables,
        and return the rendered prompt.
        """
        template = await self.aget_template(name)
        if not variables:
            remaining = _extract_variables(template)
            if remaining:
                missing = ", ".join(sorted(set(remaining)))
                raise KeyError(
                    f"Missing required prompt variables: {missing} for prompt '{name}'"
                )
        return self.render(template, **variables)


def load_prompt(name: str, /, **variables: Any) -> str:
    """
    Convenience function to load and interpolate a prompt using the default PromptLoader.
    """
    return PromptLoader.get_instance().load(name, **variables)


async def aload_prompt(name: str, /, **variables: Any) -> str:
    """
    Asynchronously load and interpolate a prompt using the default PromptLoader.
    """
    return await PromptLoader.get_instance().aload(name, **variables)
