from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MigrationProtocol(Protocol):
    """Protocol satisfied by any migration class or instance providing up/down methods."""

    async def up(self) -> None: ...

    async def down(self) -> None: ...


class Migration:
    """Base class for all SwiftPY migrations."""

    async def up(self) -> None:
        """Run the forward migration changes."""

    async def down(self) -> None:
        """Reverse the migration changes."""
