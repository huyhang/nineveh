"""Byte formatting and container introspection: pure, no service required."""

from __future__ import annotations

from pathlib import Path

import pytest

from nineveh.deployment import (
    UNLIMITED_FLOOR,
    default_gateway,
    discarded_forwarded_proto,
    memory_limit_bytes,
    memory_limit_text,
    proxy_trust_advice,
    trusts,
)
from nineveh.units import binary_size, gibibytes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 GiB"),
        (1024, "< 0.01 GiB"),
        (50 * 1024, "< 0.01 GiB"),
        (10 * 1024**2, "0.01 GiB"),
        (240 * 1024**2, "0.23 GiB"),
        (3 * 1024**3, "3.00 GiB"),
    ],
)
def test_capacity_never_rounds_a_small_library_up_to_a_tenth_of_a_gigabyte(
    value: int, expected: str
):
    assert gibibytes(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (512, "512 B"),
        (2048, "2.00 KiB"),
        (1536 * 1024**2, "1.50 GiB"),
        (3 * 1024**5, "3.00 PiB"),
        (4096 * 1024**5, "4096.00 PiB"),
    ],
)
def test_binary_size_picks_a_readable_unit(value: int, expected: str):
    assert binary_size(value) == expected


def test_the_cgroup_v2_ceiling_wins(tmp_path: Path):
    v2 = tmp_path / "memory.max"
    v2.write_text("1073741824", encoding="utf-8")
    v1 = tmp_path / "limit_in_bytes"
    v1.write_text("536870912", encoding="utf-8")
    assert memory_limit_bytes(v2, v1) == 1073741824


def test_detection_falls_back_to_cgroup_v1(tmp_path: Path):
    v1 = tmp_path / "limit_in_bytes"
    v1.write_text("536870912\n", encoding="utf-8")
    assert memory_limit_bytes(tmp_path / "absent", v1) == 536870912


@pytest.mark.parametrize("raw", ["max", str(UNLIMITED_FLOOR), "not-a-number"])
def test_an_unlimited_or_unreadable_ceiling_reports_nothing(tmp_path: Path, raw: str):
    """cgroup v1 spells "unlimited" as a huge sentinel, not the word."""
    v2 = tmp_path / "memory.max"
    v2.write_text(raw, encoding="utf-8")
    assert memory_limit_bytes(v2, tmp_path / "absent") is None


def test_missing_cgroup_files_report_nothing(tmp_path: Path):
    assert memory_limit_bytes(tmp_path / "a", tmp_path / "b") is None


def test_the_displayed_ceiling_prefers_what_the_host_enforces(tmp_path: Path):
    v2 = tmp_path / "memory.max"
    v2.write_text("2147483648", encoding="utf-8")
    assert (
        memory_limit_text("1g", v2_path=v2, v1_path=tmp_path / "absent") == "2.00 GiB"
    )


def test_a_declared_ceiling_is_flagged_when_nothing_enforces_it(tmp_path: Path):
    text = memory_limit_text("1g", v2_path=tmp_path / "a", v1_path=tmp_path / "b")
    assert text == "1g (declared; not enforced in this environment)"


def test_no_ceiling_at_all_says_so(tmp_path: Path):
    text = memory_limit_text(None, v2_path=tmp_path / "a", v1_path=tmp_path / "b")
    assert text == "Unlimited or not detected"


@pytest.mark.parametrize(
    ("configured", "address", "trusted"),
    [
        ("127.0.0.1", "127.0.0.1", True),
        ("127.0.0.1", "172.19.0.1", False),
        ("172.19.0.0/16", "172.19.0.1", True),
        ("172.17.0.0/16", "172.19.0.1", False),
        ("127.0.0.1, 172.19.0.1", "172.19.0.1", True),
        ("*", "203.0.113.9", True),
        ("127.0.0.1", "not-an-address", False),
        ("nonsense", "172.19.0.1", False),
    ],
)
def test_trusted_peer_matching_mirrors_uvicorn(
    configured: str, address: str, trusted: bool
):
    assert trusts(configured, address) is trusted


def test_the_default_gateway_is_decoded_from_the_kernel_route_table(tmp_path: Path):
    """The runtime image has no `ip` command, so /proc is the only source."""
    table = tmp_path / "route"
    table.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t00000000\t010013AC\t0003\t0\t0\t0\t00000000\n"
        "eth0\t000013AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n",
        encoding="utf-8",
    )
    assert default_gateway(table) == "172.19.0.1"


def test_a_route_table_without_a_default_route_reports_nothing(tmp_path: Path):
    table = tmp_path / "route"
    table.write_text(
        "Iface\tDestination\tGateway\neth0\t000013AC\t00000000\n", encoding="utf-8"
    )
    assert default_gateway(table) is None


def test_an_absent_route_table_reports_nothing(tmp_path: Path):
    assert default_gateway(tmp_path / "absent") is None


def test_advice_is_silent_when_the_gateway_is_already_trusted():
    assert proxy_trust_advice("172.19.0.0/16", gateway="172.19.0.1") is None
    assert proxy_trust_advice("127.0.0.1", gateway=None) is None


def test_advice_names_the_gateway_and_the_current_value():
    advice = proxy_trust_advice("127.0.0.1", gateway="172.19.0.1")
    assert advice is not None
    assert "172.19.0.1" in advice and "127.0.0.1" in advice


@pytest.mark.parametrize(
    ("header", "scheme", "discarded"),
    [
        ("https", "http", True),
        ("https, http", "http", True),
        ("wss", "ws", True),
        ("https", "https", False),
        ("http", "http", False),
        (None, "http", False),
        ("", "http", False),
    ],
)
def test_a_discarded_forwarded_proto_is_detected_exactly(
    header: str | None, scheme: str, discarded: bool
):
    assert discarded_forwarded_proto(header, scheme) is discarded
