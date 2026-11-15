from __future__ import annotations

import tempfile
from collections.abc import Generator

import pytest

from swiftpy.core.config import Config
from swiftpy.core.container import Container, set_container
from swiftpy.filesystem import Storage
from swiftpy.filesystem.disk import StorageDisk
from swiftpy.filesystem.manager import FilesystemManager
from swiftpy.helpers.helper import storage


@pytest.fixture(autouse=True)
def setup_storage_env() -> Generator[None, None, None]:
    with tempfile.TemporaryDirectory() as storage_root:
        cfg = Config({
            "filesystems": {
                "default": "local",
                "disks": {
                    "local": {
                        "driver": "local",
                        "root": storage_root,
                        "url": "/storage",
                    },
                    "backup": {
                        "driver": "local",
                        "root": f"{storage_root}/backup",
                    },
                },
            }
        })
        container = Container()
        container.instance(Config, cfg)
        manager = FilesystemManager(cfg)
        container.instance(FilesystemManager, manager)
        set_container(container)
        yield


async def test_storage_facade_basic_crud() -> None:
    await Storage.put("notes.txt", "meeting notes")
    assert await Storage.exists("notes.txt")
    assert not await Storage.missing("notes.txt")

    text = await Storage.get_text("notes.txt")
    assert text == "meeting notes"

    data = await Storage.get("notes.txt")
    assert data == b"meeting notes"

    read_alias = await Storage.read_text("notes.txt")
    assert read_alias == "meeting notes"

    assert await Storage.size("notes.txt") == len("meeting notes")
    assert await Storage.mime_type("notes.txt") == "text/plain"

    files = await Storage.files()
    assert "notes.txt" in files

    await Storage.copy("notes.txt", "notes_backup.txt")
    assert await Storage.exists("notes_backup.txt")

    await Storage.move("notes_backup.txt", "notes_archived.txt")
    assert not await Storage.exists("notes_backup.txt")
    assert await Storage.exists("notes_archived.txt")

    await Storage.delete("notes.txt")
    assert not await Storage.exists("notes.txt")


async def test_storage_named_disk_and_helper() -> None:
    # Test via Storage.disk("backup")
    backup_disk = Storage.disk("backup")
    assert isinstance(backup_disk, StorageDisk)
    await backup_disk.put("db_dump.sql", "SELECT 1;")
    assert await backup_disk.exists("db_dump.sql")

    # Test via helper storage()
    helper_manager = storage()
    assert isinstance(helper_manager, FilesystemManager)

    helper_disk = storage("backup")
    assert isinstance(helper_disk, StorageDisk)
    assert await helper_disk.read_text("db_dump.sql") == "SELECT 1;"
