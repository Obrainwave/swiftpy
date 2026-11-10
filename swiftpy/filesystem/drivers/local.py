from __future__ import annotations

import asyncio
import hashlib
import hmac
import mimetypes
import os
import shutil
import stat
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from swiftpy.filesystem.drivers.base import StorageDriver
from swiftpy.filesystem.exceptions import FileNotFoundError
from swiftpy.filesystem.metadata import FileMetadata
from swiftpy.filesystem.paths import PathNormalizer
from swiftpy.filesystem.visibility import (
    DEFAULT_DIR_PERMISSIONS,
    DEFAULT_FILE_PERMISSIONS,
    Visibility,
)


class LocalStorageDriver(StorageDriver):
    """
    Local filesystem storage driver.
    Offloads all synchronous filesystem operations to worker threads via asyncio.to_thread
    to guarantee zero event-loop blocking.
    """

    def __init__(
        self,
        root: str | Path,
        url_prefix: str = "/storage",
        default_visibility: Visibility | str = Visibility.PRIVATE,
        signing_key: str = "swiftpy-storage-secret",
        permissions: dict[str, dict[str, int]] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.url_prefix = url_prefix
        self.default_visibility = Visibility(default_visibility)
        self.signing_key = signing_key

        perm = permissions or {}
        file_perms = perm.get("file", {})
        dir_perms = perm.get("dir", {})

        self._file_permissions: dict[str, int] = {
            Visibility.PUBLIC: file_perms.get("public", DEFAULT_FILE_PERMISSIONS[Visibility.PUBLIC]),
            Visibility.PRIVATE: file_perms.get("private", DEFAULT_FILE_PERMISSIONS[Visibility.PRIVATE]),
        }
        self._dir_permissions: dict[str, int] = {
            Visibility.PUBLIC: dir_perms.get("public", DEFAULT_DIR_PERMISSIONS[Visibility.PUBLIC]),
            Visibility.PRIVATE: dir_perms.get("private", DEFAULT_DIR_PERMISSIONS[Visibility.PRIVATE]),
        }

    def _resolve(self, path: str) -> Path:
        return PathNormalizer.resolve_safe_path(self.root, path)

    def _get_file_mode(self, visibility: Visibility | str | None) -> int:
        vis = Visibility(visibility) if visibility is not None else self.default_visibility
        return self._file_permissions.get(vis, 0o600)

    def _get_dir_mode(self, visibility: Visibility | str | None) -> int:
        vis = Visibility(visibility) if visibility is not None else self.default_visibility
        return self._dir_permissions.get(vis, 0o700)

    async def write(
        self,
        path: str,
        content: bytes | str,
        visibility: Visibility | str | None = None,
    ) -> int:
        target = self._resolve(path)
        data = content.encode("utf-8") if isinstance(content, str) else content
        file_mode = self._get_file_mode(visibility)
        dir_mode = self._get_dir_mode(visibility)

        def _do_write() -> int:
            target.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
            tmp_target = target.parent / f".tmp-{uuid.uuid4().hex}"
            with open(tmp_target, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_target, file_mode)
            os.replace(tmp_target, target)
            return len(data)

        return await asyncio.to_thread(_do_write)

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes] | BinaryIO,
        chunk_size: int = 64 * 1024,
        visibility: Visibility | str | None = None,
    ) -> int:
        target = self._resolve(path)
        file_mode = self._get_file_mode(visibility)
        dir_mode = self._get_dir_mode(visibility)

        def _prepare_tmp() -> tuple[Path, Any]:
            target.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
            tmp_target = target.parent / f".tmp-{uuid.uuid4().hex}"
            f = open(tmp_target, "wb")  # noqa: SIM115
            return tmp_target, f

        tmp_target, f = await asyncio.to_thread(_prepare_tmp)
        total_written = 0

        try:
            if isinstance(stream, AsyncIterator):
                async for chunk in stream:
                    if chunk:
                        total_written += len(chunk)
                        await asyncio.to_thread(f.write, chunk)
            else:
                # Sync stream / BinaryIO
                while True:
                    chunk = await asyncio.to_thread(stream.read, chunk_size)
                    if not chunk:
                        break
                    total_written += len(chunk)
                    await asyncio.to_thread(f.write, chunk)

            def _finalize() -> None:
                f.flush()
                os.fsync(f.fileno())
                f.close()
                os.chmod(tmp_target, file_mode)
                os.replace(tmp_target, target)

            await asyncio.to_thread(_finalize)
            return total_written
        except Exception:
            def _cleanup() -> None:
                try:
                    f.close()
                except OSError:
                    pass
                if tmp_target.exists():
                    try:
                        tmp_target.unlink()
                    except OSError:
                        pass

            await asyncio.to_thread(_cleanup)
            raise

    async def read(self, path: str) -> bytes:
        target = self._resolve(path)

        def _do_read() -> bytes:
            if not target.is_file():
                raise FileNotFoundError(f"File not found at '{path}'.")
            with open(target, "rb") as f:
                return f.read()

        return await asyncio.to_thread(_do_read)

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        data = await self.read(path)
        return data.decode(encoding)

    async def read_stream(
        self,
        path: str,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        target = self._resolve(path)

        def _open() -> Any:
            if not target.is_file():
                raise FileNotFoundError(f"File not found at '{path}'.")
            return open(target, "rb")

        f = await asyncio.to_thread(_open)
        try:
            while True:
                chunk = await asyncio.to_thread(f.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(f.close)

    async def exists(self, path: str) -> bool:
        target = self._resolve(path)
        return await asyncio.to_thread(target.is_file)

    async def missing(self, path: str) -> bool:
        return not await self.exists(path)

    async def delete(self, path: str) -> bool:
        target = self._resolve(path)

        def _do_delete() -> bool:
            if target.is_file():
                target.unlink()
                return True
            return False

        return await asyncio.to_thread(_do_delete)

    async def copy(self, source: str, destination: str) -> bool:
        src = self._resolve(source)
        dst = self._resolve(destination)

        def _do_copy() -> bool:
            if not src.is_file():
                raise FileNotFoundError(f"Source file not found at '{source}'.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return True

        return await asyncio.to_thread(_do_copy)

    async def move(self, source: str, destination: str) -> bool:
        src = self._resolve(source)
        dst = self._resolve(destination)

        def _do_move() -> bool:
            if not src.is_file():
                raise FileNotFoundError(f"Source file not found at '{source}'.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            return True

        return await asyncio.to_thread(_do_move)

    async def size(self, path: str) -> int:
        target = self._resolve(path)

        def _do_size() -> int:
            if not target.is_file():
                raise FileNotFoundError(f"File not found at '{path}'.")
            return target.stat().st_size

        return await asyncio.to_thread(_do_size)

    async def mime_type(self, path: str) -> str:
        clean = PathNormalizer.normalize(path)
        mime, _ = mimetypes.guess_type(clean)
        return mime or "application/octet-stream"

    async def last_modified(self, path: str) -> datetime:
        target = self._resolve(path)

        def _do_time() -> datetime:
            if not target.is_file():
                raise FileNotFoundError(f"File not found at '{path}'.")
            return datetime.fromtimestamp(target.stat().st_mtime, tz=UTC)

        return await asyncio.to_thread(_do_time)

    async def checksum(self, path: str, algorithm: str = "sha256") -> str:
        target = self._resolve(path)

        def _do_checksum() -> str:
            if not target.is_file():
                raise FileNotFoundError(f"File not found at '{path}'.")
            hasher = hashlib.new(algorithm)
            with open(target, "rb") as f:
                while chunk := f.read(64 * 1024):
                    hasher.update(chunk)
            return hasher.hexdigest()

        return await asyncio.to_thread(_do_checksum)

    async def files(self, directory: str = "", recursive: bool = False) -> list[str]:
        target = self._resolve(directory)

        def _do_files() -> list[str]:
            if not target.is_dir():
                return []
            result: list[str] = []
            if recursive:
                for root, _, filenames in os.walk(target):
                    for filename in filenames:
                        full_p = Path(root) / filename
                        result.append(
                            full_p.relative_to(self.root).as_posix()
                        )
            else:
                with os.scandir(target) as it:
                    for entry in it:
                        if entry.is_file():
                            result.append(
                                Path(entry.path).relative_to(self.root).as_posix()
                            )
            result.sort()
            return result

        return await asyncio.to_thread(_do_files)

    async def directories(self, directory: str = "", recursive: bool = False) -> list[str]:
        target = self._resolve(directory)

        def _do_directories() -> list[str]:
            if not target.is_dir():
                return []
            result: list[str] = []
            if recursive:
                for root, dirnames, _ in os.walk(target):
                    for dirname in dirnames:
                        full_p = Path(root) / dirname
                        result.append(
                            full_p.relative_to(self.root).as_posix()
                        )
            else:
                with os.scandir(target) as it:
                    for entry in it:
                        if entry.is_dir():
                            result.append(
                                Path(entry.path).relative_to(self.root).as_posix()
                            )
            result.sort()
            return result

        return await asyncio.to_thread(_do_directories)

    async def make_directory(self, directory: str) -> bool:
        target = self._resolve(directory)
        dir_mode = self._get_dir_mode(None)

        def _do_mkdir() -> bool:
            target.mkdir(parents=True, exist_ok=True, mode=dir_mode)
            return True

        return await asyncio.to_thread(_do_mkdir)

    async def delete_directory(self, directory: str) -> bool:
        target = self._resolve(directory)

        def _do_rmdir() -> bool:
            if target.is_dir():
                shutil.rmtree(target)
                return True
            return False

        return await asyncio.to_thread(_do_rmdir)

    async def url(self, path: str) -> str:
        clean = PathNormalizer.normalize(path)
        prefix = self.url_prefix.rstrip("/")
        return f"{prefix}/{clean}"

    async def temporary_url(
        self,
        path: str,
        expires_in: int | timedelta,
    ) -> str:
        clean = PathNormalizer.normalize(path)
        duration = (
            int(expires_in)
            if isinstance(expires_in, (int, float))
            else int(expires_in.total_seconds())
        )
        expires = int(time.time() + duration)
        payload = f"GET\n{clean}\n{expires}"
        signature = hmac.new(
            self.signing_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        base_url = await self.url(clean)
        return f"{base_url}?expires={expires}&signature={signature}"

    async def get_visibility(self, path: str) -> Visibility:
        target = self._resolve(path)

        def _do_vis() -> Visibility:
            if not target.is_file():
                raise FileNotFoundError(f"File not found at '{path}'.")
            mode = target.stat().st_mode
            if mode & stat.S_IROTH:
                return Visibility.PUBLIC
            return Visibility.PRIVATE

        return await asyncio.to_thread(_do_vis)

    async def set_visibility(self, path: str, visibility: Visibility | str) -> bool:
        target = self._resolve(path)
        vis = Visibility(visibility)
        mode = self._file_permissions.get(vis, 0o600)

        def _do_chmod() -> bool:
            if not target.is_file():
                raise FileNotFoundError(f"File not found at '{path}'.")
            os.chmod(target, mode)
            return True

        return await asyncio.to_thread(_do_chmod)

    async def metadata(self, path: str) -> FileMetadata:
        target = self._resolve(path)
        clean = PathNormalizer.normalize(path)

        def _do_meta() -> FileMetadata:
            if not target.is_file():
                raise FileNotFoundError(f"File not found at '{path}'.")
            st = target.stat()
            size = st.st_size
            mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC)
            vis = Visibility.PUBLIC if (st.st_mode & stat.S_IROTH) else Visibility.PRIVATE
            mime, _ = mimetypes.guess_type(clean)
            return FileMetadata(
                path=clean,
                size=size,
                mime_type=mime or "application/octet-stream",
                last_modified=mtime,
                visibility=vis,
            )

        return await asyncio.to_thread(_do_meta)
