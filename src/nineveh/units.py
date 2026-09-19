"""Value formatting for the administration console. Pure and dependency-free."""

from __future__ import annotations

from datetime import UTC, datetime

GIB = 1024**3
_BINARY_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_AGES = ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second"))


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


def _as_datetime(value: str | datetime | None) -> datetime | None:
    """Accept either a stored ISO string or an already-parsed datetime."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def timestamp(value: str | datetime | None) -> str:
    """An absolute UTC stamp, or the original text when it will not parse.

    A status line is the wrong place to raise: showing the stored value beats
    a 500 when a timestamp arrives in a shape this release did not expect.
    """
    moment = _as_datetime(value)
    if moment is None:
        return str(value) if value else ""
    return moment.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def since(value: str | datetime | None, now: datetime | None = None) -> str:
    """How long ago, in the largest unit that still says something useful.

    Falls back to the absolute stamp past a week, where "43 days ago" stops
    being easier to read than the date itself.
    """
    moment = _as_datetime(value)
    if moment is None:
        return timestamp(value)
    elapsed = ((now or datetime.now(UTC)) - moment).total_seconds()
    if elapsed >= 7 * 86400:
        return timestamp(moment)
    # Clock skew between the writer and the reader must not print "-2 minutes".
    if elapsed < 10:
        return "just now"
    # Past that floor the one-second bucket always matches, so there is no
    # fallthrough to write.
    seconds, unit = next(pair for pair in _AGES if elapsed >= pair[0])
    count = int(elapsed // seconds)
    return f"{count} {unit}{'s' if count != 1 else ''} ago"
