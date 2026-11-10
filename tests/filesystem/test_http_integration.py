from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.filesystem.manager import FilesystemManager
from swiftpy.filesystem.uploads.uploaded_file import UploadedFile
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient


@pytest.mark.asyncio
async def test_upload_png_signature_recognition() -> None:
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
        app = Application(auto_discover_providers=False)
        app.container.instance(Config, cfg)
        manager = FilesystemManager(cfg)
        app.container.instance(FilesystemManager, manager)

        @app.post("/upload-avatar")
        async def upload_avatar(request: Request) -> Response:
            avatar = await request.file("avatar")
            if avatar is None:
                return json_response({"error": "No avatar uploaded"}, status_code=400)
            path = await avatar.store("avatars")
            return json_response({"path": path})

        client = TestClient(app)

        boundary = "----TestBoundary12345"
        png_sig_content = b"\x89PNG\r\n\x1a\nsample_image_bytes"
        body = (
            f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="avatar"; filename="my_avatar.png"\r\n'
            + b"Content-Type: image/png\r\n\r\n"
            + png_sig_content
            + f"\r\n--{boundary}--\r\n".encode()
        )

        res = await client.post(
            "/upload-avatar",
            data=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        res.assert_ok()
        data = res.json()
        assert "path" in data
        stored_path = data["path"]
        assert stored_path.startswith("avatars/")
        dest_file = Path(storage_root) / stored_path
        assert dest_file.exists()
        assert dest_file.read_bytes() == png_sig_content


@pytest.mark.asyncio
async def test_upload_structurally_valid_png() -> None:
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
        app = Application(auto_discover_providers=False)
        app.container.instance(Config, cfg)
        manager = FilesystemManager(cfg)
        app.container.instance(FilesystemManager, manager)

        @app.post("/upload-avatar")
        async def upload_avatar(request: Request) -> Response:
            avatar = await request.file("avatar")
            if avatar is None:
                return json_response({"error": "No avatar uploaded"}, status_code=400)
            path = await avatar.store("avatars")
            return json_response({"path": path})

        client = TestClient(app)

        boundary = "----ValidPngBoundary"
        valid_png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
            b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        body = (
            f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="avatar"; filename="avatar.png"\r\n'
            + b"Content-Type: image/png\r\n\r\n"
            + valid_png_bytes
            + f"\r\n--{boundary}--\r\n".encode()
        )

        res = await client.post(
            "/upload-avatar",
            data=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        res.assert_ok()
        data = res.json()
        assert (Path(storage_root) / data["path"]).read_bytes() == valid_png_bytes


@pytest.mark.asyncio
async def test_route_parameter_injection_uploaded_file() -> None:
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
        app = Application(auto_discover_providers=False)
        app.container.instance(Config, cfg)
        manager = FilesystemManager(cfg)
        app.container.instance(FilesystemManager, manager)

        @app.post("/direct-upload")
        async def direct_upload(avatar: UploadedFile) -> Response:
            path = await avatar.store("avatars")
            return json_response({"filename": avatar.filename, "path": path})

        client = TestClient(app)

        boundary = "----DirectBoundary"
        jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00"
        body = (
            f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="avatar"; filename="profile.jpg"\r\n'
            + b"Content-Type: image/jpeg\r\n\r\n"
            + jpeg_payload
            + f"\r\n--{boundary}--\r\n".encode()
        )

        res = await client.post(
            "/direct-upload",
            data=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        res.assert_ok()
        data = res.json()
        assert data["filename"] == "profile.jpg"
        assert (Path(storage_root) / data["path"]).read_bytes() == jpeg_payload


@pytest.mark.asyncio
async def test_route_parameter_injection_multiple_files() -> None:
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
        app = Application(auto_discover_providers=False)
        app.container.instance(Config, cfg)
        manager = FilesystemManager(cfg)
        app.container.instance(FilesystemManager, manager)

        @app.post("/multi-upload")
        async def multi_upload(docs: list[UploadedFile]) -> Response:
            saved_paths: list[str] = []
            for doc in docs:
                p = await doc.store("documents")
                saved_paths.append(p)
            return json_response({"count": len(docs), "paths": saved_paths})

        client = TestClient(app)

        boundary = "----MultiBoundary"
        doc1_bytes = b"%PDF-1.4 doc1 sample content"
        doc2_bytes = b"%PDF-1.5 doc2 sample content"
        body = (
            f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="docs"; filename="doc1.pdf"\r\n'
            + b"Content-Type: application/pdf\r\n\r\n"
            + doc1_bytes
            + f"\r\n--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="docs"; filename="doc2.pdf"\r\n'
            + b"Content-Type: application/pdf\r\n\r\n"
            + doc2_bytes
            + f"\r\n--{boundary}--\r\n".encode()
        )

        res = await client.post(
            "/multi-upload",
            data=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        res.assert_ok()
        data = res.json()
        assert data["count"] == 2
        assert len(data["paths"]) == 2


@pytest.mark.asyncio
async def test_upload_malformed_multipart_handling() -> None:
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
        app = Application(auto_discover_providers=False)
        app.container.instance(Config, cfg)
        manager = FilesystemManager(cfg)
        app.container.instance(FilesystemManager, manager)

        @app.post("/upload")
        async def upload_handler(request: Request) -> Response:
            form = await request.form()
            return json_response({"files": len(form.all_files())})

        client = TestClient(app)

        # Missing boundary in Content-Type
        res = await client.post(
            "/upload",
            data=b"corrupted body without boundary",
            headers={"content-type": "multipart/form-data"},
        )
        assert res.status_code == 400

        # Malformed body: ends unexpectedly without boundary
        res2 = await client.post(
            "/upload",
            data=b"------NonExistentBoundary\r\n",
            headers={"content-type": "multipart/form-data; boundary=ExpectedBoundary"},
        )
        assert res2.status_code == 400
