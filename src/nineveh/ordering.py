"""Reading order for a series' volumes, as a tuple and as a stored string.

The series page sorts volumes in Python; SQLite picks the volume a series'
cover comes from. Both must agree on which volume is first, so the tuple key
has a string twin whose byte order is the same, and that string is stored with
each publication for SQL to sort on.
"""

from __future__ import annotations

import re

from .domain import Publication

_NATURAL_PARTS = re.compile(r"(\d+)")
# Below every character a title or file name contains, and ordered so that a
# shorter key sorts first: a text part ends with _TEXT_END, a key with _KEY_END.
_TEXT_END = "\x01"
_KEY_END = "\x02"
_DIGITS = 30


def publication_order_key(publication: Publication) -> tuple[object, ...]:
    """Sort numbered issues naturally, with deterministic fallbacks."""
    return _order(
        publication.number, publication.title, publication.filename, publication.id
    )


def publication_sort_key(
    number: str | None, title: str, filename: str, identifier: str
) -> str:
    """`publication_order_key` as a string that sorts identically byte by byte."""
    numbered, primary, title_key, filename_key, _ = _order(
        number, title, filename, identifier
    )
    return "".join(
        (
            str(numbered),
            *(_encode(key) for key in (primary, title_key, filename_key)),
            identifier,
        )
    )


def natural_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_PARTS.split(value)
        if part
    )


def _order(
    number: str | None, title: str, filename: str, identifier: str
) -> tuple[object, ...]:
    primary = number or title or filename
    return (
        0 if number else 1,
        natural_key(primary),
        natural_key(title),
        natural_key(filename),
        identifier,
    )


def _encode(key: tuple[tuple[int, object], ...]) -> str:
    parts = (
        f"0{value:0{_DIGITS}d}" if kind == 0 else f"1{value}{_TEXT_END}"
        for kind, value in key
    )
    return "".join(parts) + _KEY_END
