"""Facts about the container Nineveh is running inside.

These are read-only: the memory ceiling is fixed when Docker creates the
container, so Nineveh can report it but never change it. Paths are arguments so
the detection is testable without a cgroup filesystem.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from pathlib import Path

from .units import binary_size

CGROUP_V2_LIMIT = Path("/sys/fs/cgroup/memory.max")
CGROUP_V1_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
ROUTE_TABLE = Path("/proc/net/route")

# cgroup v1 spells "unlimited" as a near-word-size sentinel rather than a word.
UNLIMITED_FLOOR = 1 << 62


def memory_limit_bytes(
    v2_path: Path = CGROUP_V2_LIMIT, v1_path: Path = CGROUP_V1_LIMIT
) -> int | None:
    """The enforced memory ceiling, or None when unlimited or undetectable."""
    for path in (v2_path, v1_path):
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        return None if value >= UNLIMITED_FLOOR else value
    return None


def memory_limit_text(configured: str | None = None, **paths: Path) -> str:
    """What to show an administrator, preferring what the host actually enforces.

    A detected cgroup ceiling beats the declared `NINEVEH_MEMORY_LIMIT`, because
    the declared value is only a Compose variable and can drift from the limit
    the container was created with.
    """
    detected = memory_limit_bytes(**paths)
    if detected is not None:
        return binary_size(detected)
    if configured:
        return f"{configured} (declared; not enforced in this environment)"
    return "Unlimited or not detected"


def default_gateway(route_table: Path = ROUTE_TABLE) -> str | None:
    """The address this container reaches the host through, or None off Linux.

    Read from the kernel rather than shelled out to `ip`, which the runtime
    image does not ship.
    """
    try:
        rows = route_table.read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return None
    for row in rows:
        fields = row.split()
        if len(fields) > 2 and fields[1] == "00000000":
            try:
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
            except (ValueError, struct.error, OSError):
                return None
    return None


def trusts(configured: str, address: str) -> bool:
    """Whether uvicorn's forwarded-allow-ips value covers `address`."""
    if configured.strip() == "*":
        return True
    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    for raw in configured.split(","):
        entry = raw.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if candidate in ipaddress.ip_network(entry, strict=False):
                    return True
            elif candidate == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def proxy_trust_advice(
    configured: str,
    gateway: str | None = None,
    route_table: Path = ROUTE_TABLE,
) -> str | None:
    """A warning when the host-facing gateway would not be believed.

    Advisory only. The gateway is where a proxy on the host arrives from under
    Linux Docker; other setups route differently, so a clean result here is not
    a guarantee -- `untrusted_proxy` reports what actually happened.

    `gateway=None` means "detect it", so `route_table` is an argument for the
    same reason the cgroup paths are: without it the only way to exercise
    detection is to inherit the host's real routing table, which makes the
    outcome depend on whether the tests run on Linux.
    """
    detected = default_gateway(route_table) if gateway is None else gateway
    if detected is None or trusts(configured, detected):
        return None
    return (
        f"Forwarded headers from the container gateway {detected} will be ignored: "
        f"NINEVEH_FORWARDED_ALLOW_IPS is {configured!r}. A reverse proxy on the host "
        f"reaches Nineveh from that address, so X-Forwarded-Proto is discarded and "
        f"HSTS is never sent."
    )


def discarded_forwarded_proto(header: str | None, scheme: str) -> bool:
    """True when a proxy asked for https and uvicorn declined to believe it.

    Uvicorn rewrites the scheme only for a trusted peer, so the header saying
    one thing while the scheme says another is an exact, false-positive-free
    signal that the peer is not in the trusted list.
    """
    if not header:
        return False
    claimed = header.split(",")[0].strip()
    return claimed in {"https", "wss"} and scheme in {"http", "ws"}
