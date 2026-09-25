"""The reader's pure model, executed as JavaScript.

`reader-model.js` holds the rules that decide what a reader actually sees:
which pages share a spread, which page a turn lands on, and which images the
continuous window keeps alive. Those rules used to be reachable only from a
browser, so a regression in them passed every check the project had. Running
the real file under an embedded engine keeps them honest from `pytest`.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import dukpy
import pytest

MODEL = Path(__file__).resolve().parent.parent / "src/nineveh/static/reader-model.js"
PORTRAIT = (1500, 2250)
# Matches the archives in `example-data`: a printed double spread is stored as
# one image twice as wide as the pages around it.
WIDE = (3000, 2250)


@cache
def _module() -> str:
    """The shipped module, with its ES exports stripped for a bare engine."""
    source = MODEL.read_text(encoding="utf-8")
    return source.replace("export function ", "function ").replace(
        "export const ", "const "
    )


def evaluate(expression: str, **names: object):
    return dukpy.evaljs(f"{_module()}\n;({expression});", **names)


def page(number: int, *, wide: bool = False, spread: bool = False) -> dict:
    width, height = WIDE if wide else PORTRAIT
    return {"number": number, "width": width, "height": height, "spread": spread}


def volume(count: int, *, wide: set[int] | None = None) -> list[dict]:
    wide = wide or set()
    return [page(number, wide=number in wide) for number in range(1, count + 1)]


def grouped(pages: list[dict], anchor: int | None = None) -> list[list[int]]:
    return evaluate(
        "pageGroups(dukpy['pages'], dukpy['anchor']).map(function (group) {"
        "  return group.map(function (item) { return item.number; });"
        "})",
        pages=pages,
        anchor=anchor,
    )


def test_without_an_anchor_the_cover_stands_alone_and_the_rest_pair():
    assert grouped(volume(7)) == [[1], [2, 3], [4, 5], [6, 7]]


def test_a_stitched_page_stands_alone_and_repairs_the_pages_after_it():
    assert grouped(volume(7, wide={4})) == [[1], [2, 3], [4], [5, 6], [7]]


@pytest.mark.parametrize("anchor", [None, 0, 1, 2, "", "not a page"])
def test_an_absent_or_meaningless_anchor_leaves_pairing_at_the_cover(anchor):
    """Anchors arrive from a manifest, so every junk value must be inert."""
    assert grouped(volume(7), anchor) == grouped(volume(7))


def test_an_anchor_shifts_the_parity_rather_than_unpairing_what_precedes_it():
    # One stray page after the cover is exactly the case the anchor exists for:
    # without it, every later spread shows two halves that do not belong.
    assert grouped(volume(8), 3) == [[1], [2], [3, 4], [5, 6], [7, 8]]
    # An even run before the anchor still pairs -- it just has to land on it.
    assert grouped(volume(8), 4) == [[1], [2, 3], [4, 5], [6, 7], [8]]
    assert grouped(volume(9), 5) == [[1], [2], [3, 4], [5, 6], [7, 8], [9]]


def test_a_late_anchor_leaves_one_single_page_not_a_wall_of_them():
    """`Ragna Crimson v01` has its first printed spread at page 33. Pairing
    every page before that singly would be unreadable; the volume needs one
    stray page after the cover and correct pairs all the way up."""
    groups = grouped(volume(40, wide={33}), 33)
    assert groups[:4] == [[1], [2], [3, 4], [5, 6]]
    assert [33] in groups, "the printed spread still stands alone"
    assert groups[groups.index([33]) - 1] == [31, 32]
    before = groups[: groups.index([33])]
    singles = [group for group in before if len(group) == 1]
    assert singles == [[1], [2]], (
        f"unexpected single pages before the spread: {singles}"
    )


def test_the_cover_never_pairs_however_the_anchor_falls():
    """The regression this file was written for: an even run of leading pages
    used to make the cover the left half of the first spread."""
    for count in range(2, 13):
        for wide in ({}, {3}, {5}, {2, 7}):
            for anchor in range(2, count + 1):
                first = grouped(volume(count, wide=set(wide)), anchor)[0]
                assert first == [1], (
                    f"cover paired at anchor {anchor} of {count} pages, wide={wide}"
                )


def test_a_real_volume_keeps_its_cover_separate_from_page_two():
    """`7thGARDEN v01` shipped a stitched page at 3, and detection anchoring
    there used to pair the cover with page 2 in the live reader."""
    assert grouped(volume(9, wide={3}), 3) == [[1], [2], [3], [4, 5], [6, 7], [8, 9]]


def test_an_anchor_past_the_last_page_still_pairs_what_it_has():
    """An anchor can outlive its volume; the pages that exist must still read."""
    assert grouped(volume(5), 9) == [[1], [2, 3], [4, 5]]
    assert grouped(volume(6), 9) == [[1], [2], [3, 4], [5, 6]]


def test_stitched_pages_still_stand_alone_on_either_side_of_an_anchor():
    assert grouped(volume(8, wide={5}), 3) == [[1], [2], [3, 4], [5], [6, 7], [8]]
    # A wide page inside the leading run breaks the backwards pairing too.
    assert grouped(volume(9, wide={3}), 6) == [[1], [2], [3], [4, 5], [6, 7], [8, 9]]


def test_pages_arrive_in_any_order_and_are_paired_by_number():
    shuffled = list(reversed(volume(6)))
    assert grouped(shuffled, 3) == [[1], [2], [3, 4], [5, 6]]


def test_an_empty_volume_has_no_groups():
    assert grouped([]) == []


def test_visible_pages_follow_the_anchor_and_collapse_when_adaptive():
    pages = volume(8)
    assert evaluate(
        "visiblePages('double', dukpy['pages'], 4, false, 3)"
        ".map(function (item) { return item.number; })",
        pages=pages,
    ) == [3, 4]
    assert evaluate(
        "visiblePages('double', dukpy['pages'], 4, true, 3)"
        ".map(function (item) { return item.number; })",
        pages=pages,
    ) == [4]
    assert evaluate(
        "visiblePages('single', dukpy['pages'], 4, false, 3)"
        ".map(function (item) { return item.number; })",
        pages=pages,
    ) == [4]


def test_turning_a_page_in_double_mode_steps_whole_spreads():
    pages = volume(8)
    step = "adjacentPage('double', dukpy['pages'], dukpy['from'], dukpy['by'], false, 8, 3)"
    assert evaluate(step, pages=pages, **{"from": 1, "by": 1}) == 2
    assert evaluate(step, pages=pages, **{"from": 2, "by": 1}) == 3
    assert evaluate(step, pages=pages, **{"from": 3, "by": 1}) == 5
    assert evaluate(step, pages=pages, **{"from": 5, "by": -1}) == 3
    assert evaluate(step, pages=pages, **{"from": 1, "by": -1}) is None
    assert evaluate(step, pages=pages, **{"from": 7, "by": 1}) is None


def test_every_page_of_a_volume_belongs_to_exactly_one_group():
    """Pairing must partition the volume: a dropped or duplicated page would
    make a spread unreachable by turning."""
    for anchor in [None, 2, 3, 4, 7]:
        for pages in (volume(9), volume(9, wide={3, 8}), volume(10, wide={2})):
            numbers = [n for group in grouped(pages, anchor) for n in group]
            assert numbers == sorted(numbers)
            assert numbers == [item["number"] for item in pages], (
                f"anchor {anchor} lost or repeated a page"
            )


def test_the_continuous_window_tracks_the_reader_and_stays_inside_the_volume():
    window = "Array.from(activeImageNumbers(dukpy['page'], dukpy['total'])).sort("
    window += "function (a, b) { return a - b; })"
    assert evaluate(window, page=10, total=200) == [8, 9, 10, 11, 12, 13]
    assert evaluate(window, page=1, total=200) == [1, 2, 3, 4]
    assert evaluate(window, page=200, total=200) == [198, 199, 200]
    assert evaluate(window, page=1, total=1) == [1]


def test_the_continuous_window_is_bounded_however_far_the_reader_jumps():
    for target in (-5, 0, 1, 50, 400, 10_000):
        numbers = evaluate(
            "Array.from(activeImageNumbers(dukpy['page'], dukpy['total']))",
            page=target,
            total=300,
        )
        assert numbers, f"no window at page {target}"
        assert len(numbers) <= 6
        assert all(1 <= number <= 300 for number in numbers)


def test_the_module_stays_free_of_browser_globals():
    """The model is imported by the reader *and* by this engine; a stray
    `document` or `window` reference would only fail in one of them."""
    source = MODEL.read_text(encoding="utf-8")
    for global_name in ("document.", "window.", "fetch("):
        assert global_name not in source, f"{global_name} leaked into the model"


def test_reading_mode_storage_is_isolated_by_user_and_series():
    assert evaluate("readerModeStorageKey('reader-1', 'series-2')") == (
        "nineveh-reader-mode:reader-1:series-2"
    )


def test_the_reader_passes_the_manifest_anchor_into_every_pairing_call():
    """`reader.js` owns the anchor; the model only honours what it is given."""
    reader = (MODEL.parent / "reader.js").read_text(encoding="utf-8")
    assert "this.pairingAnchor" in reader
    assert json.dumps("pairingAnchor") in reader
