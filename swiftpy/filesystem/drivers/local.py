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
from typing import BinaryIO
from urllib.parse import parse_qs, quote, unquote, urlsplit

from swiftpy.core.config import ConfigurationError
from swiftpy.filesystem.drivers.base import StorageDriver
from swiftpy.filesystem.exceptions import FileNotFoundError, InvalidSignatureError
from swiftpy.filesystem.metadata import FileMetadata
from swiftpy.filesystem.paths import PathNormalizer
from swiftpy.filesystem.visibility import (
    DEFAULT_DIR_PERMISSIONS,
    DEFAULT_FILE_PERMISSIONS,
    Visibility,
)


def _local_write_atomic(
    target: Path,
    data: bytes,
    file_mode: int,
    dir_mode: int,
) -> int:
    target.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    tmp_target = target.parent / f".tmp-{uuid.uuid4().hex}"
    try:
        with open(tmp_target, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_target, file_mode)
        os.replace(tmp_target, target)
    except BaseException:
        if tmp_target.exists():
            try:
                tmp_target.unlink()
            except OSError:
                pass
        raise
    return len(data)


def _local_open_write_tmp(target: Path, dir_mode: int) -> tuple[Path, BinaryIO]:
    target.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    tmp_target = target.parent / f".tmp-{uuid.uuid4().hex}"
    handle = open(tmp_target, "wb")  # noqa: SIM115
    return tmp_target, handle


def _local_finalize_write_tmp(
    tmp_target: Path,
    target: Path,
    handle: BinaryIO,
    file_mode: int,
) -> None:
    handle.flush()
    os.fsync(handle.fileno())
    handle.close()
    os.chmod(tmp_target, file_mode)
    os.replace(tmp_target, target)


def _local_cleanup_write_tmp(tmp_target: Path, handle: BinaryIO) -> None:
    try:
        handle.close()
    except OSError:
        pass
    if tmp_target.exists():
        try:
            tmp_target.unlink()
        except OSError:
            pass


def _local_read_bytes(target: Path, path_label: str) -> bytes:
    if not target.is_file():
        raise FileNotFoundError(f"File not found at '{path_label}'.")
    with open(target, "rb") as f:
        return f.read()


def _local_open_read_stream(target: Path, path_label: str) -> BinaryIO:
    if not target.is_file():
        raise FileNotFoundError(f"File not found at '{path_label}'.")
    return open(target, "rb")


def _local_delete_file(target: Path) -> bool:
    if target.is_file():
        target.unlink()
        return True
    return False


def _local_copy_file(src: Path, dst: Path, src_label: str) -> bool:
    if not src.is_file():
        raise FileNotFoundError(f"Source file not found at '{src_label}'.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _local_move_file(src: Path, dst: Path, src_label: str) -> bool:
    if not src.is_file():
        raise FileNotFoundError(f"Source file not found at '{src_label}'.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    return True


def _local_file_size(target: Path, path_label: str) -> int:
    if not target.is_file():
        raise FileNotFoundError(f"File not found at '{path_label}'.")
    return target.stat().st_size


def _local_last_modified(target: Path, path_label: str) -> datetime:
    if not target.is_file():
        raise FileNotFoundError(f"File not found at '{path_label}'.")
    return datetime.fromtimestamp(target.stat().st_mtime, tz=UTC)


def _local_checksum(target: Path, path_label: str, algorithm: str) -> str:
    if not target.is_file():
        raise FileNotFoundError(f"File not found at '{path_label}'.")
    hasher = hashlib.new(algorithm)
    with open(target, "rb") as f:
        while chunk := f.read(64 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_contained_file(root: Path, candidate: Path) -> bool:
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved.is_relative_to(root) and candidate.is_file()


def _is_contained_dir(root: Path, candidate: Path) -> bool:
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved != root and resolved.is_relative_to(root) and candidate.is_dir()


def _local_list_files(root: Path, target: Path, recursive: bool) -> list[str]:
    if not target.is_dir():
        return []
    result: list[str] = []
    if recursive:
        for walk_root, dirnames, filenames in os.walk(target, followlinks=False):
            walk_path = Path(walk_root)
            dirnames[:] = sorted(
                d for d in dirnames if not (walk_path / d).is_symlink()
            )
            for filename in filenames:
                full_p = walk_path / filename
                if _is_contained_file(root, full_p):
                    result.append(full_p.relative_to(root).as_posix())
    else:
        with os.scandir(target) as it:
            for entry in it:
                entry_path = Path(entry.path)
                if _is_contained_file(root, entry_path):
                    result.append(entry_path.relative_to(root).as_posix())
    result.sort()
    return result


def _local_list_directories(root: Path, target: Path, recursive: bool) -> list[str]:
    if not target.is_dir():
        return []
    result: list[str] = []
    if recursive:
        for walk_root, dirnames, _ in os.walk(target, followlinks=False):
            walk_path = Path(walk_root)
            kept: list[str] = []
            for dirname in dirnames:
                full_p = walk_path / dirname
                if not full_p.is_symlink() and _is_contained_dir(root, full_p):
                    kept.append(dirname)
                    result.append(full_p.relative_to(root).as_posix())
            dirnames[:] = kept
    else:
        with os.scandir(target) as it:
            for entry in it:
                entry_path = Path(entry.path)
                if not entry.is_symlink() and _is_contained_dir(root, entry_path):
                    result.append(entry_path.relative_to(root).as_posix())
    result.sort()
    return result


def _local_make_dir(target: Path, dir_mode: int) -> bool:
    target.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    return True


def _local_delete_dir(target: Path) -> bool:
    if target.is_dir():
        shutil.rmtree(target)
        return True
    return False


def _local_get_visibility(target: Path, path_label: str) -> Visibility:
    if not target.is_file():
        raise FileNotFoundError(f"File not found at '{path_label}'.")
    mode = target.stat().st_mode
    if mode & stat.S_IROTH:
        return Visibility.PUBLIC
    return Visibility.PRIVATE


def _local_set_visibility(target: Path, path_label: str, mode: int) -> bool:
    if not target.is_file():
        raise FileNotFoundError(f"File not found at '{path_label}'.")
    os.chmod(target, mode)
    return True


def _local_stat_metadata(target: Path, clean: str, path_label: str) -> FileMetadata:
    if not target.is_file():
        raise FileNotFoundError(f"File not found at '{path_label}'.")
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
        signing_key: str | None = None,
        permissions: dict[str, dict[str, int]] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.url_prefix = url_prefix
        self.default_visibility = Visibility(default_visibility)
        self.signing_key: str | None = signing_key

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
        return await asyncio.to_thread(_local_write_atomic, target, data, file_mode, dir_mode)

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

        tmp_target, handle = await asyncio.to_thread(_local_open_write_tmp, target, dir_mode)
        total_written = 0
        committed = False

        try:
            if isinstance(stream, AsyncIterator):
                async for chunk in stream:
                    if chunk:
                        total_written += len(chunk)
                        await asyncio.to_thread(handle.write, chunk)
            else:
                while True:
                    chunk = await asyncio.to_thread(stream.read, chunk_size)
                    if not chunk:
                        break
                    total_written += len(chunk)
                    await asyncio.to_thread(handle.write, chunk)

            await asyncio.to_thread(
                _local_finalize_write_tmp,
                tmp_target,
                target,
                handle,
                file_mode,
            )
            committed = True
            return total_written
        finally:
            if not committed:
                try:
                    await asyncio.to_thread(_local_cleanup_write_tmp, tmp_target, handle)
                except asyncio.CancelledError:
                    _local_cleanup_write_tmp(tmp_target, handle)

    async def read(self, path: str) -> bytes:
        target = self._resolve(path)
        return await asyncio.to_thread(_local_read_bytes, target, path)

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        data = await self.read(path)
        return data.decode(encoding)

    async def read_stream(
        self,
        path: str,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        target = self._resolve(path)
        handle = await asyncio.to_thread(_local_open_read_stream, target, path)
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def exists(self, path: str) -> bool:
        target = self._resolve(path)
        return await asyncio.to_thread(target.is_file)

    async def missing(self, path: str) -> bool:
        return not await self.exists(path)

    async def delete(self, path: str) -> bool:
        target = self._resolve(path)
        return await asyncio.to_thread(_local_delete_file, target)

    async def copy(self, source: str, destination: str) -> bool:
        src = self._resolve(source)
        dst = self._resolve(destination)
        return await asyncio.to_thread(_local_copy_file, src, dst, source)

    async def move(self, source: str, destination: str) -> bool:
        src = self._resolve(source)
        dst = self._resolve(destination)
        return await asyncio.to_thread(_local_move_file, src, dst, source)

    async def size(self, path: str) -> int:
        target = self._resolve(path)
        return await asyncio.to_thread(_local_file_size, target, path)

    async def mime_type(self, path: str) -> str:
        clean = PathNormalizer.normalize(path)
        mime, _ = mimetypes.guess_type(clean)
        return mime or "application/octet-stream"

    async def last_modified(self, path: str) -> datetime:
        target = self._resolve(path)
        return await asyncio.to_thread(_local_last_modified, target, path)

    async def checksum(self, path: str, algorithm: str = "sha256") -> str:
        target = self._resolve(path)
        return await asyncio.to_thread(_local_checksum, target, path, algorithm)

    async def files(self, directory: str = "", recursive: bool = False) -> list[str]:
        target = self._resolve(directory)
        return await asyncio.to_thread(_local_list_files, self.root, target, recursive)

    async def directories(self, directory: str = "", recursive: bool = False) -> list[str]:
        target = self._resolve(directory)
        return await asyncio.to_thread(_local_list_directories, self.root, target, recursive)

    async def make_directory(self, directory: str) -> bool:
        target = self._resolve(directory)
        dir_mode = self._get_dir_mode(None)
        return await asyncio.to_thread(_local_make_dir, target, dir_mode)

    async def delete_directory(self, directory: str) -> bool:
        target = self._resolve(directory)
        return await asyncio.to_thread(_local_delete_dir, target)

    def _require_signing_key(self) -> str:
        key = self.signing_key
        if (
            not key
            or not isinstance(key, str)
            or not key.strip()
            or key.strip().lower() == "none"
        ):
            raise ConfigurationError(
                "Cannot generate or verify temporary URL without a non-empty signing_key "
                "(configure FILESYSTEM_SIGNING_KEY or APP_KEY)."
            )
        return key

    async def url(self, path: str) -> str:
        clean = PathNormalizer.normalize(path)
        encoded = quote(clean, safe="/-_.~")
        prefix = self.url_prefix.rstrip("/")
        if not encoded:
            return prefix or "/"
        return f"{prefix}/{encoded}"

    async def temporary_url(
        self,
        path: str,
        expires_in: int | timedelta,
    ) -> str:
        if isinstance(expires_in, bool):
            raise TypeError("expires_in must be an integer or timedelta, not bool.")
        duration = (
            int(expires_in)
            if isinstance(expires_in, (int, float))
            else int(expires_in.total_seconds())
        )
        if duration <= 0:
            raise ValueError("expires_in must be greater than 0 seconds.")
        signing_key = self._require_signing_key()
        clean = PathNormalizer.normalize(path)
        encoded = quote(clean, safe="/-_.~")
        expires = int(time.time() + duration)
        payload = f"GET\n{encoded}\n{expires}"
        signature = hmac.new(
            signing_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        base_url = await self.url(clean)
        return f"{base_url}?expires={expires}&signature={signature}"

    def verify_temporary_url(
        self,
        signed_url: str,
        *,
        method: str = "GET",
        now: float | None = None,
    ) -> str:
        """
        Verify a signed temporary URL's HMAC signature and expiration timestamp.
        Returns the normalized relative storage path on success or raises InvalidSignatureError.
        """
        signing_key = self._require_signing_key()
        parsed = urlsplit(signed_url)
        prefix_path = urlsplit(self.url_prefix).path.rstrip("/")
        raw_path = parsed.path

        if prefix_path:
            expected_prefix = f"{prefix_path}/"
            if not raw_path.startswith(expected_prefix):
                raise InvalidSignatureError("Temporary URL prefix does not match disk configuration.")
            encoded_rel = raw_path[len(expected_prefix) :]
        else:
            encoded_rel = raw_path.lstrip("/")

        if not encoded_rel:
            raise InvalidSignatureError("Temporary URL path is empty.")

        query_params = parse_qs(parsed.query, keep_blank_values=True)
        expires_vals = query_params.get("expires")
        sig_vals = query_params.get("signature")
        if (
            not expires_vals
            or not sig_vals
            or len(expires_vals) != 1
            or len(sig_vals) != 1
            or not expires_vals[0]
            or not sig_vals[0]
        ):
            raise InvalidSignatureError("Temporary URL is missing required signature parameters.")

        expires_str = expires_vals[0]
        provided_sig = sig_vals[0]
        try:
            expires_ts = int(expires_str)
        except ValueError as exc:
            raise InvalidSignatureError("Temporary URL expiration timestamp is invalid.") from exc

        payload = f"{method.upper()}\n{encoded_rel}\n{expires_str}"
        expected_sig = hmac.new(
            signing_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_sig, provided_sig):
            raise InvalidSignatureError("Temporary URL signature is invalid.")

        current_ts = time.time() if now is None else now
        if expires_ts < current_ts:
            raise InvalidSignatureError("Temporary URL has expired.")

        return PathNormalizer.normalize(unquote(encoded_rel))

    async def get_visibility(self, path: str) -> Visibility:
        target = self._resolve(path)
        return await asyncio.to_thread(_local_get_visibility, target, path)

    async def set_visibility(self, path: str, visibility: Visibility | str) -> bool:
        target = self._resolve(path)
        vis = Visibility(visibility)
        mode = self._file_permissions.get(vis, 0o600)
        return await asyncio.to_thread(_local_set_visibility, target, path, mode)

    async def metadata(self, path: str) -> FileMetadata:
        target = self._resolve(path)
        clean = PathNormalizer.normalize(path)
        return await asyncio.to_thread(_local_stat_metadata, target, clean, path)
