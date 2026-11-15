from __future__ import annotations

import asyncio
import hashlib
import io
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from swiftpy.core.application import Application
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.filesystem.disk import StorageDisk
from swiftpy.filesystem.drivers.local import LocalStorageDriver
from swiftpy.filesystem.drivers.s3 import S3StorageDriver
from swiftpy.filesystem.exceptions import (
    FileTooLargeError,
    FileValidationError,
    InvalidFileExtensionError,
    InvalidMimeTypeError,
    InvalidSignatureError,
    PathTraversalError,
)
from swiftpy.filesystem.uploads.handlers import MultipartParser
from swiftpy.filesystem.uploads.uploaded_file import UploadedFile
from swiftpy.filesystem.uploads.validators import FileValidator, verify_magic_bytes
from swiftpy.http.exceptions import ClientDisconnect
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient


def _make_upload(
    content: bytes,
    filename: str,
    content_type: str,
) -> UploadedFile:
    spool = tempfile.SpooledTemporaryFile(max_size=max(len(content) + 10, 1024))  # noqa: SIM115
    spool.write(content)
    spool.seek(0)
    return UploadedFile(
        file=spool,
        filename=filename,
        content_type=content_type,
        size=len(content),
    )


@pytest.mark.asyncio
async def test_fs1_file_validator_rejects_sequence_rules() -> None:
    bad_rules: Any = ["mimes:image/png"]
    with pytest.raises(TypeError):
        FileValidator(bad_rules)

    upload = _make_upload(b"MZ_evil_exe", "payload.exe", "application/x-msdownload")
    try:
        with pytest.raises(TypeError):
            await upload.validate(bad_rules)
    finally:
        await upload.close()


@pytest.mark.asyncio
async def test_fs2_extension_and_mime_errors_inherit_validation_and_return_422() -> None:
    assert issubclass(InvalidFileExtensionError, FileValidationError)
    assert issubclass(InvalidMimeTypeError, FileValidationError)

    app = Application(auto_discover_providers=False)

    @app.post("/upload-validated")
    async def upload_validated(request: Request) -> Response:
        file = await request.file("file")
        assert file is not None
        await file.validate({"extensions": ["png"], "mimes": ["image/png"]})
        return json_response({"ok": True})

    client = TestClient(app)
    boundary = "----FS2Boundary"
    body = (
        f"--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="file"; filename="script.sh"\r\n'
        + b"Content-Type: application/x-sh\r\n\r\n"
        + b"#!/bin/sh\r\n"
        + f"\r\n--{boundary}--\r\n".encode()
    )
    res = await client.post(
        "/upload-validated",
        data=body,
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_fs3_magic_bytes_fails_closed_on_policy_and_unmapped_types() -> None:
    # Unmapped MIME must fail closed instead of passing through
    assert verify_magic_bytes(b"\xaa\xbb\xcc\xdd\xee\xff", "application/x-custom") is False

    # Disguised executable inside .png with spoofed octet-stream Content-Type
    spoofed = _make_upload(b"MZ\x90\x00\x03\x00\x00\x00", "shell.png", "application/octet-stream")
    try:
        validator = FileValidator({"extensions": ["png"], "magic_bytes": True})
        with pytest.raises(FileValidationError):
            await validator.validate(spoofed)
    finally:
        await spoofed.close()

    # Unknown extension/MIME requested with magic_bytes=True fails closed
    unknown = _make_upload(b"plain text", "notes.txt", "text/plain")
    try:
        validator_unknown = FileValidator({"extensions": ["txt"], "magic_bytes": True})
        with pytest.raises(FileValidationError):
            await validator_unknown.validate(unknown)
    finally:
        await unknown.close()


@pytest.mark.asyncio
async def test_fs4_filename_backslash_sanitization_empty_filename_and_save_containment() -> None:
    boundary = b"----FS4Boundary"
    payload = (
        b"------FS4Boundary\r\n"
        b'Content-Disposition: form-data; name="empty_file"; filename=""\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
        b"\r\n"
        b"------FS4Boundary\r\n"
        b'Content-Disposition: form-data; name="script"; filename="..\\..\\evil.sh"\r\n'
        b"Content-Type: application/x-sh\r\n\r\n"
        b"echo hi\r\n"
        b"------FS4Boundary--\r\n"
    )

    async def stream_gen() -> AsyncIterator[bytes]:
        yield payload

    parser = MultipartParser(boundary)
    form_data = await parser.parse(stream_gen())
    try:
        # Empty filename="" must NOT become a text field in form_data.fields
        assert "empty_file" not in form_data.fields
        assert form_data.get("empty_file") is None
        assert form_data.get_file("empty_file") is None

        script_file = form_data.get_file("script")
        assert script_file is not None
        assert script_file.filename == "evil.sh"
        assert "\\" not in script_file.filename
        assert ".." not in script_file.filename
    finally:
        await form_data.close()

    # UploadedFile.save() traversal blocked
    upload = _make_upload(b"safe bytes", "avatar.png", "image/png")
    try:
        with tempfile.TemporaryDirectory() as target_dir:
            with pytest.raises(PathTraversalError):
                await upload.save(Path(target_dir) / "../escape.png")
            with pytest.raises(PathTraversalError):
                await upload.save("../escape.png", base_dir=target_dir)
    finally:
        await upload.close()


@pytest.mark.asyncio
async def test_fs5_s3_driver_headers_encoding_base_path_and_listing_errors() -> None:
    requests_seen: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.params.get("list-type") == "2":
            return httpx.Response(500, text="<Error><Code>InternalError</Code><Message>Boom</Message></Error>")
        return httpx.Response(200)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)
    driver = S3StorageDriver(
        bucket="app-bucket",
        access_key="AKIA_TEST",
        secret_key="SECRET_TEST",
        region="us-east-1",
        endpoint="https://storage.example.com/minio-proxy",
        use_path_style_endpoint=True,
        client=client,
    )
    try:
        # Mixed-case header dict must not raise KeyError in _sign_headers
        signed = driver._sign_headers(
            method="PUT",
            path="/minio-proxy/app-bucket/file.txt",
            query="",
            headers={"Content-Type": " text/plain "},
            payload_hash=hashlib.sha256(b"").hexdigest(),
        )
        assert signed["content-type"] == "text/plain"

        # _base_path must be honored in generated URLs and request paths
        public_url = await driver.url("folder/report #1.pdf")
        assert "/minio-proxy/app-bucket/folder/report%20%231.pdf" in public_url

        # temporary_url must percent-encode path consistently with SigV4 canonical URI
        presigned = await driver.temporary_url("folder/my file #1.pdf", expires_in=300)
        parsed = urlparse(presigned)
        assert parsed.path == "/minio-proxy/app-bucket/folder/my%20file%20%231.pdf"

        # files() and directories() must raise on HTTP 500 instead of returning []
        with pytest.raises(RuntimeError, match="500"):
            await driver.files()
        with pytest.raises(RuntimeError, match="500"):
            await driver.directories()
    finally:
        await driver.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_fs6_s3_streaming_upload_and_checksum() -> None:
    uploaded_payloads: dict[str, bytes] = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        if request.method == "PUT":
            uploaded_payloads[key] = request.content
            return httpx.Response(200)
        if request.method == "GET" and key in uploaded_payloads:
            return httpx.Response(200, content=uploaded_payloads[key])
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)
    driver = S3StorageDriver(
        bucket="stream-bucket",
        access_key="k",
        secret_key="s",
        client=client,
    )
    try:
        chunks = [b"chunk-one-", b"chunk-two-", b"chunk-three"]
        written = await driver.write_stream("large.bin", io.BytesIO(b"".join(chunks)), chunk_size=5)
        expected_bytes = b"".join(chunks)
        assert written == len(expected_bytes)
        assert uploaded_payloads["large.bin"] == expected_bytes

        expected_sha = hashlib.sha256(expected_bytes).hexdigest()
        assert await driver.checksum("large.bin") == expected_sha
    finally:
        await driver.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_fs7_local_signed_url_validation_encoding_ttl_and_key() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # Reject string "None" fallback too
        bad_driver = LocalStorageDriver(root=tmp, signing_key="None")
        with pytest.raises(ConfigurationError):
            await bad_driver.temporary_url("file.txt", expires_in=60)

        driver = LocalStorageDriver(root=tmp, signing_key="real-signing-secret")
        # Reject non-positive TTLs
        with pytest.raises(ValueError):
            await driver.temporary_url("file.txt", expires_in=0)
        with pytest.raises(ValueError):
            await driver.temporary_url("file.txt", expires_in=-5)

        # Percent-encode spaces and verify round-trip
        signed = await driver.temporary_url("reports/my report #1.pdf", expires_in=120)
        parsed = urlparse(signed)
        assert "/storage/reports/my%20report%20%231.pdf" in parsed.path
        assert driver.verify_temporary_url(signed) == "reports/my report #1.pdf"

        # Tampering raises InvalidSignatureError
        tampered = signed.replace("signature=", "signature=deadbeef")
        with pytest.raises(InvalidSignatureError):
            driver.verify_temporary_url(tampered)

        # Expiration check raises InvalidSignatureError
        qs = parse_qs(parsed.query)
        exp_ts = int(qs["expires"][0])
        with pytest.raises(InvalidSignatureError):
            driver.verify_temporary_url(signed, now=float(exp_ts + 10))


@pytest.mark.asyncio
async def test_fs8_local_symlinks_excluded_and_cancel_cleans_tmp_files() -> None:
    with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as outside_dir:
        outside_path = Path(outside_dir).resolve()
        (outside_path / "secret.txt").write_text("top secret")
        outside_sub = outside_path / "ext_dir"
        outside_sub.mkdir()
        (outside_sub / "leak.txt").write_text("leak")

        root = Path(root_dir).resolve()
        driver = LocalStorageDriver(root=root, signing_key="secret")
        await driver.write("inside.txt", b"safe")
        (root / "sub").mkdir()
        await driver.write("sub/ok.txt", b"ok")

        # Create external symlinks inside storage root
        (root / "evil_file_link.txt").symlink_to(outside_path / "secret.txt")
        (root / "evil_dir_link").symlink_to(outside_sub, target_is_directory=True)
        (root / "sub" / "nested_link.txt").symlink_to(outside_path / "secret.txt")

        listed_files = await driver.files(recursive=True)
        assert listed_files == ["inside.txt", "sub/ok.txt"]

        listed_dirs = await driver.directories(recursive=True)
        assert listed_dirs == ["sub"]

        # Task cancellation during write_stream must clean up .tmp-* files
        started = asyncio.Event()

        async def slow_stream() -> AsyncIterator[bytes]:
            yield b"partial"
            started.set()
            await asyncio.sleep(10)

        task = asyncio.create_task(driver.write_stream("cancelled.bin", slow_stream()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        leftovers = [p for p in root.rglob(".tmp-*")]
        assert leftovers == []


@pytest.mark.asyncio
async def test_fs9_multipart_prefix_disconnect_field_limit_request_config_and_download_header() -> None:
    # 1. Mid-body boundary prefix (e.g. --ABC_suffix inside body when boundary=ABC)
    boundary = b"ABC"
    file_body = b"hello\r\n--ABC_suffix\r\nworld"
    multipart_payload = (
        b"--ABC\r\n"
        b'Content-Disposition: form-data; name="doc"; filename="data.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
        + file_body
        + b"\r\n--ABC--\r\n"
    )

    async def stream_chunks() -> AsyncIterator[bytes]:
        # Deliver in tiny chunks to exercise cross-chunk matching
        for i in range(0, len(multipart_payload), 5):
            yield multipart_payload[i : i + 5]

    parser = MultipartParser(boundary)
    form_data = await parser.parse(stream_chunks())
    try:
        doc = form_data.get_file("doc")
        assert doc is not None
        assert await doc.read() == file_body
    finally:
        await form_data.close()

    # 2. http.disconnect raises ClientDisconnect instead of MalformedMultipartError
    async def disconnect_receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await parser.parse(disconnect_receive)

    # 3. Text field size capped cleanly (max_field_size)
    big_field_payload = (
        b"--ABC\r\n"
        b'Content-Disposition: form-data; name="bio"\r\n\r\n'
        + (b"A" * 200)
        + b"\r\n--ABC--\r\n"
    )

    async def big_field_stream() -> AsyncIterator[bytes]:
        yield big_field_payload

    strict_parser = MultipartParser(boundary, memory_limit=50, max_field_size=50)
    with pytest.raises(FileTooLargeError):
        await strict_parser.parse(big_field_stream())

    # 4. Request.form() honors config["filesystems.uploads"] limits
    cfg = Config(
        {
            "filesystems": {
                "default": "local",
                "uploads": {
                    "max_file_size": 30,
                    "max_request_size": 500,
                    "memory_limit": 64,
                },
            }
        }
    )
    app = Application(auto_discover_providers=False, config=cfg)

    @app.post("/cfg-upload")
    async def cfg_upload(request: Request) -> Response:
        f = await request.file("file")
        return json_response({"size": f.size if f else 0})

    client = TestClient(app)
    res = await client.post(
        "/cfg-upload",
        data=(
            b"--CFGBND\r\n"
            b'Content-Disposition: form-data; name="file"; filename="big.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
            + (b"X" * 40)
            + b"\r\n--CFGBND--\r\n"
        ),
        headers={"content-type": "multipart/form-data; boundary=CFGBND"},
    )
    assert res.status_code == 413

    # 5. StorageDisk.download() escapes quotes and strips CR/LF in Content-Disposition
    with tempfile.TemporaryDirectory() as tmp:
        disk = StorageDisk(name="local", driver=LocalStorageDriver(root=tmp))
        await disk.put("file.txt", b"hello")
        resp = await disk.download("file.txt", name='bad"\r\nX-Evil: 1.txt')
        cd = resp.headers["content-disposition"]
        assert "\r" not in cd
        assert "\n" not in cd
        assert "X-Evil" not in cd or '\\"' in cd
        assert '\\"' in cd
