from __future__ import annotations

import argparse
import operator
import sys
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swiftpy.console.commands.seed import SeedCommand
from swiftpy.database.seeder import Seeder


def _noop_recorder(_ev: str) -> None:
    return None


_record_tx_event: Callable[[str], None] = _noop_recorder
_record_fail_event: Callable[[str], None] = _noop_recorder

# Shared execution log for sequence testing
shared_execution_log: list[str] = []


class FirstChildSeeder(Seeder):
    async def run(self) -> None:
        shared_execution_log.append("first")


class SecondChildSeeder(Seeder):
    async def run(self) -> None:
        shared_execution_log.append("second")


class ParentSeeder(Seeder):
    async def run(self) -> None:
        await self.call(FirstChildSeeder, SecondChildSeeder)


class AbstractChildSeeder(Seeder):
    # run() not implemented
    pass


class CustomDottedSeeder(Seeder):
    did_run: ClassVar[bool] = False

    async def run(self) -> None:
        CustomDottedSeeder.did_run = True


@pytest.mark.asyncio
async def test_seeder_call_chains_seeders_in_sequence() -> None:
    shared_execution_log.clear()

    parent = ParentSeeder()
    await parent.run()

    # Proves FirstChildSeeder executed strictly before SecondChildSeeder
    assert shared_execution_log == ["first", "second"]


@pytest.mark.asyncio
async def test_seeder_call_rejects_abstract_and_invalid_types() -> None:
    parent = ParentSeeder()

    # 1. Abstract seeder subclass
    with pytest.raises(TypeError, match="Must be a non-abstract subclass of Seeder"):
        await operator.methodcaller("call", AbstractChildSeeder)(parent)

    # 2. Unrelated class
    class NotASeeder:
        pass

    with pytest.raises(TypeError, match="Must be a non-abstract subclass of Seeder"):
        await operator.methodcaller("call", NotASeeder)(parent)


def test_seed_command_finds_seeder_class_in_directory_and_dotted(tmp_path: Path) -> None:
    seeder_code = """from __future__ import annotations
from swiftpy.database.seeder import Seeder

class CustomUserSeeder(Seeder):
    async def run(self) -> None:
        pass

class AbstractSampleSeeder(Seeder):
    pass
"""
    seeder_file = tmp_path / "custom_user_seeder.py"
    seeder_file.write_text(seeder_code, encoding="utf-8")

    # 1. Look up by PascalCase class name matching snake_case file
    cls1 = SeedCommand._find_seeder_class("CustomUserSeeder", tmp_path)
    assert issubclass(cls1, Seeder)
    assert cls1.__name__ == "CustomUserSeeder"

    # 2. Look up by direct class name
    cls2 = SeedCommand._find_seeder_class("CustomUserSeeder", tmp_path)
    assert cls2 is cls1

    # 3. Look up by dotted Python import path
    cls3 = SeedCommand._find_seeder_class(
        "tests.database.test_seeder.CustomDottedSeeder", tmp_path
    )
    assert cls3.__name__ == "CustomDottedSeeder"
    assert issubclass(cls3, Seeder)

    # 4. Reject abstract seeder class
    with pytest.raises(SystemExit, match="Cannot run abstract seeder class"):
        SeedCommand._find_seeder_class("AbstractSampleSeeder", tmp_path)

    # 5. Missing seeder raises SystemExit
    with pytest.raises(SystemExit, match="could not be found"):
        SeedCommand._find_seeder_class("NonExistentSeeder", tmp_path)


@pytest.mark.asyncio
async def test_seed_command_execution_inside_transaction_and_commits(tmp_path: Path) -> None:
    tx_events: list[str] = []

    seeder_code = f"""from __future__ import annotations
import sys

from swiftpy.database.seeder import Seeder

class MockDatabaseSeeder(Seeder):
    async def run(self) -> None:
        # Record execution inside transaction
        sys.modules['{__name__}']._record_tx_event('seeder_run')
"""
    seeder_file = tmp_path / "mock_database_seeder.py"
    seeder_file.write_text(seeder_code, encoding="utf-8")

    def _record(ev: str) -> None:
        tx_events.append(ev)

    # Attach recorder to module for seeder code to access
    global _record_tx_event
    _record_tx_event = _record

    args = argparse.Namespace(
        app="main:app",
        class_name="MockDatabaseSeeder",
        no_transaction=False,
        path=str(tmp_path),
    )

    with (
        patch("swiftpy.console.commands.seed.load_application") as mock_load,
        patch("swiftpy.console.commands.seed.db.transaction") as mock_tx,
    ):
        mock_app = MagicMock()
        mock_pool = MagicMock()
        mock_pool.init = AsyncMock()
        mock_pool.close = AsyncMock()
        mock_app.container.resolve.return_value = mock_pool
        mock_load.return_value = mock_app

        class DummyTx:
            async def __aenter__(self) -> Self:
                tx_events.append("tx_enter")
                return self

            async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
                if exc_type is not None:
                    tx_events.append("tx_rollback")
                else:
                    tx_events.append("tx_commit")

        mock_tx.return_value = DummyTx()

        await SeedCommand._execute_seed(args)

        # Proves seeder executed strictly AFTER transaction entry and BEFORE commit
        assert tx_events == ["tx_enter", "seeder_run", "tx_commit"]
        mock_pool.init.assert_awaited_once()
        mock_pool.close.assert_awaited_once()
        mock_tx.assert_called_once()


@pytest.mark.asyncio
async def test_seed_command_execution_failure_triggers_rollback(tmp_path: Path) -> None:
    tx_events: list[str] = []

    seeder_code = f"""from __future__ import annotations
import sys

from swiftpy.database.seeder import Seeder

class FailingDatabaseSeeder(Seeder):
    async def run(self) -> None:
        sys.modules['{__name__}']._record_fail_event('seeder_fail')
        raise RuntimeError("Database write error during seeding")
"""
    seeder_file = tmp_path / "failing_database_seeder.py"
    seeder_file.write_text(seeder_code, encoding="utf-8")

    def _record(ev: str) -> None:
        tx_events.append(ev)

    global _record_fail_event
    _record_fail_event = _record

    args = argparse.Namespace(
        app="main:app",
        class_name="FailingDatabaseSeeder",
        no_transaction=False,
        path=str(tmp_path),
    )

    with (
        patch("swiftpy.console.commands.seed.load_application") as mock_load,
        patch("swiftpy.console.commands.seed.db.transaction") as mock_tx,
    ):
        mock_app = MagicMock()
        mock_pool = MagicMock()
        mock_pool.init = AsyncMock()
        mock_pool.close = AsyncMock()
        mock_app.container.resolve.return_value = mock_pool
        mock_load.return_value = mock_app

        class DummyTx:
            async def __aenter__(self) -> Self:
                tx_events.append("tx_enter")
                return self

            async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
                if exc_type is not None:
                    tx_events.append("tx_rollback")
                else:
                    tx_events.append("tx_commit")

        mock_tx.return_value = DummyTx()

        with pytest.raises(RuntimeError, match="Database write error during seeding"):
            await SeedCommand._execute_seed(args)

        # Proves transaction rolled back upon seeder failure
        assert tx_events == ["tx_enter", "seeder_fail", "tx_rollback"]
        mock_pool.init.assert_awaited_once()
        mock_pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_seed_command_execution_no_transaction(tmp_path: Path) -> None:
    seeder_code = """from __future__ import annotations
from swiftpy.database.seeder import Seeder

class DirectSeeder(Seeder):
    did_run = False
    async def run(self) -> None:
        DirectSeeder.did_run = True
"""
    seeder_file = tmp_path / "direct_seeder.py"
    seeder_file.write_text(seeder_code, encoding="utf-8")

    args = argparse.Namespace(
        app="main:app",
        class_name="DirectSeeder",
        no_transaction=True,
        path=str(tmp_path),
    )

    with (
        patch("swiftpy.console.commands.seed.load_application") as mock_load,
        patch("swiftpy.console.commands.seed.db.transaction") as mock_tx,
    ):
        mock_app = MagicMock()
        mock_pool = MagicMock()
        mock_pool.init = AsyncMock()
        mock_pool.close = AsyncMock()
        mock_app.container.resolve.return_value = mock_pool
        mock_load.return_value = mock_app

        await SeedCommand._execute_seed(args)

        mock_pool.init.assert_awaited_once()
        mock_pool.close.assert_awaited_once()
        mock_tx.assert_not_called()

        # Observable side effect: the seeder body actually executed
        seeder_module = sys.modules["_seeders.direct_seeder"]
        assert seeder_module.DirectSeeder.did_run is True
