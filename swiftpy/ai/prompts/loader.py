from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import re
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from swiftpy.core.config import Config

logger = logging.getLogger("swiftpy.ai.prompts")

PromptMessageDict = dict[str, str]
PromptTemplateValue = str | list[PromptMessageDict]

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
    _auto_instance: PromptLoader | None = None

    def __init__(
        self,
        base_dir: str | Path | None = None,
        cache: bool | None = None,
        config: Config | None = None,
    ) -> None:
        self._cache_override = cache
        if config is None:
            try:
                self._config = Config.get_instance()
            except RuntimeError:
                self._config = Config()
        else:
            self._config = config

        if base_dir is None:
            cfg_path = self._config.get("ai.prompts.path")
            if isinstance(cfg_path, (str, Path)) and str(cfg_path).strip():
                self._base_dir = Path(cfg_path)
            else:
                self._base_dir = Path("prompts")
        else:
            self._base_dir = Path(base_dir)

        self._lock = threading.Lock()
        self._cache: dict[str, PromptTemplateValue] = {}
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

    @staticmethod
    def _clone_template(value: PromptTemplateValue) -> PromptTemplateValue:
        if isinstance(value, str):
            return value
        return [dict(item) for item in value]

    @staticmethod
    def _normalize_messages_list(
        raw: Any,
        source: Path | str,
    ) -> list[PromptMessageDict]:
        if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
            raise TypeError(
                f"Prompt '{source}' 'messages' must be a non-empty sequence of message mappings."
            )
        normalized: list[PromptMessageDict] = []
        for idx, item in enumerate(raw):
            if isinstance(item, Mapping):
                role = item.get("role")
                content = item.get("content")
                name = item.get("name")
            elif hasattr(item, "role") and hasattr(item, "content"):
                role = item.role
                content = item.content
                name = getattr(item, "name", None)
            else:
                raise ValueError(
                    f"Prompt '{source}' message at index {idx} must be a mapping or Message with 'role' and 'content'."
                )
            if not isinstance(role, str) or not role.strip():
                raise ValueError(
                    f"Prompt '{source}' message at index {idx} must define a non-empty string 'role'."
                )
            if not isinstance(content, str):
                raise TypeError(
                    f"Prompt '{source}' message at index {idx} must define a string 'content'."
                )
            entry: PromptMessageDict = {"role": role.strip(), "content": content}
            if isinstance(name, str) and name:
                entry["name"] = name
            normalized.append(entry)
        if not normalized:
            raise ValueError(
                f"Prompt '{source}' defines an empty 'messages' list."
            )
        return normalized

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
                        if not isinstance(key, str):
                            continue
                        if isinstance(value, str):
                            self._cache[key] = value
                        elif isinstance(value, list):
                            try:
                                self._cache[key] = self._normalize_messages_list(
                                    value, key
                                )
                            except (TypeError, ValueError):
                                continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read prompt cache: %s", exc)

    def dump_cache(self, cache_path: str | Path | None = None) -> Path:
        """Recursively scan base prompts directory and precompile templates into a serialized cache."""
        target = Path(cache_path) if cache_path is not None else self._default_cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, PromptTemplateValue] = {}
        base = self._base_dir.resolve()
        if base.is_dir() and not self._base_dir.is_symlink():
            candidates = sorted(
                (
                    p
                    for p in base.rglob("*")
                    if p.is_file()
                    and not p.is_symlink()
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
        """
        Return or create a singleton instance of PromptLoader.

        When the singleton was automatically created (rather than manually pinned
        to `cls._instance`), re-binds to the active application's `Config` if the
        active `Config` instance has changed.
        """
        if cls._instance is not None and cls._instance is not cls._auto_instance:
            return cls._instance

        try:
            active_cfg: Config | None = Config.get_instance()
        except RuntimeError:
            active_cfg = None

        if (
            cls._instance is None
            or (
                active_cfg is not None
                and cls._instance._config is not active_cfg
            )
        ):
            created = cls(config=active_cfg)
            cls._instance = created
            cls._auto_instance = created
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (useful in tests)."""
        cls._instance = None
        cls._auto_instance = None

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

    @staticmethod
    def _validate_prompt_name(name: str) -> str:
        """Validate prompt name lexically against empty names or path traversal tokens."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Prompt name cannot be empty.")

        if (
            "\\" in clean_name
            or clean_name.startswith("/")
            or clean_name.endswith("/")
            or "//" in clean_name
            or Path(clean_name).is_absolute()
        ):
            raise ValueError(f"Path traversal detected in prompt name: '{clean_name}'")

        parts = clean_name.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"Path traversal detected in prompt name: '{clean_name}'")

        return clean_name

    @staticmethod
    def _verify_no_symlinks_within_base(
        candidate: Path,
        base_dir: Path,
        clean_name: str,
    ) -> None:
        """Reject symlinks inside the prompt base directory."""
        if base_dir.is_symlink():
            raise ValueError(f"Path traversal detected in prompt name: '{clean_name}'")
        current = candidate
        while current != base_dir:
            if current.is_symlink():
                raise ValueError(
                    f"Path traversal detected in prompt name: '{clean_name}'"
                )
            if current.parent == current:
                break
            current = current.parent

    def get_template(self, name: str, /) -> PromptTemplateValue:
        """
        Load raw template string or message list for the given prompt name without
        variable substitution.

        Searches for {name}.md then {name}.py in the base directory (explicit precedence: .md preferred).
        Guards against directory traversal and symlinks by verifying lexical tokens before cache
        lookup and verifying resolved path containment on cache miss.
        """
        clean_name = self._validate_prompt_name(name)

        with self._lock:
            if self.is_cache_enabled() and clean_name in self._cache:
                return self._clone_template(self._cache[clean_name])

        raw_md = self._base_dir / f"{clean_name}.md"
        raw_py = self._base_dir / f"{clean_name}.py"
        self._verify_no_symlinks_within_base(raw_md, self._base_dir, clean_name)
        self._verify_no_symlinks_within_base(raw_py, self._base_dir, clean_name)

        base_resolved = self._base_dir.resolve()
        path_md = raw_md.resolve()
        path_py = raw_py.resolve()

        if not path_md.is_relative_to(base_resolved) or not path_py.is_relative_to(base_resolved):
            raise ValueError(f"Path traversal detected in prompt name: '{clean_name}'")

        template_val: PromptTemplateValue | None = None
        if path_md.is_file():
            template_val = path_md.read_text(encoding="utf-8")
        elif path_py.is_file():
            template_val = self._load_from_python_file(path_py)
        else:
            raise FileNotFoundError(
                f"Prompt '{clean_name}' not found. Searched {path_md} and {path_py}."
            )

        with self._lock:
            if self.is_cache_enabled():
                self._cache[clean_name] = self._clone_template(template_val)

        return self._clone_template(template_val)

    async def aget_template(self, name: str, /) -> PromptTemplateValue:
        """Asynchronously load raw template string or message list off the asyncio event loop."""
        return await asyncio.to_thread(self.get_template, name)

    def _load_from_python_file(self, path: Path) -> PromptTemplateValue:
        """Load prompt template string or `messages` list from a Python module."""
        resolved_path = path.resolve()
        digest = hashlib.sha256(str(resolved_path).encode("utf-8")).hexdigest()[:16]
        safe_stem = re.sub(r"[^a-zA-Z0-9_]", "_", path.stem).strip("_") or "prompt"
        if safe_stem[0].isdigit():
            safe_stem = f"p_{safe_stem}"
        module_name = f"_swiftpy_prompt_{safe_stem}_{digest}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load Python prompt specification from {path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for attr in ("MESSAGES", "messages"):
            if hasattr(module, attr):
                val = getattr(module, attr)
                if callable(val):
                    val = val()
                return self._normalize_messages_list(val, path)

        for attr in ("PROMPT", "prompt", "TEMPLATE", "template"):
            if hasattr(module, attr):
                val = getattr(module, attr)
                if isinstance(val, str):
                    return val
                if callable(val):
                    res = val()
                    if isinstance(res, str):
                        return res
                    if isinstance(res, Sequence) and not isinstance(res, (bytes, bytearray)):
                        return self._normalize_messages_list(res, path)
                elif isinstance(val, Sequence) and not isinstance(val, (bytes, bytearray)):
                    return self._normalize_messages_list(val, path)

        if module.__doc__ and module.__doc__.strip():
            return module.__doc__.strip()

        raise ValueError(
            f"Python prompt file '{path}' does not define 'messages', 'MESSAGES', 'PROMPT', 'prompt', or 'TEMPLATE'."
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

    def render_messages(
        self,
        messages_template: Sequence[Mapping[str, str]],
        /,
        **variables: Any,
    ) -> list[PromptMessageDict]:
        """Interpolate {{ variable }} placeholders across a sequence of message dicts."""
        rendered: list[PromptMessageDict] = []
        for msg in messages_template:
            item: PromptMessageDict = {
                "role": str(msg["role"]),
                "content": self.render(str(msg["content"]), **variables),
            }
            if msg.get("name"):
                item["name"] = str(msg["name"])
            rendered.append(item)
        return rendered

    def _render_loaded_template(
        self,
        name: str,
        template: PromptTemplateValue,
        variables: Mapping[str, Any],
    ) -> PromptTemplateValue:
        if isinstance(template, str):
            if not variables:
                remaining = _extract_variables(template)
                if remaining:
                    missing = ", ".join(sorted(set(remaining)))
                    raise KeyError(
                        f"Missing required prompt variables: {missing} for prompt '{name}'"
                    )
            return self.render(template, **variables)

        if not variables:
            remaining_list: list[str] = []
            for msg in template:
                remaining_list.extend(_extract_variables(msg["content"]))
            if remaining_list:
                missing = ", ".join(sorted(set(remaining_list)))
                raise KeyError(
                    f"Missing required prompt variables: {missing} for prompt '{name}'"
                )
        return self.render_messages(template, **variables)

    def load(self, name: str, /, **variables: Any) -> PromptTemplateValue:
        """
        Load prompt by name, interpolate {{ variable }} placeholders, and return
        the rendered string or rendered message list.
        """
        template = self.get_template(name)
        return self._render_loaded_template(name, template, variables)

    async def aload(self, name: str, /, **variables: Any) -> PromptTemplateValue:
        """
        Asynchronously load prompt by name off the event loop, interpolate variables,
        and return the rendered string or rendered message list.
        """
        template = await self.aget_template(name)
        return self._render_loaded_template(name, template, variables)

    def load_messages(
        self,
        name: str,
        /,
        *,
        default_role: str = "user",
        **variables: Any,
    ) -> list[PromptMessageDict]:
        """Load prompt and normalize output as a rendered list of message dictionaries."""
        rendered = self.load(name, **variables)
        if isinstance(rendered, list):
            return rendered
        return [{"role": default_role, "content": rendered}]

    async def aload_messages(
        self,
        name: str,
        /,
        *,
        default_role: str = "user",
        **variables: Any,
    ) -> list[PromptMessageDict]:
        """Asynchronously load prompt and normalize output as a rendered list of message dictionaries."""
        rendered = await self.aload(name, **variables)
        if isinstance(rendered, list):
            return rendered
        return [{"role": default_role, "content": rendered}]


class Prompt:
    """
    Facade for loading and interpolating named and versioned prompts from `prompts/`.

    Supports both message-list Python prompts (`prompts/summarise.v2.py`) and
    Markdown/string templates (`prompts/greeting.md`).
    """

    @staticmethod
    def load(name: str, /, **variables: Any) -> PromptTemplateValue:
        """Load and interpolate a prompt by name (e.g. `Prompt.load('summarise.v2', text=...)`)."""
        return PromptLoader.get_instance().load(name, **variables)

    @staticmethod
    async def aload(name: str, /, **variables: Any) -> PromptTemplateValue:
        """Asynchronously load and interpolate a prompt by name."""
        return await PromptLoader.get_instance().aload(name, **variables)

    @staticmethod
    def load_messages(
        name: str,
        /,
        *,
        default_role: str = "user",
        **variables: Any,
    ) -> list[PromptMessageDict]:
        """Load and interpolate a prompt, always returning a list of message dicts."""
        return PromptLoader.get_instance().load_messages(
            name, default_role=default_role, **variables
        )

    @staticmethod
    async def aload_messages(
        name: str,
        /,
        *,
        default_role: str = "user",
        **variables: Any,
    ) -> list[PromptMessageDict]:
        """Asynchronously load and interpolate a prompt as a list of message dicts."""
        return await PromptLoader.get_instance().aload_messages(
            name, default_role=default_role, **variables
        )

    @staticmethod
    def get_template(name: str, /) -> PromptTemplateValue:
        """Load raw template string or message list without variable substitution."""
        return PromptLoader.get_instance().get_template(name)

    @staticmethod
    def render(template: str, /, **variables: Any) -> str:
        """Render a raw template string with `{{ variable }}` placeholders."""
        return PromptLoader.get_instance().render(template, **variables)

    @staticmethod
    def render_messages(
        messages_template: Sequence[Mapping[str, str]],
        /,
        **variables: Any,
    ) -> list[PromptMessageDict]:
        """Render a sequence of message dicts with `{{ variable }}` placeholders."""
        return PromptLoader.get_instance().render_messages(
            messages_template, **variables
        )


def load_prompt(name: str, /, **variables: Any) -> PromptTemplateValue:
    """
    Convenience function to load and interpolate a prompt using the default PromptLoader.
    """
    return PromptLoader.get_instance().load(name, **variables)


async def aload_prompt(name: str, /, **variables: Any) -> PromptTemplateValue:
    """
    Asynchronously load and interpolate a prompt using the default PromptLoader.
    """
    return await PromptLoader.get_instance().aload(name, **variables)
