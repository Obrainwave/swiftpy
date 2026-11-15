from __future__ import annotations

import pytest

from swiftpy.core.config import Config
from swiftpy.filesystem.disk import StorageDisk
from swiftpy.filesystem.drivers.local import LocalStorageDriver
from swiftpy.filesystem.exceptions import DiskNotFoundError, DriverNotSupportedError
from swiftpy.filesystem.manager import FilesystemManager


def test_manager_resolves_default_disk() -> None:
    cfg = Config({
        "filesystems": {
            "default": "local",
            "disks": {
                "local": {
                    "driver": "local",
                    "root": "storage/test/local",
                }
            },
        }
    })
    manager = FilesystemManager(cfg)
    assert manager.default_disk_name == "local"

    disk = manager.disk()
    assert isinstance(disk, StorageDisk)
    assert disk.name == "local"


def test_manager_raises_on_unconfigured_disk() -> None:
    cfg = Config({
        "filesystems": {
            "default": "local",
            "disks": {},
        }
    })
    manager = FilesystemManager(cfg)
    with pytest.raises(DiskNotFoundError):
        manager.disk("nonexistent")


def test_manager_raises_on_unsupported_driver() -> None:
    cfg = Config({
        "filesystems": {
            "disks": {
                "ftp_disk": {
                    "driver": "unsupported_ftp",
                }
            }
        }
    })
    manager = FilesystemManager(cfg)
    with pytest.raises(DriverNotSupportedError):
        manager.disk("ftp_disk")


def test_manager_extend_custom_driver() -> None:
    cfg = Config({
        "filesystems": {
            "disks": {
                "custom_disk": {
                    "driver": "custom",
                }
            }
        }
    })
    manager = FilesystemManager(cfg)

    manager.extend("custom", lambda conf: LocalStorageDriver(root="storage/custom"))
    disk = manager.disk("custom_disk")
    assert isinstance(disk, StorageDisk)
    assert disk.name == "custom_disk"


def test_manager_build_on_demand_disk() -> None:
    manager = FilesystemManager(Config({}))
    disk = manager.build({
        "driver": "local",
        "root": "storage/on_demand",
        "read_only": True,
        "prefix": "tenant_42",
    })
    assert isinstance(disk, StorageDisk)
    assert disk.read_only is True
    assert disk.prefix == "tenant_42"

