"""Find the page where true double-page spreads begin in a volume.

Double-page mode pairs sequentially from the cover, so a single stray page in
the front matter shifts every later spread: the reader sees the right half of
one spread beside the left half of the next. Page dimensions cannot reveal
that — a leading single and half a spread are the same shape — so detection
reads the printed gutter instead. Two halves of one spread continue across the
seam; two unrelated pages meet margin to margin.

Detection abstains whenever the evidence is weak. An abstaining volume keeps
today's pairing, and an administrator can always pin the start page by hand.
"""

from __future__ import annotations

import logging
from statistics import mean
from typing import Protocol

from PIL import Image

from .domain import Page, Publication, SpreadGuess
from .ports import ArchiveSource

LOGGER = logging.getLogger(__name__)

# Mirrors `isStitched` in reader-model.js: an already-stitched spread stands
# alone in the reader, so it carries no pairing signal and is skipped here.
STITCHED_ASPECT = 1.25
# The seam is sampled as a few columns of a fixed-height grayscale strip, which
# keeps the cost independent of the scan's resolution.
STRIP_HEIGHT = 64
STRIP_WIDTH = 4
# Front matter longer than this is rarer than the false positives a wider
# search would invite; the manual override covers those volumes.
FIRST_CANDIDATE = 2
LAST_CANDIDATE = 8
# How far the printed image may jump across the seam before the pair is read as
# two separate pages meeting rather than one picture continuing.
STEP_CEILING = 0.2
# Two blank margins also meet smoothly, so continuity alone is not enough:
# there has to be ink varying across the seam.
SPREAD_FLOOR = 0.0005
# A lone continuous-looking pair can be two full-bleed singles meeting by
# chance, so a candidate only counts once a later pair agrees with it.
CONFIRMATION_OFFSETS = (2, 4)

Strips = tuple[list[list[float]], list[list[float]]]


# Recorded with each anchor so the admin panel can name the evidence.
GUTTER_SOURCE = "gutter"
WIDE_PAGE_SOURCE = "wide page"


class SpreadDetection(Protocol):
    def detect(
        self, publication: Publication, pages: list[Page], direction: str
    ) -> SpreadGuess: ...


class SeamSpreadDetector:
    """Locate the first true spread by reading the gutter between pages."""

    def __init__(self, archives: ArchiveSource, max_image_pixels: int) -> None:
        self._archives = archives
        self._max_image_pixels = max_image_pixels

    def detect(
        self, publication: Publication, pages: list[Page], direction: str
    ) -> SpreadGuess:
        """The first page that starts a run of true spreads, or nothing."""
        by_number = {page.number: page for page in pages}
        cache: dict[int, Strips | None] = {}
        last = min(LAST_CANDIDATE, publication.page_count - 1)
        for start in range(FIRST_CANDIDATE, last + 1):
            if not self._continuous(publication, by_number, start, direction, cache):
                continue
            if self._confirmed(publication, by_number, start, direction, cache):
                return SpreadGuess(start, GUTTER_SOURCE)
        return SpreadGuess.none()

    def _confirmed(
        self,
        publication: Publication,
        by_number: dict[int, Page],
        start: int,
        direction: str,
        cache: dict[int, Strips | None],
    ) -> bool:
        return any(
            self._continuous(publication, by_number, start + offset, direction, cache)
            for offset in CONFIRMATION_OFFSETS
            if start + offset + 1 <= publication.page_count
        )

    def _continuous(
        self,
        publication: Publication,
        by_number: dict[int, Page],
        left: int,
        direction: str,
        cache: dict[int, Strips | None],
    ) -> bool:
        score = self._seam_score(publication, by_number, left, direction, cache)
        return score is not None and score >= SPREAD_FLOOR

    def _seam_score(
        self,
        publication: Publication,
        by_number: dict[int, Page],
        left: int,
        direction: str,
        cache: dict[int, Strips | None],
    ) -> float | None:
        first, second = by_number.get(left), by_number.get(left + 1)
        if first is None or second is None or first.is_spread or second.is_spread:
            return None
        outer = self._strips(publication, first, cache)
        inner = self._strips(publication, second, cache)
        if outer is None or inner is None:
            return None
        # Right-to-left scans store each spread as (right half, left half), so
        # the gutter sits on the opposite edge of both files.
        if direction == "rtl":
            return seam_continuity(outer[0], inner[1])
        return seam_continuity(outer[1], inner[0])

    def _strips(
        self, publication: Publication, page: Page, cache: dict[int, Strips | None]
    ) -> Strips | None:
        if page.number not in cache:
            cache[page.number] = self._read_strips(publication, page)
        return cache[page.number]

    def _read_strips(self, publication: Publication, page: Page) -> Strips | None:
        """Grayscale edge columns at a fixed height, or None when unusable."""
        try:
            with (
                self._archives.open_page_file(publication, page) as source,
                Image.open(source) as opened,
            ):
                width, height = opened.size
                if not self._usable(width, height):
                    return None
                gray = opened.convert("L").resize(
                    (max(1, round(width * STRIP_HEIGHT / height)), STRIP_HEIGHT),
                    Image.BILINEAR,
                )
                pixels = gray.load()
                sampled = gray.size[0]
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            LOGGER.warning(
                "Spread detection skipped %s page %d: %s",
                publication.id,
                page.number,
                error,
            )
            return None
        return (
            _edge(pixels, sampled, STRIP_HEIGHT, from_left=True),
            _edge(pixels, sampled, STRIP_HEIGHT, from_left=False),
        )

    def _usable(self, width: int, height: int) -> bool:
        """Big enough to sample, small enough to decode, not already stitched."""
        if width <= 0 or height <= 0 or width * height > self._max_image_pixels:
            return False
        return width / height < STITCHED_ASPECT


class WidePageDetector:
    """Anchor on the first spread the scanner left stitched into one image.

    A printed spread that survived as a single wide file takes one slot in the
    reader, so the pages between it and the cover have to pair evenly to reach
    it. Anchoring there lets the pairing align backwards from a page we know is
    a real spread boundary, which is a far stronger signal than any guess about
    the front matter — and it costs nothing but the dimensions already indexed.
    """

    def detect(
        self, publication: Publication, pages: list[Page], _direction: str
    ) -> SpreadGuess:
        for page in pages:
            if page.number <= publication.cover_page:
                continue
            if page.is_spread or _is_wide(page):
                return SpreadGuess(page.number, WIDE_PAGE_SOURCE)
        return SpreadGuess.none()


class LayeredSpreadDetector:
    """Read the gutter first; fall back to the first stitched spread."""

    def __init__(self, *detectors: SpreadDetection) -> None:
        self._detectors = detectors

    def detect(
        self, publication: Publication, pages: list[Page], direction: str
    ) -> SpreadGuess:
        for detector in self._detectors:
            guess = detector.detect(publication, pages, direction)
            # An anchor of two is the default pairing, so it tells us nothing a
            # later detector could not improve on.
            if guess.anchor is not None and guess.anchor > FIRST_CANDIDATE:
                return guess
        return SpreadGuess.none()


def _is_wide(page: Page) -> bool:
    if not page.width or not page.height:
        return False
    return page.width / page.height >= STITCHED_ASPECT


def _edge(pixels, width: int, height: int, *, from_left: bool) -> list[list[float]]:
    """Edge columns as normalized luminance, seam-adjacent column first."""
    columns = range(min(STRIP_WIDTH, width))
    return [
        [
            pixels[column if from_left else width - 1 - column, row] / 255
            for row in range(height)
        ]
        for column in columns
    ]


def seam_continuity(first: list[list[float]], second: list[list[float]]) -> float:
    """How much printed detail runs across the seam between two page edges.

    Each argument holds one page's edge columns on its gutter side, closest
    column first. A true spread carries textured ink through the seam; a sharp
    step where content meets blank paper scores nothing, and two blank margins
    meeting score almost nothing.
    """
    samples = [value for column in (*first, *second) for value in column]
    average = sum(samples) / len(samples)
    variance = sum((value - average) ** 2 for value in samples) / len(samples)
    step = mean(abs(left - right) for left, right in zip(first[0], second[0]))
    return 0.0 if step > STEP_CEILING else variance
