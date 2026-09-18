"""Byte formatting for the administration console. Pure and dependency-free."""

from __future__ import annotations

GIB = 1024**3
_BINARY_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def gibibytes(value: int) -> str:
    """Capacity as GiB, the one unit the whole console reports in.

    Values too small to round to 0.01 GiB are reported as such rather than
    clamped up to it — a 50 KiB series and a 10 MiB series are not the same
    size, and claiming both are "0.01 GiB" is simply untrue. Exact byte counts
    stay available in the surrounding markup.
    """
    if value <= 0:
        return "0 GiB"
    amount = value / GIB
    return f"{amount:.2f} GiB" if amount >= 0.005 else "< 0.01 GiB"


def binary_size(value: int) -> str:
    """A byte count in whichever binary unit keeps it readable."""
    amount = float(value)
    for unit in _BINARY_UNITS[:-1]:
        if amount < 1024:
            return f"{value} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{amount:.2f} {_BINARY_UNITS[-1]}"
