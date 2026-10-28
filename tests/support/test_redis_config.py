from __future__ import annotations

from urllib.parse import unquote, urlsplit

from swiftpy.core.config import Config
from swiftpy.support.redis_config import build_redis_url, resolve_redis_params


class TestRedisConfig:
    def test_default_redis_url(self) -> None:
        cfg = Config({})
        url = build_redis_url(cfg, section="redis")
        assert url == "redis://127.0.0.1:6379/0"

    def test_explicit_url_in_config(self) -> None:
        cfg = Config({"redis": {"url": "redis://custom-redis.internal:6380/5"}})
        assert build_redis_url(cfg, section="redis") == "redis://custom-redis.internal:6380/5"

    def test_explicit_url_in_section(self) -> None:
        cfg = Config({"queue": {"url": "redis://queue-redis:6381/2"}})
        assert build_redis_url(cfg, section="queue") == "redis://queue-redis:6381/2"

    def test_special_characters_in_password(self) -> None:
        special_pw = "p@ss:w/ord#1?val=2"
        cfg = Config({
            "redis": {
                "host": "localhost",
                "port": 6379,
                "password": special_pw,
                "database": 1,
            }
        })
        url = build_redis_url(cfg, section="redis")
        # Should not have raw @ or # inside the authority part
        parsed = urlsplit(url)
        assert parsed.hostname == "localhost"
        assert parsed.port == 6379
        assert parsed.path == "/1"
        assert parsed.password is not None
        assert unquote(parsed.password) == special_pw

    def test_username_and_password(self) -> None:
        cfg = Config({
            "redis": {
                "host": "localhost",
                "port": 6379,
                "username": "admin@user",
                "password": "secret:password",
                "database": 0,
            }
        })
        url = build_redis_url(cfg, section="redis")
        parsed = urlsplit(url)
        assert parsed.username is not None and unquote(parsed.username) == "admin@user"
        assert parsed.password is not None and unquote(parsed.password) == "secret:password"

    def test_ssl_scheme(self) -> None:
        cfg = Config({
            "redis": {
                "host": "secure-redis.cloud",
                "port": 6380,
                "ssl": True,
                "database": 0,
            }
        })
        url = build_redis_url(cfg, section="redis")
        assert url.startswith("rediss://")
        assert "secure-redis.cloud:6380/0" in url

    def test_section_resolution_cache(self) -> None:
        cfg = Config({
            "cache": {
                "stores": {
                    "redis": {
                        "host": "cache-host",
                        "port": 6379,
                        "database": 2,
                    }
                }
            }
        })
        url = build_redis_url(cfg, section="cache")
        assert url == "redis://cache-host:6379/2"

    def test_section_resolution_queue(self) -> None:
        cfg = Config({
            "queue": {
                "connections": {
                    "redis": {
                        "host": "queue-host",
                        "port": 6380,
                        "database": 4,
                    }
                }
            }
        })
        url = build_redis_url(cfg, section="queue")
        assert url == "redis://queue-host:6380/4"

    def test_section_resolution_events(self) -> None:
        cfg = Config({
            "events": {
                "redis": {
                    "host": "events-host",
                    "port": 6379,
                    "database": 6,
                }
            }
        })
        url = build_redis_url(cfg, section="events")
        assert url == "redis://events-host:6379/6"

    def test_explicit_keyword_overrides(self) -> None:
        cfg = Config({
            "redis": {
                "host": "configured-host",
                "port": 6379,
                "database": 0,
            }
        })
        url = build_redis_url(cfg, section="redis", host="override-host", database=9)
        assert url == "redis://override-host:6379/9"

    def test_resolve_redis_params(self) -> None:
        cfg = Config({
            "redis": {
                "host": "127.0.0.1",
                "port": 6379,
                "password": "pass",
                "database": 3,
            }
        })
        params = resolve_redis_params(cfg, section="redis")
        assert params["host"] == "127.0.0.1"
        assert params["port"] == 6379
        assert params["password"] == "pass"
        assert params["database"] == 3
        assert params["url"] == "redis://:pass@127.0.0.1:6379/3"
