from __future__ import annotations

import functools
import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Any, Protocol, runtime_checkable

from swiftpy.core.config import ConfigurationError

MAX_FORWARDED_HEADER_LENGTH: int = 4096
MAX_FORWARDED_HOPS: int = 32
MAX_REAL_IP_HEADER_LENGTH: int = 256

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network
TrustedProxies = tuple[IPNetwork, ...]


@runtime_checkable
class _MultiHeaderLookup(Protocol):
    def getlist(self, name: str) -> list[str]: ...


def _unwrap_ipv4_mapped(addr: IPAddress) -> IPAddress:
    """Normalize IPv4-mapped IPv6 addresses (e.g. ::ffff:203.0.113.5) to IPv4."""
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _parse_ip(raw: object, *, allow_zone_id: bool) -> IPAddress | None:
    """
    Strictly validate and normalize a bare IPv4 or IPv6 address literal.

    Rejects hostnames, ports, CIDR masks, quotes, brackets, internal whitespace,
    control characters, and zone IDs when `allow_zone_id=False`.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    if not allow_zone_id and "%" in raw:
        return None
    if raw[0] in ('"', "[", "'") or "/" in raw:
        return None

    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None

    return _unwrap_ipv4_mapped(addr)


def _normalize_network(net: IPNetwork) -> IPNetwork:
    """Normalize IPv4-mapped IPv6 CIDR networks into equivalent IPv4 networks."""
    if isinstance(net, IPv6Network) and net.prefixlen >= 96:
        mapped = net.network_address.ipv4_mapped
        if mapped is not None:
            return IPv4Network((int(mapped), net.prefixlen - 96), strict=True)
    return net


@functools.lru_cache(maxsize=64)
def _compile_trusted_proxies_cached(entries: tuple[str, ...]) -> TrustedProxies:
    networks: list[IPNetwork] = []
    for raw_entry in entries:
        cleaned = raw_entry.strip()
        if (
            not cleaned
            or "*" in cleaned
            or "%" in cleaned
            or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned)
        ):
            raise ConfigurationError(f"Invalid trusted proxy entry: {raw_entry!r}")

        if "/" in cleaned:
            try:
                net = ipaddress.ip_network(cleaned, strict=True)
            except ValueError as exc:
                raise ConfigurationError(
                    f"Invalid trusted proxy CIDR {raw_entry!r}: {exc}"
                ) from exc
            networks.append(_normalize_network(net))
        else:
            addr = _parse_ip(cleaned, allow_zone_id=False)
            if addr is None:
                raise ConfigurationError(
                    f"Invalid trusted proxy IP address: {raw_entry!r}"
                )
            networks.append(ipaddress.ip_network(addr))

    return tuple(networks)


def parse_trusted_proxies(
    raw: Sequence[str] | Iterable[str] | TrustedProxies | str | None,
) -> TrustedProxies:
    """
    Validate and compile configured trusted proxy IPs and CIDR ranges.

    Supports individual IPv4/IPv6 addresses and strict CIDR ranges.
    Empty/None configuration returns ``()`` (no proxies trusted).
    """
    if raw is None:
        return ()

    if isinstance(raw, str):
        if not raw.strip():
            return ()
        entries = tuple(part.strip() for part in raw.split(","))
        return _compile_trusted_proxies_cached(entries)

    if isinstance(raw, (bytes, bytearray, Mapping)):
        raise ConfigurationError(
            f"trusted_proxies must be a sequence of IP/CIDR strings or comma-separated string, got {type(raw).__name__}"
        )

    if not isinstance(raw, Iterable):
        raise ConfigurationError(
            f"trusted_proxies must be a sequence of IP/CIDR strings, got {type(raw).__name__}"
        )

    items = list(raw)
    if not items:
        return ()

    if all(isinstance(item, (IPv4Network, IPv6Network)) for item in items):
        return tuple(_normalize_network(item) for item in items if isinstance(item, (IPv4Network, IPv6Network)))

    str_entries: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ConfigurationError(
                f"Trusted proxy entry must be a string, got {type(item).__name__}: {item!r}"
            )
        str_entries.append(item)

    return _compile_trusted_proxies_cached(tuple(str_entries))


def extract_trusted_proxies_from_config(config: Any) -> TrustedProxies:
    """Resolve and validate ``trusted_proxies`` from a Config-like object."""
    if config is None or not hasattr(config, "get"):
        return ()

    raw_proxies: Any = None
    for key in ("http.trusted_proxies", "app.trusted_proxies", "trusted_proxies"):
        candidate = config.get(key)
        if candidate is not None:
            raw_proxies = candidate
            break

    return parse_trusted_proxies(raw_proxies)


def _is_trusted(addr: IPAddress, trusted_proxies: TrustedProxies) -> bool:
    for net in trusted_proxies:
        if addr.version == net.version and addr in net:
            return True
    return False


def _get_header_values(
    scope: Mapping[str, Any],
    headers: _MultiHeaderLookup | Mapping[str, str] | None,
    name: str,
) -> list[str]:
    if isinstance(headers, _MultiHeaderLookup):
        return headers.getlist(name)

    target_lower = name.lower()
    raw_headers = scope.get("headers")
    if isinstance(raw_headers, Sequence):
        values: list[str] = []
        for entry in raw_headers:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                continue
            raw_k, raw_v = entry
            key_str = (
                raw_k.decode("latin-1")
                if isinstance(raw_k, (bytes, bytearray))
                else str(raw_k)
            )
            if key_str.lower() == target_lower:
                val_str = (
                    raw_v.decode("latin-1")
                    if isinstance(raw_v, (bytes, bytearray))
                    else str(raw_v)
                )
                values.append(val_str)
        if values:
            return values

    if isinstance(headers, Mapping):
        for k, v in headers.items():
            if k.lower() == target_lower:
                return [v]

    return []


def resolve_client_ip(
    scope: Mapping[str, Any],
    headers: _MultiHeaderLookup | Mapping[str, str] | None = None,
    *,
    trusted_proxies: TrustedProxies = (),
) -> str | None:
    """
    Resolve the normalized canonical client IPv4 or IPv6 string from ASGI scope
    and request headers, trusting forwarding headers only from `trusted_proxies`.

    Conservative fallback rules:
    - Missing/invalid ASGI peer -> ``None`` (ignores headers).
    - Untrusted peer -> ``str(peer)`` (ignores headers).
    - ``X-Forwarded-For`` present but empty, malformed, oversized, exceeding hop
      limit, or containing only trusted proxies -> ``str(peer)`` (never falls back
      to ``X-Real-IP``).
    - ``X-Forwarded-For`` absent -> inspects validated single/consistent ``X-Real-IP``,
      otherwise ``str(peer)``.
    """
    client = scope.get("client")
    if not isinstance(client, (tuple, list)) or len(client) < 1:
        return None

    peer = _parse_ip(client[0], allow_zone_id=True)
    if peer is None:
        return None

    if not trusted_proxies or not _is_trusted(peer, trusted_proxies):
        return str(peer)

    xff_values = _get_header_values(scope, headers, "x-forwarded-for")
    if xff_values:
        combined = ",".join(xff_values)
        if len(combined) > MAX_FORWARDED_HEADER_LENGTH or not combined.strip():
            return str(peer)

        raw_parts = combined.split(",")
        if len(raw_parts) > MAX_FORWARDED_HOPS:
            return str(peer)

        for raw_part in reversed(raw_parts):
            hop = _parse_ip(raw_part.strip(" \t"), allow_zone_id=False)
            if hop is None:
                return str(peer)
            if _is_trusted(hop, trusted_proxies):
                continue
            return str(hop)

        # Valid chain where every listed hop is a trusted proxy.
        return str(peer)

    real_ip_values = _get_header_values(scope, headers, "x-real-ip")
    if not real_ip_values:
        return str(peer)

    if sum(len(v) for v in real_ip_values) + max(0, len(real_ip_values) - 1) > MAX_REAL_IP_HEADER_LENGTH:
        return str(peer)

    normalized_real: list[str] = []
    for raw_val in real_ip_values:
        parsed_real = _parse_ip(raw_val.strip(" \t"), allow_zone_id=False)
        if parsed_real is None:
            return str(peer)
        normalized_real.append(str(parsed_real))

    if len(set(normalized_real)) != 1:
        return str(peer)

    return normalized_real[0]


__all__ = [
    "MAX_FORWARDED_HEADER_LENGTH",
    "MAX_FORWARDED_HOPS",
    "MAX_REAL_IP_HEADER_LENGTH",
    "IPAddress",
    "IPNetwork",
    "TrustedProxies",
    "extract_trusted_proxies_from_config",
    "parse_trusted_proxies",
    "resolve_client_ip",
]
