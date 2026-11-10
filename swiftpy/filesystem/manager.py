from __future__ import annotations

from collections.abc import Callable
from typing import Any

from swiftpy.core.config import Config
from swiftpy.core.container import get_container
from swiftpy.filesystem.disk import StorageDisk
from swiftpy.filesystem.drivers.base import StorageDriver
from swiftpy.filesystem.drivers.local import LocalStorageDriver
from swiftpy.filesystem.drivers.s3 import S3StorageDriver
from swiftpy.filesystem.exceptions import DiskNotFoundError, DriverNotSupportedError


class FilesystemManager:
    """
    Registry and factory for configured StorageDisk instances.
    Integrates with SwiftPY's configuration repository and DI container.
    """

    _instance: FilesystemManager | None = None

    def __init__(self, config: Config | None = None) -> None:
        if config is None:
            try:
                container = get_container()
                if container.has(Config):
                    self.config = container.resolve(Config)
                else:
                    self.config = Config.get_instance()
            except RuntimeError:
                self.config = Config.get_instance()
        else:
            self.config = config

        self._disks: dict[str, StorageDisk] = {}
        self._custom_creators: dict[str, Callable[[dict[str, Any]], StorageDriver]] = {}

    @classmethod
    def get_instance(cls) -> FilesystemManager:
        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
        except RuntimeError:
            pass

        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def default_disk_name(self) -> str:
        return str(self.config.get("filesystems.default", "local"))

    def extend(
        self,
        driver_name: str,
        factory: Callable[[dict[str, Any]], StorageDriver],
    ) -> None:
        """Register a custom storage driver factory."""
        self._custom_creators[driver_name] = factory

    def disk(self, name: str | None = None) -> StorageDisk:
        """Resolve a storage disk instance by name, or return the default disk."""
        disk_name = name or self.default_disk_name

        if disk_name in self._disks:
            return self._disks[disk_name]

        disk_instance = self._resolve_disk(disk_name)
        self._disks[disk_name] = disk_instance
        return disk_instance

    def build(self, disk_config: dict[str, Any], name: str = "on_demand") -> StorageDisk:
        """Build an unmanaged on-demand storage disk from a configuration dict."""
        driver = self._create_driver(disk_config)
        read_only = bool(disk_config.get("read_only", False))
        prefix = str(disk_config.get("prefix", ""))
        return StorageDisk(name=name, driver=driver, read_only=read_only, prefix=prefix)

    def _resolve_disk(self, name: str) -> StorageDisk:
        disks = self.config.get("filesystems.disks", {})
        if not isinstance(disks, dict) or name not in disks:
            raise DiskNotFoundError(f"Storage disk '{name}' is not configured.")

        disk_config = disks[name]
        if not isinstance(disk_config, dict):
            raise DiskNotFoundError(f"Configuration for disk '{name}' must be a dictionary.")

        driver = self._create_driver(disk_config)
        read_only = bool(disk_config.get("read_only", False))
        prefix = str(disk_config.get("prefix", ""))

        return StorageDisk(name=name, driver=driver, read_only=read_only, prefix=prefix)

    def _create_driver(self, disk_config: dict[str, Any]) -> StorageDriver:
        driver_type = str(disk_config.get("driver", "local")).lower()

        if driver_type in self._custom_creators:
            return self._custom_creators[driver_type](disk_config)

        if driver_type == "local":
            root = str(disk_config.get("root", "storage/app/private"))
            url_prefix = str(disk_config.get("url", "/storage"))
            visibility = str(disk_config.get("visibility", "private"))
            signing_key = str(
                self.config.get(
                    "filesystems.signing_key",
                    self.config.get("app.key", "swiftpy-storage-secret"),
                )
            )
            permissions = disk_config.get("permissions")
            return LocalStorageDriver(
                root=root,
                url_prefix=url_prefix,
                default_visibility=visibility,
                signing_key=signing_key,
                permissions=permissions if isinstance(permissions, dict) else None,
            )

        if driver_type == "s3":
            return S3StorageDriver.from_config(disk_config)

        raise DriverNotSupportedError(f"Unsupported storage driver: '{driver_type}'")

    async def close(self) -> None:
        """Close and clean up all instantiated storage drivers."""
        for disk_instance in self._disks.values():
            await disk_instance.driver.close()
        self._disks.clear()
