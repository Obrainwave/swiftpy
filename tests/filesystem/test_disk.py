from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from swiftpy.core.config import Config
from swiftpy.core.container import Container, set_container
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.filesystem.disk import StorageDisk
from swiftpy.filesystem.drivers.local import LocalStorageDriver
from swiftpy.filesystem.events import FileDeleted, FileMoved, FileUploaded
from swiftpy.filesystem.exceptions import ReadOnlyFileSystemError
from swiftpy.filesystem.manager import FilesystemManager
from swiftpy.http.response import StreamingResponse


@pytest.fixture
def disk_setup() -> Generator[tuple[Path, StorageDisk], None, None]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name).resolve()
    driver = LocalStorageDriver(root=root)
    disk = StorageDisk(name="test_disk", driver=driver)
    try:
        yield root, disk
    finally:
        tmp.cleanup()


@pytest.mark.asyncio
async def test_read_only_disk_blocks_mutations() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = LocalStorageDriver(root=tmp)
        ro_disk = StorageDisk(name="ro", driver=driver, read_only=True)

        with pytest.raises(ReadOnlyFileSystemError):
            await ro_disk.put("file.txt", "data")

        with pytest.raises(ReadOnlyFileSystemError):
            await ro_disk.delete("file.txt")

        with pytest.raises(ReadOnlyFileSystemError):
            await ro_disk.move("a.txt", "b.txt")


@pytest.mark.asyncio
async def test_prefix_scoping() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = LocalStorageDriver(root=tmp)
        scoped_disk = StorageDisk(name="scoped", driver=driver, prefix="tenant_1")

        await scoped_disk.put("doc.txt", "hello tenant")

        # Verify underlying driver wrote to tenant_1/doc.txt
        assert await driver.exists("tenant_1/doc.txt")

        # Verify disk methods use stripped paths
        assert await scoped_disk.exists("doc.txt")
        assert await scoped_disk.read_text("doc.txt") == "hello tenant"

        files = await scoped_disk.files()
        assert files == ["doc.txt"]


@pytest.mark.asyncio
async def test_multi_disk_isolation() -> None:
    """
    Verify that multiple configured disks with identical relative paths
    remain completely isolated in their separate storage roots.
    """
    with tempfile.TemporaryDirectory() as priv_dir, tempfile.TemporaryDirectory() as pub_dir:
        cfg = Config({
            "filesystems": {
                "default": "private",
                "disks": {
                    "private": {
                        "driver": "local",
                        "root": priv_dir,
                    },
                    "public": {
                        "driver": "local",
                        "root": pub_dir,
                    },
                },
            }
        })
        manager = FilesystemManager(cfg)

        disk_priv = manager.disk("private")
        disk_pub = manager.disk("public")

        target_file = "reports/annual_2026.pdf"

        # Write different contents to identical relative paths on each disk
        await disk_priv.put(target_file, b"CONFIDENTIAL PRIVATE FINANCIALS")
        await disk_pub.put(target_file, b"PUBLIC PRESS RELEASE SUMMARY")

        # Verify isolation
        assert await disk_priv.read_text(target_file) == "CONFIDENTIAL PRIVATE FINANCIALS"
        assert await disk_pub.read_text(target_file) == "PUBLIC PRESS RELEASE SUMMARY"

        # Delete on private should not affect public
        await disk_priv.delete(target_file)
        assert not await disk_priv.exists(target_file)
        assert await disk_pub.exists(target_file)
        assert await disk_pub.read_text(target_file) == "PUBLIC PRESS RELEASE SUMMARY"


@pytest.mark.asyncio
async def test_storage_events_emitted(disk_setup: tuple[Path, StorageDisk]) -> None:
    _, disk = disk_setup

    container = Container()
    dispatcher = EventDispatcher()
    container.instance(EventDispatcher, dispatcher)
    set_container(container)

    events: list[object] = []

    async def listener(evt: object) -> None:
        events.append(evt)

    dispatcher.listen(FileUploaded.EVENT_NAME, listener)
    dispatcher.listen(FileDeleted.EVENT_NAME, listener)
    dispatcher.listen(FileMoved.EVENT_NAME, listener)

    await disk.put("event_test.txt", "test content")
    assert any(isinstance(e, FileUploaded) and e.path == "event_test.txt" for e in events)

    await disk.move("event_test.txt", "event_renamed.txt")
    assert any(
        isinstance(e, FileMoved)
        and e.source == "event_test.txt"
        and e.destination == "event_renamed.txt"
        for e in events
    )

    await disk.delete("event_renamed.txt")
    assert any(isinstance(e, FileDeleted) and e.path == "event_renamed.txt" for e in events)


@pytest.mark.asyncio
async def test_download_response(disk_setup: tuple[Path, StorageDisk]) -> None:
    _, disk = disk_setup
    await disk.put("manual.pdf", b"%PDF data")

    resp = await disk.download("manual.pdf")
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"] == 'attachment; filename="manual.pdf"'
    assert resp.headers["content-length"] == "9"
