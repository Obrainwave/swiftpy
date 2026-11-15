from __future__ import annotations

import pytest

from swiftpy.cache import Cache, CacheDriver, MemoryDriver, RedisDriver
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.helpers.helper import cache


async def test_cache_manager_default_resolution() -> None:
    cfg = Config(
        {
            "cache": {
                "default": "memory",
                "prefix": "test_mgr:",
                "stores": {
                    "memory": {"driver": "memory", "prefix": "test_mgr_mem:"},
                    "redis": {"driver": "redis", "prefix": "test_mgr_red:"},
                },
            }
        }
    )
    cache_mgr = Cache(config=cfg)
    assert cache_mgr.default_driver_name == "memory"

    # Default driver should be MemoryDriver
    driver = cache_mgr.driver()
    assert isinstance(driver, MemoryDriver)
    assert driver.prefix == "test_mgr_mem:"

    # Proxy methods forward to default driver
    await cache_mgr.set("key1", "val1")
    assert await cache_mgr.get("key1") == "val1"
    assert await cache_mgr.has("key1") is True
    assert await cache_mgr.forget("key1") is True
    assert await cache_mgr.has("key1") is False


async def test_cache_manager_multi_store_switching() -> None:
    cfg = Config(
        {
            "cache": {
                "default": "memory",
                "stores": {
                    "memory": {"driver": "memory", "prefix": "mem_store:"},
                    "redis": {"driver": "redis", "prefix": "red_store:"},
                },
            }
        }
    )
    cache_mgr = Cache(config=cfg)

    mem_driver = cache_mgr.driver("memory")
    assert isinstance(mem_driver, MemoryDriver)

    red_driver = cache_mgr.driver("redis")
    assert isinstance(red_driver, RedisDriver)

    # store() is an alias for driver()
    assert cache_mgr.store("memory") is mem_driver
    assert cache_mgr.store("redis") is red_driver

    await cache_mgr.close()


async def test_cache_manager_custom_driver_extension() -> None:
    class DummyCustomDriver(CacheDriver):
        def __init__(self) -> None:
            super().__init__()
            self.data: dict[str, str] = {}

        async def get(self, key: str, default: object = None) -> object:
            return self.data.get(key, default)

        async def set(self, key: str, value: object, ttl: float | None = None) -> bool:
            self.data[key] = str(value)
            return True

        async def forget(self, key: str) -> bool:
            return self.data.pop(key, None) is not None

        async def has(self, key: str) -> bool:
            return key in self.data

        async def flush(self) -> bool:
            self.data.clear()
            return True

    cache_mgr = Cache()
    cache_mgr.extend("dummy", lambda: DummyCustomDriver())

    driver = cache_mgr.driver("dummy")
    assert isinstance(driver, DummyCustomDriver)

    await driver.set("hello", "world")
    assert await driver.get("hello") == "world"


async def test_cache_manager_unsupported_driver_error() -> None:
    cache_mgr = Cache()
    with pytest.raises(ValueError, match="Unsupported cache driver: 'unknown'"):
        cache_mgr.driver("unknown")


async def test_cache_di_container_and_helper() -> None:
    app = Application()
    resolved_cache = app.container.resolve(Cache)
    assert isinstance(resolved_cache, Cache)

    # Verify singleton identity
    second_resolved = app.container.resolve(Cache)
    assert resolved_cache is second_resolved

    # Verify helper cache() returns the container-resolved instance
    helper_cache = cache()
    assert isinstance(helper_cache, Cache)

    mem_driver = cache("memory")
    assert isinstance(mem_driver, MemoryDriver)

    await resolved_cache.close()
