from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from swiftpy.core.config import Config
from swiftpy.core.container import Container, set_container
from swiftpy.filesystem.manager import FilesystemManager
from swiftpy.filesystem.uploads.uploaded_file import UploadedFile


@pytest.mark.asyncio
async def test_uploaded_file_operations() -> None:
    spool = tempfile.SpooledTemporaryFile(max_size=1024)  # noqa: SIM115
    spool.write(b"uploaded file test content")
    spool.seek(0)

    uploaded = UploadedFile(
        file=spool,
        filename="report.pdf",
        content_type="application/pdf",
        size=26,
    )

    try:
        assert uploaded.filename == "report.pdf"
        assert uploaded.extension == "pdf"
        assert uploaded.content_type == "application/pdf"
        assert uploaded.size == 26

        content = await uploaded.read()
        assert content == b"uploaded file test content"

        # Test chunks
        chunks: list[bytes] = []
        async for chunk in uploaded.chunks(chunk_size=10):
            chunks.append(chunk)
        assert b"".join(chunks) == b"uploaded file test content"

        # Test save directly to local path
        with tempfile.TemporaryDirectory() as dest_dir:
            dest_file = Path(dest_dir) / "saved_report.pdf"
            await uploaded.save(dest_file)
            assert dest_file.exists()
            assert dest_file.read_bytes() == b"uploaded file test content"
    finally:
        await uploaded.close()


@pytest.mark.asyncio
async def test_uploaded_file_store() -> None:
    with tempfile.TemporaryDirectory() as storage_root:
        cfg = Config({
            "filesystems": {
                "default": "local",
                "disks": {
                    "local": {
                        "driver": "local",
                        "root": storage_root,
                    }
                },
            }
        })
        container = Container()
        container.instance(Config, cfg)
        manager = FilesystemManager(cfg)
        container.instance(FilesystemManager, manager)
        set_container(container)

        spool = tempfile.SpooledTemporaryFile(max_size=1024)  # noqa: SIM115
        spool.write(b"store avatar content")
        spool.seek(0)

        uploaded = UploadedFile(
            file=spool,
            filename="user_photo.png",
            content_type="image/png",
            size=20,
        )

        try:
            # Store with generated name
            path = await uploaded.store("avatars")
            assert path.startswith("avatars/")
            assert path.endswith(".png")
            assert await manager.disk().exists(path)
            assert await manager.disk().read(path) == b"store avatar content"

            # Store with custom name
            path2 = await uploaded.store_as("avatars", "custom.png")
            assert path2 == "avatars/custom.png"
            assert await manager.disk().exists("avatars/custom.png")
        finally:
            await uploaded.close()


@pytest.mark.asyncio
async def test_uploaded_file_spooled_temporary_file_rollover_to_disk() -> None:
    # Set a tiny max_size so the file definitely rolls over to disk
    spool = tempfile.SpooledTemporaryFile(max_size=32)  # noqa: SIM115
    data = b"X" * 256  # 256 bytes > 32 bytes threshold
    spool.write(data)
    spool.seek(0)

    # In Python's SpooledTemporaryFile, _rolled indicates whether it rolled over to a real disk file
    assert getattr(spool, "_rolled", False) is True

    upload = UploadedFile(
        file=spool,
        filename="large_log.txt",
        content_type="text/plain",
        size=len(data),
    )

    try:
        assert await upload.get_real_size() == 256
        content = await upload.read()
        assert content == data
        assert len(content) == 256

        # Verify chunks read correctly from disk-backed spool
        collected = b"".join([c async for c in upload.chunks(chunk_size=64)])
        assert collected == data
    finally:
        await upload.close()


@pytest.mark.asyncio
async def test_uploaded_file_real_size_vs_declared_size() -> None:
    spool = tempfile.SpooledTemporaryFile(max_size=1024)  # noqa: SIM115
    real_content = b"Super sensitive actual file bytes " * 100
    spool.write(real_content)
    spool.seek(0)

    # Maliciously declare size as 10 bytes
    upload = UploadedFile(
        file=spool,
        filename="test.bin",
        content_type="application/octet-stream",
        size=10,
    )

    try:
        assert upload.size == 10
        assert await upload.get_real_size() == len(real_content)
        assert len(await upload.read()) == len(real_content)
    finally:
        await upload.close()


@pytest.mark.asyncio
async def test_uploaded_file_store_never_overwrites_duplicate_names() -> None:
    with tempfile.TemporaryDirectory() as storage_root:
        cfg = Config({
            "filesystems": {
                "default": "local",
                "disks": {
                    "local": {
                        "driver": "local",
                        "root": storage_root,
                    }
                },
            }
        })
        container = Container()
        container.instance(Config, cfg)
        manager = FilesystemManager(cfg)
        container.instance(FilesystemManager, manager)
        set_container(container)

        spool1 = tempfile.SpooledTemporaryFile(max_size=1024)  # noqa: SIM115
        spool1.write(b"user 1 photo")
        spool1.seek(0)
        upload1 = UploadedFile(file=spool1, filename="avatar.png", content_type="image/png", size=12)

        spool2 = tempfile.SpooledTemporaryFile(max_size=1024)  # noqa: SIM115
        spool2.write(b"user 2 photo")
        spool2.seek(0)
        upload2 = UploadedFile(file=spool2, filename="avatar.png", content_type="image/png", size=12)

        try:
            path1 = await upload1.store("avatars")
            path2 = await upload2.store("avatars")

            assert path1 != path2
            assert await manager.disk().read(path1) == b"user 1 photo"
            assert await manager.disk().read(path2) == b"user 2 photo"
        finally:
            await upload1.close()
            await upload2.close()
