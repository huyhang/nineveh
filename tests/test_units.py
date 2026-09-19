from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nineveh.units import binary_size, gibibytes, since, timestamp

NOW = datetime(2026, 9, 19, 1, 11, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0 GiB"), (-1, "0 GiB"), (1024**3, "1.00 GiB"), (50 * 1024, "< 0.01 GiB")],
)
def test_gibibytes(value: int, expected: str):
    assert gibibytes(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(512, "512 B"), (2048, "2.00 KiB"), (5 * 1024**4, "5.00 TiB")],
)
def test_binary_size(value: int, expected: str):
    assert binary_size(value) == expected


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (timedelta(seconds=3), "just now"),
        (timedelta(seconds=45), "45 seconds ago"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=4), "4 minutes ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(hours=2), "2 hours ago"),
        (timedelta(days=1), "1 day ago"),
        (timedelta(days=3), "3 days ago"),
    ],
)
def test_since_reads_as_a_phrase(elapsed: timedelta, expected: str):
    assert since((NOW - elapsed).isoformat(), NOW) == expected


def test_since_falls_back_to_a_date_once_a_relative_age_stops_helping():
    assert (
        since((NOW - timedelta(days=30)).isoformat(), NOW) == "20 Aug 2026, 01:11 UTC"
    )


def test_a_timestamp_from_the_future_does_not_count_backwards():
    """Writer and reader clocks drift; "-2 minutes ago" is never right."""
    assert since((NOW + timedelta(seconds=30)).isoformat(), NOW) == "just now"


def test_a_naive_timestamp_is_read_as_utc():
    # Deliberately tz-naive; built via fromisoformat so the intent is explicit
    # rather than looking like a forgotten tzinfo argument.
    naive = datetime.fromisoformat("2026-09-19T01:07:05")
    assert naive.tzinfo is None
    assert timestamp(naive) == "19 Sep 2026, 01:07 UTC"


def test_the_stored_value_survives_a_shape_this_release_cannot_parse():
    """A status line must not 500 because a timestamp looks unfamiliar."""
    assert since("not a timestamp", NOW) == "not a timestamp"
    assert timestamp("not a timestamp") == "not a timestamp"
    assert since(None, NOW) == ""
    assert timestamp(None) == ""


def test_the_iso_string_the_scanner_stores_renders_readably():
    assert timestamp("2026-09-19T01:07:05.943821+00:00") == "19 Sep 2026, 01:07 UTC"
