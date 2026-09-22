"""The gutter-reading spread detector.

Detection decides whether a whole volume's double-page pairing shifts, so the
tests below check both halves of the contract: it finds a real run of spreads,
and it abstains on everything else rather than guessing.
"""

from __future__ import annotations

import io
import random
from contextlib import contextmanager
from dataclasses import replace

import pytest
from fakes import page, publication
from PIL import Image, ImageDraw

from nineveh.domain import SpreadGuess
from nineveh.spreads import (
    GUTTER_SOURCE,
    WIDE_PAGE_SOURCE,
    LayeredSpreadDetector,
    SeamSpreadDetector,
    WidePageDetector,
    seam_continuity,
)

WIDTH = 100
HEIGHT = 140


def _single() -> Image.Image:
    """A page whose gutter edge is blank margin, like ordinary printed art."""
    image = Image.new("L", (WIDTH, HEIGHT), 255)
    ImageDraw.Draw(image).rectangle([18, 18, WIDTH - 18, HEIGHT - 18], fill=40)
    return image


def _blank() -> Image.Image:
    return Image.new("L", (WIDTH, HEIGHT), 255)


def _spread_halves(seed: int) -> tuple[Image.Image, Image.Image]:
    """One textured picture, split down the middle as a scanner would."""
    full = Image.new("L", (WIDTH * 2, HEIGHT))
    draw = ImageDraw.Draw(full)
    for x in range(WIDTH * 2):
        draw.line([(x, 0), (x, HEIGHT)], fill=int(60 + 120 * (x / (WIDTH * 2))))
    for x in range(0, WIDTH * 2, 4):
        draw.line([(x, 0), (x, HEIGHT)], fill=30 + (seed * 53 + x * 13) % 90)
    noise = random.Random(seed)
    pixels = full.load()
    for _ in range(3000):
        x, y = noise.randrange(WIDTH * 2), noise.randrange(HEIGHT)
        pixels[x, y] = max(0, min(255, pixels[x, y] + noise.randint(-25, 25)))
    return (
        full.crop((0, 0, WIDTH, HEIGHT)),
        full.crop((WIDTH, 0, WIDTH * 2, HEIGHT)),
    )


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _volume(count: int, *, first_spread: int | None, rtl: bool = False) -> dict:
    """Cover, leading singles, then true spreads from `first_spread` onward."""
    images = {number: _png(_single()) for number in range(1, count + 1)}
    if first_spread is None:
        return images
    for seed, number in enumerate(range(first_spread, count, 2)):
        left, right = _spread_halves(seed)
        # A right-to-left scan stores each spread as (right half, left half).
        images[number], images[number + 1] = (
            (_png(right), _png(left)) if rtl else (_png(left), _png(right))
        )
    return images


class StubArchives:
    def __init__(self, images: dict[int, bytes]) -> None:
        self.images = images
        self.opened: list[int] = []

    @contextmanager
    def open_page_file(self, _publication, item):
        self.opened.append(item.number)
        if item.number not in self.images:
            raise OSError("page missing from archive")
        yield io.BytesIO(self.images[item.number])


def _guess(images, count, direction="ltr", *, pixels=10_000_000, pages=None):
    detector = SeamSpreadDetector(StubArchives(images), pixels)
    item = publication("vol", pages=count)
    return detector.detect(
        item, pages or [page(number) for number in range(1, count + 1)], direction
    )


def _detect(images, count, direction="ltr", *, pixels=10_000_000, pages=None):
    return _guess(images, count, direction, pixels=pixels, pages=pages).anchor


def test_it_finds_the_first_page_of_a_run_of_true_spreads():
    assert _detect(_volume(10, first_spread=3), 10) == 3
    assert _detect(_volume(10, first_spread=4), 10) == 4


def test_it_reads_the_gutter_on_the_correct_side_for_right_to_left_scans():
    assert _detect(_volume(10, first_spread=3, rtl=True), 10, "rtl") == 3


def test_a_volume_of_ordinary_single_pages_moves_nothing():
    assert _detect(_volume(10, first_spread=None), 10) is None


def test_blank_margins_meeting_are_not_mistaken_for_a_spread():
    """Two empty pages continue across the seam perfectly, which is exactly
    why continuity alone cannot be the test."""
    assert _detect({number: _png(_blank()) for number in range(1, 11)}, 10) is None


def test_one_continuous_looking_pair_is_not_enough_on_its_own():
    images = _volume(10, first_spread=None)
    left, right = _spread_halves(1)
    images[3], images[4] = _png(left), _png(right)
    assert _detect(images, 10) is None, "a lone pair should not move the volume"


def test_a_spread_run_starting_past_the_search_window_is_left_alone():
    assert _detect(_volume(24, first_spread=12), 24) is None


def test_pages_already_stitched_into_one_wide_image_are_skipped():
    images = _volume(10, first_spread=3)
    images[3] = _png(Image.new("L", (WIDTH * 2, HEIGHT), 128))
    assert _detect(images, 10) in {None, 5}


def test_a_page_marked_as_a_spread_in_comicinfo_is_skipped():
    pages = [replace(page(number), is_spread=number == 3) for number in range(1, 11)]
    assert _detect(_volume(10, first_spread=3), 10, pages=pages) != 3


def test_an_unreadable_page_abstains_instead_of_raising():
    images = _volume(10, first_spread=3)
    del images[4]
    assert _detect(images, 10) in {None, 5, 7}


def test_pages_over_the_decode_budget_are_refused():
    assert _detect(_volume(10, first_spread=3), 10, pixels=100) is None


def test_a_volume_too_short_to_pair_is_skipped_without_reading_it():
    archives = StubArchives(_volume(1, first_spread=None))
    detector = SeamSpreadDetector(archives, 10_000_000)
    assert detector.detect(publication("vol", pages=1), [page(1)], "ltr").anchor is None
    assert archives.opened == []


def test_each_page_is_read_at_most_once_per_volume():
    images = _volume(12, first_spread=None)
    archives = StubArchives(images)
    detector = SeamSpreadDetector(archives, 10_000_000)
    detector.detect(
        publication("vol", pages=12),
        [page(number) for number in range(1, 13)],
        "ltr",
    )
    assert len(archives.opened) == len(set(archives.opened))


def test_a_textured_seam_scores_above_a_blank_one():
    ink = [[0.2 + (row % 7) / 40 for row in range(64)] for _ in range(4)]
    blank = [[1.0] * 64 for _ in range(4)]
    assert seam_continuity(ink, ink) > seam_continuity(blank, blank)


def test_a_hard_step_across_the_seam_scores_nothing():
    dark = [[0.0] * 64 for _ in range(4)]
    light = [[1.0] * 64 for _ in range(4)]
    assert seam_continuity(dark, light) == 0.0


@pytest.mark.parametrize("direction", ["ltr", "rtl"])
def test_detection_is_deterministic(direction):
    images = _volume(10, first_spread=3, rtl=direction == "rtl")
    assert _detect(images, 10, direction) == _detect(images, 10, direction)


# --- WidePageDetector ------------------------------------------------------


def _pages_with_wide(count: int, wide: set[int]):
    return [
        replace(page(number), width=3000 if number in wide else 1500, height=2250)
        for number in range(1, count + 1)
    ]


def test_the_first_stitched_spread_after_the_cover_becomes_the_anchor():
    detector = WidePageDetector()
    item = publication("vol", pages=40)
    assert detector.detect(item, _pages_with_wide(40, {33, 70}), "rtl") == SpreadGuess(
        33, WIDE_PAGE_SOURCE
    )


def test_a_wide_cover_is_not_mistaken_for_an_interior_spread():
    detector = WidePageDetector()
    item = publication("vol", pages=10)
    assert detector.detect(item, _pages_with_wide(10, {1}), "rtl").anchor is None


def test_a_comicinfo_spread_marker_counts_even_without_dimensions():
    detector = WidePageDetector()
    item = publication("vol", pages=10)
    pages = [replace(page(number), is_spread=number == 6) for number in range(1, 11)]
    assert detector.detect(item, pages, "ltr").anchor == 6


def test_a_volume_of_ordinary_pages_yields_no_wide_anchor():
    detector = WidePageDetector()
    item = publication("vol", pages=10)
    assert detector.detect(item, _pages_with_wide(10, set()), "ltr").anchor is None


# --- LayeredSpreadDetector -------------------------------------------------


class StubDetection:
    def __init__(self, anchor, source="stub"):
        self.guess = SpreadGuess(anchor, source if anchor else None)
        self.calls = 0

    def detect(self, _publication, _pages, _direction):
        self.calls += 1
        return self.guess


def test_the_first_detector_with_a_real_answer_wins():
    first, second = StubDetection(7), StubDetection(4)
    assert LayeredSpreadDetector(first, second).detect(None, [], "ltr").anchor == 7
    assert second.calls == 0, "a confident answer must not cost a second read"


def test_a_detector_that_only_confirms_the_default_falls_through():
    """Anchoring on page two is what already happens, so it is not an answer
    that should shadow a detector with a stronger signal."""
    for weak in (None, 2):
        guess = LayeredSpreadDetector(StubDetection(weak), StubDetection(33)).detect(
            None, [], "ltr"
        )
        assert guess.anchor == 33


def test_layering_abstains_when_nothing_has_a_signal():
    guess = LayeredSpreadDetector(StubDetection(None), StubDetection(2)).detect(
        None, [], "ltr"
    )
    assert guess.anchor is None and guess.source is None


def test_each_detector_names_the_evidence_it_used():
    """An administrator judging a suspicious anchor needs to know whether it
    came from reading pixels or from a page that is wide by construction."""
    seam = _guess(_volume(10, first_spread=3), 10)
    assert seam == SpreadGuess(3, GUTTER_SOURCE)

    item = publication("vol", pages=40)
    wide = WidePageDetector().detect(item, _pages_with_wide(40, {33}), "rtl")
    assert wide == SpreadGuess(33, WIDE_PAGE_SOURCE)

    layered = LayeredSpreadDetector(StubDetection(None), WidePageDetector())
    assert layered.detect(item, _pages_with_wide(40, {33}), "rtl").source == (
        WIDE_PAGE_SOURCE
    )


def test_an_abstention_carries_no_source():
    assert _guess(_volume(10, first_spread=None), 10) == SpreadGuess(None, None)
