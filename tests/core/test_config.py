from __future__ import annotations

import pytest

from swiftpy.core.application import Application
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.helpers.helper import config


def test_config_dotted_get_and_set() -> None:
    cfg = Config(
        {
            "database": {
                "default": "postgresql",
                "pool": {
                    "min": 5,
                    "max": 50,
                },
            },
            "app": {
                "name": "SwiftPY Test",
                "debug": False,
            },
        }
    )

    # 1. Dotted get
    assert cfg.get("database.default") == "postgresql"
    assert cfg.get("database.pool.min") == 5
    assert cfg.get("database.pool.max") == 50
    assert cfg.get("app.name") == "SwiftPY Test"
    assert cfg.get("nonexistent.key", "default_val") == "default_val"

    # 2. Dotted set
    cfg.set("database.pool.max", 100)
    assert cfg.get("database.pool.max") == 100

    cfg.set("cache.redis.port", 6379)
    assert cfg.get("cache.redis.port") == 6379

    # 3. Dunder methods: __getitem__, __setitem__, __contains__, __call__
    assert cfg["database.pool.max"] == 100
    assert "database.pool.min" in cfg
    assert "missing.key" not in cfg

    cfg["app.debug"] = True
    assert cfg.get("app.debug") is True

    # Call operator
    assert cfg("database.pool.max") == 100
    assert cfg("missing.nested", "fallback") == "fallback"


def test_config_env_variable_merging() -> None:
    mock_env = {
        # Double underscore notation
        "DATABASE__POOL__MAX_SIZE": "88",
        # Top-level prefix mapping
        "APP_DEBUG": "true",
        "APP_NAME": "Overridden Name",
        # SWIFTPY_ prefix
        "SWIFTPY_APP__ENV": "production",
        # Custom nested key
        "CACHE__REDIS__PORT": "6379",
        "FEATURE__ENABLED": "1",
        "FEATURE__COUNT": "42",
        "FEATURE__RATIO": "3.14",
        "FEATURE__CONFIG": '{"flag": true, "items": [1, 2]}',
    }

    # Load with custom env dict
    cfg = Config.load(config_dir="config", env=mock_env)

    # Test overridden database pool max_size
    assert cfg.get("database.pool.max_size") == 88
    # Test top-level mappings
    assert cfg.get("app.debug") is True
    assert cfg.get("app.name") == "Overridden Name"
    # Test SWIFTPY_ prefix
    assert cfg.get("app.env") == "production"
    # Test dynamic nested additions
    assert cfg.get("cache.redis.port") == 6379
    assert cfg.get("feature.count") == 42
    assert cfg.get("feature.ratio") == 3.14
    assert cfg.get("feature.config") == {"flag": True, "items": [1, 2]}


def test_config_single_underscore_flattened_matching() -> None:
    """Verify DATABASE_POOL_MAX_SIZE=88 matches existing normalized path database.pool.max_size."""
    mock_env = {
        "DATABASE_POOL_MAX_SIZE": "88",
    }
    cfg = Config.load(config_dir="config", env=mock_env)
    assert cfg.get("database.pool.max_size") == 88


def test_application_startup_automatically_loads_config_modules() -> None:
    """Verify that Application() startup automatically loads config/*.py without explicit Config.load call."""
    app = Application()

    # app.config must automatically contain keys from config/database.py and config/app.py
    assert app.config.has("database.default")
    assert app.config.has("database.pool.max_size")
    assert app.config.get("database.default") == "postgresql"
    assert isinstance(app.config.get("database.pool.max_size"), int)


def test_config_environment_variable_precedence_hierarchy() -> None:
    """
    Verify precedence order:
    SWIFTPY_ prefix (highest) > double-underscore (medium) > single-underscore / prefix (lowest).
    """
    # 1. Double underscore overrides single-underscore normalized path
    mock_env_1 = {
        "DATABASE_POOL_MAX_SIZE": "60",
        "DATABASE__POOL__MAX_SIZE": "88",
    }
    cfg_1 = Config.load(config_dir="config", env=mock_env_1)
    assert cfg_1.get("database.pool.max_size") == 88

    # 2. SWIFTPY_ prefix overrides non-prefixed double-underscore
    mock_env_2 = {
        "DATABASE_POOL_MAX_SIZE": "60",
        "DATABASE__POOL__MAX_SIZE": "88",
        "SWIFTPY_DATABASE__POOL__MAX_SIZE": "99",
    }
    cfg_2 = Config.load(config_dir="config", env=mock_env_2)
    assert cfg_2.get("database.pool.max_size") == 99


def test_config_malformed_values_raise_configuration_error() -> None:
    """Verify that malformed integer, float, and JSON values raise ConfigurationError."""
    # 1. Malformed integer for existing int key
    with pytest.raises(ConfigurationError, match="expects an integer"):
        Config.load(
            config_dir="config",
            env={"DATABASE__POOL__MAX_SIZE": "not_an_integer_value"},
        )

    # 2. Malformed float for existing float key
    with pytest.raises(ConfigurationError, match="expects a float"):
        Config.load(
            config_dir="config",
            env={"DATABASE__POOL__COMMAND_TIMEOUT": "invalid_float_value"},
        )

    # 3. Malformed JSON syntax
    with pytest.raises(ConfigurationError, match="malformed JSON"):
        Config.load(
            config_dir="config",
            env={"APP__INVALID_CONFIG": "{broken_json: True, invalid}"},
        )


def test_global_config_helper() -> None:
    app = Application(config={"database": {"pool": {"max": 75}}})

    # Test global helper function
    assert config("database.pool.max") == 75
    assert config("missing.param", default=999) == 999
    assert config() is app.config
