from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from swiftpy.filesystem.exceptions import PathTraversalError
from swiftpy.filesystem.paths import PathNormalizer


def test_normalize_valid_paths() -> None:
    assert PathNormalizer.normalize("documents/report.pdf") == "documents/report.pdf"
    assert PathNormalizer.normalize("/avatars/user.png/") == "avatars/user.png"
    assert PathNormalizer.normalize("photos\\2026\\vacation.jpg") == "photos/2026/vacation.jpg"
    assert PathNormalizer.normalize("./docs/./file.txt") == "docs/file.txt"
    assert PathNormalizer.normalize("") == ""


def test_normalize_rejects_traversal() -> None:
    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("../secret.txt")

    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("docs/../../etc/passwd")

    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("..\\secret.txt")


def test_normalize_rejects_windows_drive_paths() -> None:
    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("C:\\Windows\\System32\\cmd.exe")

    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("C:/Windows/System32")

    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("D:/files/data.csv")

    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("sub/C:/file.txt")


def test_normalize_rejects_null_bytes() -> None:
    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("report.pdf\x00.exe")

    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("docs/%00/file.txt")


def test_normalize_rejects_encoded_traversal() -> None:
    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("%2e%2e/etc/passwd")

    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("docs/%2e%2e/secret.txt")


def test_normalize_rejects_double_encoded_traversal() -> None:
    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("%252e%252e/etc/passwd")

    with pytest.raises(PathTraversalError):
        PathNormalizer.normalize("docs/%252e%252e/secret.txt")


def test_resolve_safe_path() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir).resolve()
        safe = PathNormalizer.resolve_safe_path(root, "sub/file.txt")
        assert safe == root / "sub" / "file.txt"
        assert safe.is_relative_to(root)


def test_resolve_safe_path_rejects_symlink_escape() -> None:
    with tempfile.TemporaryDirectory() as outside_dir, tempfile.TemporaryDirectory() as root_dir:
        outside_path = Path(outside_dir).resolve() / "outside.txt"
        outside_path.write_text("secret")

        root = Path(root_dir).resolve()
        symlink_path = root / "symlink_dir"
        symlink_path.symlink_to(Path(outside_dir).resolve(), target_is_directory=True)

        with pytest.raises(PathTraversalError):
            PathNormalizer.resolve_safe_path(root, "symlink_dir/outside.txt")


def test_generate_name() -> None:
    name1 = PathNormalizer.generate_name("photo.jpg")
    name2 = PathNormalizer.generate_name("photo.jpg")

    assert name1 != name2
    assert name1.endswith(".jpg")
    assert name2.endswith(".jpg")

    prefixed = PathNormalizer.generate_name(prefix="avatar_")
    assert prefixed.startswith("avatar_")

    override_ext = PathNormalizer.generate_name("test.txt", extension="pdf")
    assert override_ext.endswith(".pdf")

    # Safe handling of None and empty name
    none_name = PathNormalizer.generate_name(None)
    assert "." not in none_name
    assert len(none_name) == 32

    empty_name = PathNormalizer.generate_name("")
    assert "." not in empty_name

    # Safe handling of null characters in filename
    null_name = PathNormalizer.generate_name("avatar\x00.png")
    assert null_name.endswith(".png")
    assert "\x00" not in null_name
