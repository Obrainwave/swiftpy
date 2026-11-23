from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container, bind_container
from swiftpy.core.context import Context
from swiftpy.http.client_ip import (
    MAX_FORWARDED_HEADER_LENGTH,
    MAX_FORWARDED_HOPS,
    MAX_REAL_IP_HEADER_LENGTH,
    parse_trusted_proxies,
    resolve_client_ip,
)
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient
from swiftpy.websockets.connection import WebSocketConnection


async def _dummy_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _dummy_send(_message: dict[str, Any]) -> None:
    return None


def _make_request(
    *,
    client: Any = ("198.51.100.25", 50000),
    headers: list[tuple[bytes, bytes]] | None = None,
    trusted_proxies: list[str] | str | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/whoami",
        "headers": headers or [],
    }
    if client is not None:
        scope["client"] = client
    return Request(scope, _dummy_receive, trusted_proxies=trusted_proxies)


@pytest.fixture(autouse=True)
def _clean_context() -> Any:
    Context.clear()
    yield
    Context.clear()


def test_direct_peer_ipv4_ipv6_missing_and_invalid_peers() -> None:
    assert _make_request(client=("203.0.113.5", 45123)).ip() == "203.0.113.5"
    assert (
        _make_request(client=("2001:0db8:0000:0000:0000:0000:0000:0001", 80)).ip()
        == "2001:db8::1"
    )
    # Direct IPv4-mapped IPv6 peer normalizes to canonical IPv4
    assert _make_request(client=("::ffff:203.0.113.5", 443)).ip() == "203.0.113.5"

    # Missing/invalid peer ignores any forwarding headers cleanly
    hdrs = [
        (b"x-forwarded-for", b"198.51.100.10"),
        (b"x-real-ip", b"198.51.100.11"),
    ]
    assert _make_request(client=None, headers=hdrs, trusted_proxies=["0.0.0.0/0"]).ip() is None
    assert _make_request(client=(), headers=hdrs, trusted_proxies=["0.0.0.0/0"]).ip() is None
    for bad_peer in ("", "localhost", "127.0.0.1:8080", "not-an-ip", "unix:/tmp/sock"):
        assert (
            _make_request(
                client=(bad_peer, 1234),
                headers=hdrs,
                trusted_proxies=["127.0.0.1"],
            ).ip()
            is None
        )


def test_trusted_proxies_validation_and_private_subnets_not_auto_trusted() -> None:
    assert parse_trusted_proxies(None) == ()
    assert parse_trusted_proxies([]) == ()
    assert parse_trusted_proxies("") == ()
    assert parse_trusted_proxies("   ") == ()

    # Private / Docker peers are NOT trusted unless explicitly configured
    docker_hdrs = [
        (b"x-forwarded-for", b"203.0.113.88"),
        (b"x-real-ip", b"203.0.113.89"),
    ]
    for priv in ("127.0.0.1", "10.0.0.5", "172.17.0.2", "172.30.0.10", "192.168.1.25", "::1"):
        assert _make_request(client=(priv, 1234), headers=docker_hdrs).ip() == priv

    # Invalid entries fail eagerly on parse and on Application startup
    invalid_inputs: list[Any] = [
        "*",
        ["*"],
        [""],
        ["   "],
        [123],
        {"proxy": "127.0.0.1"},
        ["not-an-ip"],
        ["localhost"],
        ["127.0.0.1:8080"],
        ["10.0.0.0/99"],
        ["192.168.1.5/24"],  # host bits set
        ["fe80::1%eth0"],
    ]
    for bad in invalid_inputs:
        with pytest.raises(ConfigurationError):
            parse_trusted_proxies(bad)
        with pytest.raises(ConfigurationError):
            Application(trusted_proxies=bad)
        with pytest.raises(ConfigurationError):
            Application(config={"http": {"trusted_proxies": bad}})


def test_exact_cidr_and_ipv4_mapped_proxy_matching() -> None:
    # Exact match
    req = _make_request(
        client=("127.0.0.1", 5555),
        headers=[(b"x-forwarded-for", b"198.51.100.77")],
        trusted_proxies=["127.0.0.1"],
    )
    assert req.ip() == "198.51.100.77"

    # IPv6 proxy exact & CIDR match
    req_v6 = _make_request(
        client=("2001:db8:1::10", 5555),
        headers=[(b"x-forwarded-for", b"2001:0db8:0099::1, 2001:db8:1::20")],
        trusted_proxies=["2001:db8:1::/48"],
    )
    assert req_v6.ip() == "2001:db8:99::1"

    # Docker CIDR matching & non-matching range
    docker_cidr = ["172.30.0.0/24"]
    assert (
        _make_request(
            client=("172.30.0.10", 4444),
            headers=[(b"x-forwarded-for", b"203.0.113.50, 172.30.0.5")],
            trusted_proxies=docker_cidr,
        ).ip()
        == "203.0.113.50"
    )
    # Non-matching Docker subnet ignores XFF
    assert (
        _make_request(
            client=("172.31.0.10", 4444),
            headers=[(b"x-forwarded-for", b"203.0.113.50")],
            trusted_proxies=docker_cidr,
        ).ip()
        == "172.31.0.10"
    )

    # Dual-stack peer ::ffff:172.30.0.10 matches IPv4 CIDR 172.30.0.0/24
    assert (
        _make_request(
            client=("::ffff:172.30.0.10", 4444),
            headers=[(b"x-forwarded-for", b"::ffff:203.0.113.50")],
            trusted_proxies=["172.30.0.0/24"],
        ).ip()
        == "203.0.113.50"
    )
    # Proxy CIDR configured as ::ffff:172.30.0.0/120 matches IPv4 peer 172.30.0.10
    assert (
        _make_request(
            client=("172.30.0.10", 4444),
            headers=[(b"x-forwarded-for", b"203.0.113.51")],
            trusted_proxies=["::ffff:172.30.0.0/120"],
        ).ip()
        == "203.0.113.51"
    )


def test_xff_right_to_left_spoofing_and_strict_fallback_matrix() -> None:
    trusted = ["127.0.0.1", "10.0.0.0/8", "172.30.0.0/24", "::1"]

    # 1. Forged XFF/X-Real-IP from untrusted peer ignored completely
    untrusted_req = _make_request(
        client=("198.51.100.9", 5000),
        headers=[
            (b"x-forwarded-for", b"1.1.1.1, 203.0.113.50"),
            (b"x-real-ip", b"1.1.1.1"),
        ],
        trusted_proxies=trusted,
    )
    assert untrusted_req.ip() == "198.51.100.9"

    # 2. Multi-hop chain: spoofed prefix 1.1.1.1 ignored; stops at right-most untrusted 203.0.113.50
    multi_hop = _make_request(
        client=("127.0.0.1", 5000),
        headers=[(b"x-forwarded-for", b"1.1.1.1, 203.0.113.50, 10.2.3.4, 172.30.0.5")],
        trusted_proxies=trusted,
    )
    assert multi_hop.ip() == "203.0.113.50"

    # 3. Mixed IPv4/IPv6 chain with repeated headers folded in arrival order
    mixed_chain = _make_request(
        client=("::1", 5000),
        headers=[
            (b"x-forwarded-for", b"2001:db8::99, 10.1.1.1"),
            (b"X-Forwarded-For", b"::ffff:172.30.0.9"),
        ],
        trusted_proxies=trusted,
    )
    assert mixed_chain.ip() == "2001:db8::99"

    # 4. Malformed XFF must NOT fall through to a valid-looking X-Real-IP
    malformed_with_real = _make_request(
        client=("172.30.0.10", 5000),
        headers=[
            (b"x-forwarded-for", b"8.8.8.8, garbage, 172.30.0.5"),
            (b"x-real-ip", b"8.8.8.8"),
        ],
        trusted_proxies=trusted,
    )
    assert malformed_with_real.ip() == "172.30.0.10"

    # 5. Malformed tokens (hostnames, ports, brackets, quotes, zone IDs) abort to direct peer
    for bad_token in (
        "8.8.8.8, evil.example.com",
        "8.8.8.8, 203.0.113.10:8080",
        "8.8.8.8, [2001:db8::1]:443",
        '"8.8.8.8"',
        "fe80::1%eth0",
        "203.0.113.10\n",
    ):
        bad_req = _make_request(
            client=("127.0.0.1", 5000),
            headers=[
                (b"x-forwarded-for", bad_token.encode("latin-1")),
                (b"x-real-ip", b"8.8.8.8"),
            ],
            trusted_proxies=trusted,
        )
        assert bad_req.ip() == "127.0.0.1"

    # 6. Empty and whitespace-only XFF headers fail conservatively to direct peer (no X-Real-IP fallthrough)
    for empty_xff in (b"", b"   ", b" , , "):
        empty_req = _make_request(
            client=("172.30.0.10", 5000),
            headers=[(b"x-forwarded-for", empty_xff), (b"x-real-ip", b"8.8.8.8")],
            trusted_proxies=trusted,
        )
        assert empty_req.ip() == "172.30.0.10"

    # 7. XFF containing ONLY trusted hops resolves deterministically to direct peer (no X-Real-IP leakage)
    only_trusted = _make_request(
        client=("172.30.0.10", 5000),
        headers=[
            (b"x-forwarded-for", b"127.0.0.1, 10.0.0.5"),
            (b"x-real-ip", b"8.8.8.8"),
        ],
        trusted_proxies=trusted,
    )
    assert only_trusted.ip() == "172.30.0.10"

    # 8. Oversized XFF (> MAX_FORWARDED_HEADER_LENGTH) & excessive hops (> MAX_FORWARDED_HOPS)
    huge_xff = (b"203.0.113.5, " * 400)[: MAX_FORWARDED_HEADER_LENGTH + 10]
    assert (
        _make_request(
            client=("127.0.0.1", 5000),
            headers=[(b"x-forwarded-for", huge_xff), (b"x-real-ip", b"8.8.8.8")],
            trusted_proxies=trusted,
        ).ip()
        == "127.0.0.1"
    )

    excessive_hops = ", ".join(["203.0.113.5"] * (MAX_FORWARDED_HOPS + 1)).encode("ascii")
    assert (
        _make_request(
            client=("127.0.0.1", 5000),
            headers=[(b"x-forwarded-for", excessive_hops), (b"x-real-ip", b"8.8.8.8")],
            trusted_proxies=trusted,
        ).ip()
        == "127.0.0.1"
    )

    # 9. Missing XFF falls back cleanly to validated X-Real-IP
    assert (
        _make_request(
            client=("172.30.0.10", 5000),
            headers=[(b"x-real-ip", b"2001:0db8::0042")],
            trusted_proxies=trusted,
        ).ip()
        == "2001:db8::42"
    )

    # 10. Invalid, oversized, or conflicting X-Real-IP falls back safely to direct peer
    for bad_real in (
        [(b"x-real-ip", b"not-an-ip")],
        [(b"x-real-ip", b"203.0.113.5:8080")],
        [(b"x-real-ip", b"fe80::1%eth0")],
        [(b"x-real-ip", b"1" * (MAX_REAL_IP_HEADER_LENGTH + 1))],
        [(b"x-real-ip", b"8.8.8.8"), (b"x-real-ip", b"1.1.1.1")],
    ):
        assert (
            _make_request(
                client=("172.30.0.10", 5000),
                headers=bad_real,
                trusted_proxies=trusted,
            ).ip()
            == "172.30.0.10"
        )


def test_request_class_ip_outside_context_and_standalone_container_isolation() -> None:
    # Outside any request context, Request.ip() safely returns None
    assert Request.ip() is None

    # Standalone bare Request ignores Container fallback outside bind_container if none configured
    c = Container()
    c.instance(Config, Config({"http": {"trusted_proxies": ["127.0.0.1"]}}))
    with bind_container(c):
        req = _make_request(
            client=("127.0.0.1", 5000),
            headers=[(b"x-forwarded-for", b"203.0.113.45")],
        )
        assert req.ip() == "203.0.113.45"
        Context.set("request", req)
        assert Request.ip() == "203.0.113.45"
        Context.clear()
        assert Request.ip() is None


def test_application_trusted_proxies_comma_string_resolves_forwarded_ip() -> None:
    app = Application(trusted_proxies="10.0.0.1, 10.0.0.2")

    assert app.config.get("http.trusted_proxies") == ["10.0.0.1/32", "10.0.0.2/32"]

    for peer in ("10.0.0.1", "10.0.0.2"):
        req = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [(b"x-forwarded-for", b"203.0.113.9")],
                "client": (peer, 5000),
            },
            _dummy_receive,
            config=app.config,
        )
        assert req.ip() == "203.0.113.9"
        # Memoized per request: repeated calls return the identical value
        assert req.ip() == "203.0.113.9"
        assert req.ip is req.ip


def test_config_load_http_trusted_proxies_env_and_json_formats(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    src_http = Path(__file__).resolve().parents[2] / "config" / "http.py"
    (cfg_dir / "http.py").write_text(src_http.read_text(encoding="utf-8"), encoding="utf-8")

    # Default empty
    cfg_default = Config.load(cfg_dir, env={})
    assert cfg_default.get("http.trusted_proxies") == []

    # Comma-separated TRUSTED_PROXIES
    cfg_csv = Config.load(
        cfg_dir,
        env={"TRUSTED_PROXIES": "127.0.0.1, 172.30.0.0/24"},
    )
    assert parse_trusted_proxies(cfg_csv.get("http.trusted_proxies")) == parse_trusted_proxies(
        ["127.0.0.1", "172.30.0.0/24"]
    )

    # JSON array HTTP_TRUSTED_PROXIES
    cfg_json = Config.load(
        cfg_dir,
        env={"HTTP_TRUSTED_PROXIES": json.dumps(["::1", "10.0.0.0/8"])},
    )
    assert parse_trusted_proxies(cfg_json.get("http.trusted_proxies")) == parse_trusted_proxies(
        ["::1", "10.0.0.0/8"]
    )


@pytest.mark.asyncio
async def test_http_websocket_and_concurrent_request_ip_isolation() -> None:
    app = Application(
        config={
            "app": {"env": "testing", "debug": True},
            "http": {"trusted_proxies": ["172.30.0.0/24", "127.0.0.1"]},
        }
    )

    @app.get("/ip")
    async def get_ip(request: Request) -> Response:
        # Yield to event loop so concurrent tasks overlap in flight
        await asyncio.sleep(0.005)
        inst_ip = request.ip()
        static_ip = Request.ip()
        assert inst_ip == static_ip
        return json_response({"ip": inst_ip, "static_ip": static_ip})

    @app.websocket("/ws-ip")
    async def ws_ip(ws: WebSocketConnection) -> None:
        await ws.accept()
        await ws.send_json({"ws_ip": ws.ip(), "static_ip": Request.ip()})
        await ws.close()

    # Concurrent HTTP requests via ASGI scope to vary peer IPs and forwarding chains
    async def _invoke_http(idx: int) -> dict[str, Any]:
        is_trusted = idx % 2 == 0
        peer_ip = f"172.30.0.{(idx % 10) + 1}" if is_trusted else f"198.51.100.{idx + 1}"
        forwarded_client = f"203.0.113.{idx + 10}"
        raw_headers = [(b"x-forwarded-for", forwarded_client.encode("ascii"))]
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/ip",
            "raw_path": b"/ip",
            "query_string": b"",
            "headers": raw_headers,
            "client": (peer_ip, 40000 + idx),
            "server": ("127.0.0.1", 8000),
        }
        status = 0
        body = bytearray()

        async def send(msg: dict[str, Any]) -> None:
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
            elif msg["type"] == "http.response.body":
                body.extend(msg.get("body", b""))

        await app(scope, _dummy_receive, send)
        assert status == 200
        parsed = json.loads(body.decode("utf-8"))
        assert isinstance(parsed, dict)
        expected = forwarded_client if is_trusted else peer_ip
        assert parsed == {"ip": expected, "static_ip": expected}
        return parsed

    async with TestClient(app):
        results = await asyncio.gather(*(_invoke_http(i) for i in range(30)))
        assert len(results) == 30

        # WebSocket behind trusted Nginx peer resolves forwarded IP
        ws_trusted = _make_ws_result(
            app,
            peer=("172.30.0.5", 60000),
            headers=[(b"x-forwarded-for", b"203.0.113.200, 127.0.0.1")],
        )
        ws_trusted_payload = await ws_trusted
        assert ws_trusted_payload == {
            "ws_ip": "203.0.113.200",
            "static_ip": "203.0.113.200",
        }

        # WebSocket conflicting X-Real-IP headers fall back safely to direct peer
        ws_conflict = await _make_ws_result(
            app,
            peer=("172.30.0.5", 60001),
            headers=[
                (b"x-real-ip", b"203.0.113.1"),
                (b"x-real-ip", b"203.0.113.2"),
            ],
        )
        assert ws_conflict == {"ws_ip": "172.30.0.5", "static_ip": "172.30.0.5"}

        # Ensure pure resolver rejects dict headers cleanly on conflicting scope headers
        assert (
            resolve_client_ip(
                {
                    "client": ("172.30.0.5", 1234),
                    "headers": [
                        (b"x-real-ip", b"203.0.113.1"),
                        (b"x-real-ip", b"203.0.113.2"),
                    ],
                },
                trusted_proxies=parse_trusted_proxies(["172.30.0.0/24"]),
            )
            == "172.30.0.5"
        )


async def _make_ws_result(
    app: Application,
    *,
    peer: tuple[str, int],
    headers: list[tuple[bytes, bytes]],
) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "websocket",
        "path": "/ws-ip",
        "headers": headers,
        "client": peer,
        "server": ("127.0.0.1", 8000),
    }
    sent_messages: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = [{"type": "websocket.connect"}]

    async def ws_receive() -> dict[str, Any]:
        if incoming:
            return incoming.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def ws_send(msg: dict[str, Any]) -> None:
        sent_messages.append(msg)

    await app(scope, ws_receive, ws_send)
    text_msg = next(m for m in sent_messages if m["type"] == "websocket.send")
    parsed = json.loads(text_msg["text"])
    assert isinstance(parsed, dict)
    return parsed
