from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Generator
from pathlib import Path

import pytest

from swiftpy.auth.auth_manager import CurrentUser
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.core.container import Container, get_container, set_container
from swiftpy.core.context import Context
from swiftpy.helpers import mail, storage
from swiftpy.http.request import Request
from swiftpy.http.response import Response, ResponseHeaders, json_response
from swiftpy.testing.http_client import TestClient


@pytest.fixture(autouse=True)
def _restore_config_and_container() -> Generator[None, None, None]:
    original_instance = Config._instance
    try:
        original_container: Container | None = get_container()
    except RuntimeError:
        original_container = None

    yield

    Config._instance = original_instance
    if original_container is not None:
        set_container(original_container)
    Context.clear()


def test_current_user_json_dumps_and_dict_stay_in_sync_after_late_context_update() -> None:
    """CORE-4: json.dumps(early_user) and dict(early_user) reflect Context updates."""
    app = Application(config={}, auto_discover_providers=False)
    Context.clear()

    early_user = app.container.resolve(CurrentUser)
    assert json.loads(json.dumps(early_user)) == {}
    assert dict(early_user) == {}

    Context.set(
        "__swiftpy_current_user__",
        {"id": 99, "email": "late@example.com", "role": "editor"},
    )

    assert json.loads(json.dumps(early_user)) == {
        "id": 99,
        "email": "late@example.com",
        "role": "editor",
    }
    assert dict(early_user) == {
        "id": 99,
        "email": "late@example.com",
        "role": "editor",
    }
    assert early_user == {
        "id": 99,
        "email": "late@example.com",
        "role": "editor",
    }

    Context.set("__swiftpy_current_user__", {"id": 100, "email": "next@example.com"})
    assert json.loads(json.dumps(early_user)) == {
        "id": 100,
        "email": "next@example.com",
    }

    Context.clear()
    assert json.loads(json.dumps(early_user)) == {}
    assert dict(early_user) == {}


def test_config_load_dotenv_precedence_and_relative_cache_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CORE-5: Config.load loads .env without mutating os.environ and scopes cache to config_dir root."""
    proj_a = tmp_path / "proj_a"
    (proj_a / "config").mkdir(parents=True)
    (proj_a / "config" / "app.py").write_text(
        'from swiftpy.core.config import env\nCONFIG = {"name": env("APP_NAME", "DefaultA"), "env": env("APP_ENV", "local")}\n',
        encoding="utf-8",
    )
    (proj_a / ".env").write_text(
        'APP_NAME="FromDotEnv"\nAPP_ENV=staging\nDOTENV_ONLY_SECRET="secret-val"\n',
        encoding="utf-8",
    )

    cwd_dir = tmp_path / "unrelated_cwd"
    (cwd_dir / "bootstrap" / "cache").mkdir(parents=True)
    (cwd_dir / "bootstrap" / "cache" / "config.json").write_text(
        json.dumps({"app": {"name": "WrongCwdCache"}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(cwd_dir)
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DOTENV_ONLY_SECRET", raising=False)

    cfg = Config.load(proj_a / "config")
    assert cfg.get("app.name") == "FromDotEnv"
    assert cfg.get("app.env") == "staging"
    assert "DOTENV_ONLY_SECRET" not in os.environ

    # Process environment overrides .env
    monkeypatch.setenv("APP_NAME", "FromProcessEnv")
    cfg_override = Config.load(proj_a / "config")
    assert cfg_override.get("app.name") == "FromProcessEnv"
    assert cfg_override.get("app.env") == "staging"

    # Relative cache inside proj_a/bootstrap/cache/config.json works regardless of CWD
    cfg_override.dump_cache(cache_path=proj_a / "bootstrap" / "cache" / "config.json")
    monkeypatch.setenv("APP_NAME", "ShouldUseProjectCache")
    cfg_cached = Config.load(proj_a / "config")
    assert cfg_cached.get("app.name") == "FromProcessEnv"


def test_application_kwargs_take_top_precedence_over_loaded_config() -> None:
    """CORE-5: Explicit Application(**kwargs) overrides existing config keys."""
    app = Application(
        config={
            "app": {"name": "BaseName", "env": "local", "debug": False},
            "custom": "from_config",
        },
        auto_discover_providers=False,
        app={"name": "TopKwargName"},
        custom="from_kwarg",
    )
    assert app.config.get("app.name") == "TopKwargName"
    assert app.config.get("app.env") == "local"
    assert app.config.get("custom") == "from_kwarg"


@pytest.mark.asyncio
async def test_multi_app_concurrent_asgi_container_and_config_isolation() -> None:
    """CORE-5: Concurrent ASGI requests to two Application instances stay container/config isolated."""
    app1 = Application(
        config={"app": {"name": "AlphaApp"}},
        auto_discover_providers=False,
    )
    app2 = Application(
        config={"app": {"name": "BetaApp"}},
        auto_discover_providers=False,
    )

    @app1.get("/who")
    async def who1(request: Request) -> Response:
        await asyncio.sleep(0.01)
        return json_response(
            {
                "cfg": Config.get_instance().get("app.name"),
                "same_container": get_container() is app1.container,
            }
        )

    @app2.get("/who")
    async def who2(request: Request) -> Response:
        await asyncio.sleep(0.01)
        return json_response(
            {
                "cfg": Config.get_instance().get("app.name"),
                "same_container": get_container() is app2.container,
            }
        )

    c1 = TestClient(app1)
    c2 = TestClient(app2)

    r1, r2, r1b = await asyncio.gather(
        c1.get("/who"),
        c2.get("/who"),
        c1.get("/who"),
    )
    r1.assert_ok()
    r2.assert_ok()
    r1b.assert_ok()

    assert r1.json() == {"cfg": "AlphaApp", "same_container": True}
    assert r2.json() == {"cfg": "BetaApp", "same_container": True}
    assert r1b.json() == {"cfg": "AlphaApp", "same_container": True}


def test_helpers_exports_and_response_headers_update_protocol() -> None:
    """CORE-6: helpers exports mail/storage and ResponseHeaders.update works cleanly."""
    assert callable(mail)
    assert callable(storage)

    hdrs = ResponseHeaders({"X-Initial": "one"})
    hdrs.update([("X-Pair", "two")], x_kw="three")
    assert hdrs["x-initial"] == "one"
    assert hdrs["x-pair"] == "two"
    assert hdrs["x_kw"] == "three"
