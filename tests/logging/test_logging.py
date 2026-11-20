from __future__ import annotations

import ast
import asyncio
import io
import logging as stdlib_logging
import sys
import threading
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import pytest

import swiftpy
import swiftpy.logging.channels as ch_mod
import swiftpy.logging.log as log_mod
import swiftpy.logging.manager as mgr_mod
from swiftpy import Application, Log, Response, TestClient, json_response
from swiftpy.console.commands.new import CONFIG_LOGGING_CONTENT
from swiftpy.core.container import bind_container
from swiftpy.logging import LogManager


def _base_logging_config(
    *,
    default: str = "single",
    level: str = "info",
    channels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    default_channels: dict[str, Any] = {
        "single": {
            "driver": "single",
            "path": "storage/logs/swiftpy.log",
            "level": level,
        },
        "daily": {
            "driver": "daily",
            "path": "storage/logs/swiftpy.log",
            "level": level,
            "days": 14,
        },
        "stack": {
            "driver": "stack",
            "channels": ["daily"],
        },
    }
    return {
        "logging": {
            "default": default,
            "level": level,
            "channels": channels if channels is not None else default_channels,
        }
    }


@pytest.mark.asyncio
async def test_log_facade_levels_context_and_relative_path_resolution(
    tmp_path: Path,
) -> None:
    app = Application(
        config=_base_logging_config(
            default="single",
            level="info",
            channels={
                "single": {
                    "driver": "single",
                    "path": "nested/logs/app.log",
                    "level": "info",
                },
            },
        ),
        project_root=tmp_path,
        auto_discover_providers=False,
    )

    log_file = tmp_path / "nested" / "logs" / "app.log"
    assert not log_file.exists()

    @app.get("/log")
    async def write_logs() -> Response:
        Log.debug("ignored-debug", trace_id="d1")
        Log.info("user-created", user_id=42, name="Ada")
        Log.warning("slow-query", duration_ms=250)
        Log.error("payment-failed", order_id=99)
        Log.critical("disk-full", volume="/var")
        try:
            raise ValueError("boom-value")
        except ValueError:
            Log.exception("handled-failure", step="checkout")
        return json_response({"logged": True})

    async with TestClient(app) as client:
        res = await client.get("/log")
        res.assert_ok()

    # Shutdown completed at context exit -> all queued records are guaranteed flushed
    assert log_file.is_file()
    content = log_file.read_text(encoding="utf-8")

    assert "ignored-debug" not in content
    assert "user-created" in content
    assert '"user_id": 42' in content
    assert '"name": "Ada"' in content
    assert "[WARNING]" in content and "slow-query" in content and '"duration_ms": 250' in content
    assert "[ERROR]" in content and "payment-failed" in content and '"order_id": 99' in content
    assert "[CRITICAL]" in content and "disk-full" in content and '"/var"' in content
    assert "handled-failure" in content and '"step": "checkout"' in content
    assert "ValueError: boom-value" in content


@pytest.mark.asyncio
async def test_daily_driver_uses_timed_rotating_handler_and_rotation_naming(
    tmp_path: Path,
) -> None:
    app = Application(
        config=_base_logging_config(
            default="daily",
            level="debug",
            channels={
                "daily": {
                    "driver": "daily",
                    "path": "storage/logs/swiftpy.log",
                    "level": "debug",
                    "days": 7,
                }
            },
        ),
        project_root=tmp_path,
        auto_discover_providers=False,
    )

    async with TestClient(app):
        mgr = app.container.resolve(LogManager)
        target_handlers = mgr.target_handlers_for("daily")
        assert len(target_handlers) == 1
        handler = target_handlers[0]
        assert isinstance(handler, TimedRotatingFileHandler)
        assert handler.backupCount == 7
        assert handler.when == "MIDNIGHT"
        assert handler.suffix == "%Y-%m-%d"

        Log.info("before-rollover")
        # Wait briefly so background QueueListener processes before manual rollover test
        await asyncio.sleep(0.05)
        handler.doRollover()
        Log.info("after-rollover")

    active_file = tmp_path / "storage" / "logs" / "swiftpy.log"
    assert active_file.is_file()
    rotated_files = list((tmp_path / "storage" / "logs").glob("swiftpy.log.*"))
    assert len(rotated_files) >= 1
    assert "after-rollover" in active_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_stack_fanout_channel_method_and_level_filtering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr_capture = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr_capture)
    app = Application(
        config=_base_logging_config(
            default="stack",
            level="debug",
            channels={
                "stack": {
                    "driver": "stack",
                    "channels": ["single", "stderr"],
                },
                "single": {
                    "driver": "single",
                    "path": "storage/logs/stack.log",
                    "level": "info",
                },
                "stderr": {
                    "driver": "stderr",
                    "level": "error",
                },
            },
        ),
        project_root=tmp_path,
        auto_discover_providers=False,
    )

    async with TestClient(app):
        with bind_container(app.container):
            Log.debug("dropped-everywhere")
            Log.info("only-single-file")
            Log.error("both-single-and-stderr", code="E500")
            Log.channel("single").warning("direct-single-warning")

    file_text = (tmp_path / "storage" / "logs" / "stack.log").read_text(encoding="utf-8")
    stderr_text = stderr_capture.getvalue()

    assert "dropped-everywhere" not in file_text
    assert "only-single-file" in file_text
    assert "only-single-file" not in stderr_text
    assert "both-single-and-stderr" in file_text
    assert "both-single-and-stderr" in stderr_text
    assert "direct-single-warning" in file_text
    assert "direct-single-warning" not in stderr_text


@pytest.mark.asyncio
async def test_framework_swiftpy_logs_and_unhandled_exceptions_reach_log_file(
    tmp_path: Path,
) -> None:
    app = Application(
        config=_base_logging_config(default="stack", level="info"),
        project_root=tmp_path,
        debug=False,
        auto_discover_providers=False,
    )

    @app.get("/crash")
    async def crash() -> Response:
        raise RuntimeError("unhandled-route-crash")

    @app.get("/framework-msg")
    async def framework_msg() -> Response:
        stdlib_logging.getLogger("swiftpy.custom.subsystem").warning(
            "subsystem-warning-signal"
        )
        return json_response({"ok": True})

    async with TestClient(app) as client:
        res_err = await client.get("/crash")
        res_err.assert_status(500)
        res_ok = await client.get("/framework-msg")
        res_ok.assert_ok()

    log_content = (tmp_path / "storage" / "logs" / "swiftpy.log").read_text(
        encoding="utf-8"
    )
    assert "Unhandled exception during request processing" in log_content
    assert "RuntimeError: unhandled-route-crash" in log_content
    assert "subsystem-warning-signal" in log_content


@pytest.mark.asyncio
async def test_logging_does_not_block_event_loop_and_flushes_on_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Application(
        config=_base_logging_config(default="single", level="info"),
        project_root=tmp_path,
        auto_discover_providers=False,
    )

    await app.startup()
    emit_threads: list[int] = []
    try:
        mgr = app.container.resolve(LogManager)
        target_handler = mgr.target_handlers_for("single")[0]
        orig_emit = target_handler.emit

        def slow_emit(record: stdlib_logging.LogRecord) -> None:
            emit_threads.append(threading.get_ident())
            time.sleep(0.15)
            orig_emit(record)

        monkeypatch.setattr(target_handler, "emit", slow_emit)

        main_thread_id = threading.get_ident()
        with bind_container(app.container):
            start_ts = time.perf_counter()
            for idx in range(3):
                Log.info("queued-fast", index=idx)
            elapsed = time.perf_counter() - start_ts
            # Enqueuing 3 records to slow 150ms disk sink must return immediately
            assert elapsed < 0.08
    finally:
        await app.shutdown()

    assert len(emit_threads) == 3
    assert main_thread_id not in emit_threads
    content = (tmp_path / "storage" / "logs" / "swiftpy.log").read_text(
        encoding="utf-8"
    )
    for idx in range(3):
        assert f'"index": {idx}' in content


@pytest.mark.asyncio
async def test_idempotent_lifecycle_and_multi_application_isolation(
    tmp_path: Path,
) -> None:
    dir1 = tmp_path / "app1"
    dir2 = tmp_path / "app2"
    dir1.mkdir()
    dir2.mkdir()

    app1 = Application(
        config=_base_logging_config(
            default="single",
            level="info",
            channels={
                "single": {
                    "driver": "single",
                    "path": "logs/app1.log",
                    "level": "info",
                }
            },
        ),
        project_root=dir1,
        auto_discover_providers=False,
    )
    app2 = Application(
        config=_base_logging_config(
            default="single",
            level="info",
            channels={
                "single": {
                    "driver": "single",
                    "path": "logs/app2.log",
                    "level": "info",
                }
            },
        ),
        project_root=dir2,
        auto_discover_providers=False,
    )

    @app1.get("/hit")
    async def hit1() -> Response:
        Log.info("entry-for-app1")
        stdlib_logging.getLogger("swiftpy.test.isolated").info("fw-for-app1")
        return json_response({"app": 1})

    @app2.get("/hit")
    async def hit2() -> Response:
        Log.info("entry-for-app2")
        stdlib_logging.getLogger("swiftpy.test.isolated").info("fw-for-app2")
        return json_response({"app": 2})

    # Idempotent start and stop calls
    await app1.startup()
    await app1.startup()
    mgr1 = app1.container.resolve(LogManager)
    swiftpy_logger = stdlib_logging.getLogger("swiftpy")
    handler_count_after_app1 = len(swiftpy_logger.handlers)

    await app2.startup()
    try:
        client1 = TestClient(app1)
        client2 = TestClient(app2)
        await client1.get("/hit")
        await client2.get("/hit")
    finally:
        await app1.shutdown()
        await app1.shutdown()
        assert mgr1.is_started is False
        # App2 remains running cleanly after App1 shutdown
        client2_after = TestClient(app2)
        await client2_after.get("/hit")
        await app2.shutdown()

    # All handlers attached to shared "swiftpy" logger are removed after shutdown
    assert len(swiftpy_logger.handlers) < handler_count_after_app1 or all(
        "QueueHandler" not in type(h).__name__ for h in swiftpy_logger.handlers
    )

    log1_text = (dir1 / "logs" / "app1.log").read_text(encoding="utf-8")
    log2_text = (dir2 / "logs" / "app2.log").read_text(encoding="utf-8")

    assert "entry-for-app1" in log1_text
    assert "fw-for-app1" in log1_text
    assert "entry-for-app2" not in log1_text
    assert "fw-for-app2" not in log1_text

    assert "entry-for-app2" in log2_text
    assert "fw-for-app2" in log2_text
    assert "entry-for-app1" not in log2_text
    assert "fw-for-app1" not in log2_text


@pytest.mark.asyncio
async def test_no_logging_config_preserves_backward_compatibility(
    tmp_path: Path,
) -> None:
    swiftpy_logger = stdlib_logging.getLogger("swiftpy")
    before_handlers = list(swiftpy_logger.handlers)

    app = Application(
        config={},
        project_root=tmp_path,
        auto_discover_providers=False,
    )
    async with TestClient(app):
        Log.info("no-file-opened-here")

    assert list(swiftpy_logger.handlers) == before_handlers
    assert not (tmp_path / "storage" / "logs").exists()


@pytest.mark.asyncio
async def test_invalid_logging_configs_raise_clear_errors(tmp_path: Path) -> None:
    bad_channel_app = Application(
        config={
            "logging": {
                "default": "missing",
                "level": "info",
                "channels": {"single": {"driver": "single", "path": "a.log"}},
            }
        },
        project_root=tmp_path,
        auto_discover_providers=False,
    )
    with pytest.raises(ValueError, match="missing"):
        await bad_channel_app.startup()

    cyclic_app = Application(
        config={
            "logging": {
                "default": "a",
                "level": "info",
                "channels": {
                    "a": {"driver": "stack", "channels": ["b"]},
                    "b": {"driver": "stack", "channels": ["a"]},
                },
            }
        },
        project_root=tmp_path,
        auto_discover_providers=False,
    )
    with pytest.raises(ValueError, match="cycle|Cyclic|circular"):
        await cyclic_app.startup()


def test_stdlib_logging_not_shadowed_and_no_relative_logging_imports() -> None:
    assert sys.modules["logging"] is stdlib_logging
    assert ch_mod.__dict__["logging"] is stdlib_logging
    assert log_mod.__dict__["logging"] is stdlib_logging
    assert mgr_mod.__dict__["logging"] is stdlib_logging
    assert swiftpy.Log is Log

    swiftpy_root = Path(swiftpy.__file__).resolve().parent
    for py_file in sorted(swiftpy_root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                mod_name = node.module or ""
                if mod_name == "logging" or mod_name.startswith("logging."):
                    pytest.fail(
                        f"Relative import from logging in {py_file}: level={node.level} module={mod_name!r}"
                    )
                for alias in node.names:
                    if alias.name == "logging" and node.module is None:
                        pytest.fail(f"Relative import of 'logging' in {py_file}")


@pytest.mark.asyncio
async def test_scaffolded_logging_config_creates_swiftpy_log_and_supports_stderr_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOG_CHANNEL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "logging.py").write_text(CONFIG_LOGGING_CONTENT, encoding="utf-8")

    # 1. Default template ("stack" -> "daily" -> "storage/logs/swiftpy.log")
    app = Application(project_root=tmp_path, auto_discover_providers=False)
    async with TestClient(app):
        with bind_container(app.container):
            Log.info("scaffold-default-log", env="local")

    log_path = tmp_path / "storage" / "logs" / "swiftpy.log"
    assert log_path.is_file()
    assert "scaffold-default-log" in log_path.read_text(encoding="utf-8")

    # 2. Switching default channel via .env LOG_CHANNEL=stderr works with scaffold config
    stderr_buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr_buf)
    (tmp_path / ".env").write_text("LOG_CHANNEL=stderr\nLOG_LEVEL=warning\n", encoding="utf-8")

    stderr_app = Application(project_root=tmp_path, auto_discover_providers=False)
    async with TestClient(stderr_app):
        with bind_container(stderr_app.container):
            Log.info("skipped-by-warning-level")
            Log.warning("emitted-to-stderr", component="worker")

    captured = stderr_buf.getvalue()
    assert "skipped-by-warning-level" not in captured
    assert "emitted-to-stderr" in captured
    assert '"component": "worker"' in captured


def test_stack_level_override_custom_log_level_and_sync_context_manager(
    tmp_path: Path,
) -> None:
    cfg = {
        "default": "stack",
        "level": "debug",
        "channels": {
            "stack": {
                "driver": "stack",
                "channels": ["daily"],
                "level": "warning",
            },
            "daily": {
                "driver": "daily",
                "path": "sync/app.log",
                "level": "debug",
            },
        },
    }

    with LogManager(cfg, base_path=tmp_path) as mgr:
        mgr.channel().info("filtered-by-stack-level")
        mgr.channel().log("error", "custom-level-error", code=503)
        mgr.channel("daily").debug("direct-daily-debug")

    text = (tmp_path / "sync" / "app.log").read_text(encoding="utf-8")
    assert "filtered-by-stack-level" not in text
    assert "custom-level-error" in text and '"code": 503' in text
    assert "direct-daily-debug" in text

    # Fallback when called outside any active started Application
    Log.info("fallback-outside-app")

    with pytest.raises(ValueError, match="days"):
        LogManager(
            {
                "default": "daily",
                "channels": {"daily": {"driver": "daily", "path": "x.log", "days": -1}},
            },
            base_path=tmp_path,
        ).start()

    with pytest.raises(ValueError, match="Unsupported log driver"):
        LogManager(
            {
                "default": "bad",
                "channels": {"bad": {"driver": "syslog"}},
            },
            base_path=tmp_path,
        ).start()


def test_sensitive_context_keys_are_redacted(tmp_path: Path) -> None:
    cfg = {
        "default": "single",
        "level": "info",
        "channels": {
            "single": {
                "driver": "single",
                "path": "logs/redacted.log",
            },
        },
    }
    with LogManager(cfg, base_path=tmp_path) as mgr:
        mgr.channel().info(
            "user-login-attempt",
            username="alice",
            password="SuperSecretPassword123!",
            nested={"api_key": "sk_live_999", "refresh_token": "jwt-tok", "safe": "ok"},
        )

    content = (tmp_path / "logs" / "redacted.log").read_text(encoding="utf-8")
    assert "SuperSecretPassword123!" not in content
    assert "sk_live_999" not in content
    assert "jwt-tok" not in content
    assert '"username": "alice"' in content
    assert '"password": "[REDACTED]"' in content
    assert '"api_key": "[REDACTED]"' in content
    assert '"refresh_token": "[REDACTED]"' in content
    assert '"safe": "ok"' in content

