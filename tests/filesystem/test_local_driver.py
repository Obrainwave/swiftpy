from __future__ import annotations

import io
import tempfile
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import pytest

from swiftpy.filesystem.drivers.local import LocalStorageDriver
from swiftpy.filesystem.exceptions import FileNotFoundError
from swiftpy.filesystem.visibility import Visibility


@pytest.fixture
def temp_disk() -> Generator[tuple[Path, LocalStorageDriver], None, None]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name).resolve()
    driver = LocalStorageDriver(
        root=root,
        url_prefix="/storage",
        signing_key="test-key",
    )
    try:
        yield root, driver
    finally:
        tmp.cleanup()


@pytest.mark.asyncio
async def test_write_and_read(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk

    bytes_written = await driver.write("test.txt", b"Hello, SwiftPY!")
    assert bytes_written == 15

    assert await driver.exists("test.txt")
    assert not await driver.missing("test.txt")

    content = await driver.read("test.txt")
    assert content == b"Hello, SwiftPY!"

    text = await driver.read_text("test.txt")
    assert text == "Hello, SwiftPY!"


@pytest.mark.asyncio
async def test_read_nonexistent_raises(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    with pytest.raises(FileNotFoundError):
        await driver.read("missing.txt")


@pytest.mark.asyncio
async def test_write_and_read_stream(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk

    async def sample_stream() -> AsyncIterator[bytes]:
        yield b"chunk 1 - "
        yield b"chunk 2"

    written = await driver.write_stream("streamed.txt", sample_stream())
    assert written == 17

    chunks: list[bytes] = []
    async for chunk in driver.read_stream("streamed.txt", chunk_size=5):
        chunks.append(chunk)

    assert b"".join(chunks) == b"chunk 1 - chunk 2"


@pytest.mark.asyncio
async def test_write_stream_from_binary_io(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    buf = io.BytesIO(b"binary data from io")
    written = await driver.write_stream("buf.dat", buf)
    assert written == 19
    assert await driver.read("buf.dat") == b"binary data from io"


@pytest.mark.asyncio
async def test_empty_file_operations(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    written = await driver.write("empty.txt", b"")
    assert written == 0
    assert await driver.exists("empty.txt")
    assert await driver.size("empty.txt") == 0
    assert await driver.read("empty.txt") == b""
    assert await driver.read_text("empty.txt") == ""


@pytest.mark.asyncio
async def test_large_file_streaming(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    # Stream 10 MB in 1 MB chunks
    chunk_1mb = b"L" * (1024 * 1024)
    total_chunks = 10

    async def large_stream() -> AsyncIterator[bytes]:
        for _ in range(total_chunks):
            yield chunk_1mb

    written = await driver.write_stream("large_10mb.dat", large_stream())
    assert written == 10 * 1024 * 1024
    assert await driver.size("large_10mb.dat") == 10 * 1024 * 1024

    # Read back in stream
    read_total = 0
    async for chunk in driver.read_stream("large_10mb.dat", chunk_size=512 * 1024):
        read_total += len(chunk)
    assert read_total == 10 * 1024 * 1024


@pytest.mark.asyncio
async def test_atomic_replacement(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    root, driver = temp_disk
    await driver.write("versioned.txt", "version 1 content")
    assert await driver.read_text("versioned.txt") == "version 1 content"

    await driver.write("versioned.txt", "version 2 content")
    assert await driver.read_text("versioned.txt") == "version 2 content"

    # Verify no temporary files left in root directory
    tmp_files = [f for f in root.iterdir() if f.name.startswith(".tmp-")]
    assert len(tmp_files) == 0


@pytest.mark.asyncio
async def test_failed_write_stream_rollback_preserves_destination(
    temp_disk: tuple[Path, LocalStorageDriver],
) -> None:
    root, driver = temp_disk
    await driver.write("critical.txt", "pristine original content")

    async def broken_stream() -> AsyncIterator[bytes]:
        yield b"beginning chunk"
        raise RuntimeError("Simulated connection dropped midway")

    with pytest.raises(RuntimeError):
        await driver.write_stream("critical.txt", broken_stream())

    # Verify destination file remains intact and uncorrupted
    assert await driver.read_text("critical.txt") == "pristine original content"

    # Verify no dangling temporary files
    tmp_files = [f for f in root.iterdir() if f.name.startswith(".tmp-")]
    assert len(tmp_files) == 0


@pytest.mark.asyncio
async def test_delete(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    await driver.write("to_delete.txt", "bye")
    assert await driver.exists("to_delete.txt")

    deleted = await driver.delete("to_delete.txt")
    assert deleted is True
    assert not await driver.exists("to_delete.txt")

    # Deleting absent file returns False
    assert await driver.delete("to_delete.txt") is False


@pytest.mark.asyncio
async def test_copy_and_move(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    await driver.write("orig.txt", "content")

    await driver.copy("orig.txt", "copied.txt")
    assert await driver.exists("orig.txt")
    assert await driver.read_text("copied.txt") == "content"

    await driver.move("copied.txt", "moved.txt")
    assert not await driver.exists("copied.txt")
    assert await driver.read_text("moved.txt") == "content"


@pytest.mark.asyncio
async def test_size_and_checksum(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    await driver.write("hash_test.txt", b"abc")

    assert await driver.size("hash_test.txt") == 3
    # sha256 of "abc" is ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert await driver.checksum("hash_test.txt") == expected


@pytest.mark.asyncio
async def test_directories_and_files_listing(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    await driver.write("a.txt", "1")
    await driver.write("sub/b.txt", "2")
    await driver.write("sub/deeper/c.txt", "3")

    flat_files = await driver.files()
    assert "a.txt" in flat_files
    assert "sub/b.txt" not in flat_files

    recursive_files = await driver.files(recursive=True)
    assert recursive_files == ["a.txt", "sub/b.txt", "sub/deeper/c.txt"]

    flat_dirs = await driver.directories()
    assert flat_dirs == ["sub"]

    recursive_dirs = await driver.directories(recursive=True)
    assert recursive_dirs == ["sub", "sub/deeper"]


@pytest.mark.asyncio
async def test_directory_creation_and_deletion(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    await driver.make_directory("custom/dir")
    await driver.write("custom/dir/file.txt", "data")

    assert await driver.exists("custom/dir/file.txt")
    await driver.delete_directory("custom")
    assert not await driver.exists("custom/dir/file.txt")


@pytest.mark.asyncio
async def test_visibility_and_permissions(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    await driver.write("private.txt", "secret", visibility=Visibility.PRIVATE)
    assert await driver.get_visibility("private.txt") == Visibility.PRIVATE

    await driver.set_visibility("private.txt", Visibility.PUBLIC)
    assert await driver.get_visibility("private.txt") == Visibility.PUBLIC


@pytest.mark.asyncio
async def test_urls(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    url = await driver.url("avatars/user.png")
    assert url == "/storage/avatars/user.png"

    signed = await driver.temporary_url("docs/report.pdf", expires_in=300)
    assert "/storage/docs/report.pdf?expires=" in signed
    assert "&signature=" in signed


@pytest.mark.asyncio
async def test_metadata(temp_disk: tuple[Path, LocalStorageDriver]) -> None:
    _, driver = temp_disk
    await driver.write("report.pdf", b"%PDF-1.4 sample")

    meta = await driver.metadata("report.pdf")
    assert meta.path == "report.pdf"
    assert meta.size == 15
    assert meta.mime_type == "application/pdf"
    assert meta.last_modified is not None
