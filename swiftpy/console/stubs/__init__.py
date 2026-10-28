from __future__ import annotations

from pathlib import Path

STUBS_DIR = Path(__file__).resolve().parent


def get_stub(name: str) -> str:
    """
    Load a stub template string by name from the stubs directory.
    Supports both dotted and underscore conventions (e.g. 'migration.create' or 'migration_create').
    """
    cleaned = name.removesuffix(".stub")
    candidates = (
        STUBS_DIR / f"{cleaned}.stub",
        STUBS_DIR / f"{cleaned.replace('_', '.')}.stub",
        STUBS_DIR / f"{cleaned.replace('.', '_')}.stub",
    )
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")

    raise FileNotFoundError(f"Stub file not found for name '{name}' in {STUBS_DIR}")


__all__ = ["STUBS_DIR", "get_stub"]
