from __future__ import annotations

import re
from dataclasses import replace
from typing import Protocol

from .domain import (
    Page,
    Publication,
    ReaderContext,
    ReadingProgress,
    ReadingState,
    ReadScope,
    SpreadAnalysis,
    SpreadGuess,
)
from .ports import ArchiveSource, CatalogRepository, ReadingRepository, SpreadRepository

READING_MODES = frozenset({"single", "double", "scroll"})
_NATURAL_PARTS = re.compile(r"(\d+)")
# The cover always stands alone, so the earliest page pairing can start at.
FIRST_PAIRED_PAGE = 2


class SpreadCatalogRepository(CatalogRepository, SpreadRepository, Protocol):
    pass


class SpreadDetector(Protocol):
    def detect(
        self, publication: Publication, pages: list[Page], direction: str
    ) -> SpreadGuess: ...


class SpreadDetectionService:
    """Keep each volume's double-page alignment stored and current."""

    def __init__(
        self,
        repository: SpreadCatalogRepository,
        archives: ArchiveSource,
        detector: SpreadDetector,
    ) -> None:
        self._repository = repository
        self._archives = archives
        self._detector = detector

    def analyze_series(self, series_id: str, *, force: bool = False) -> None:
        for publication in self._repository.publications_in_series(series_id):
            self.analyze_publication(publication, force=force)

    def analyze_publication(
        self, publication: Publication, *, force: bool = False
    ) -> SpreadAnalysis:
        current = self._repository.publication_spread_analysis(
            publication.id, publication.revision
        )
        if current and not force:
            return current
        pages = self._measured_pages(publication)
        guess = self._detector.detect(
            publication, pages, reading_direction(publication.category)
        )
        return self._repository.save_publication_spread_analysis(
            publication.id, publication.revision, guess.anchor, guess.source
        )

    def _measured_pages(self, publication: Publication) -> list[Page]:
        """Stored pages, filling in any dimensions nobody has needed yet.

        The reader sizes its scroll placeholders from these, so measuring them
        here means a volume never reflows the first time it is opened.
        """
        pages = self._repository.pages(publication.id, 1, publication.page_count)
        missing = [page for page in pages if page.width is None or page.height is None]
        measured = (
            self._archives.page_dimensions_many(publication, missing) if missing else {}
        )
        if not measured:
            return pages
        self._repository.update_page_dimensions(
            publication.id,
            [(number, width, height) for number, (width, height) in measured.items()],
        )
        return [
            replace(
                page, width=measured[page.number][0], height=measured[page.number][1]
            )
            if page.number in measured
            else page
            for page in pages
        ]

    def anchor_for(self, publication: Publication) -> int | None:
        """Where double-page pairing starts, honouring an admin override."""
        if not publication.series_id or not self._repository.series_spread_detection(
            publication.series_id
        ):
            return None
        override = self._repository.publication_spread_override(publication.id)
        if override is not None:
            return clamp_anchor(override, publication.page_count)
        detected = self.analyze_publication(publication).anchor_page
        return clamp_anchor(detected, publication.page_count)


def clamp_anchor(anchor: int | None, page_count: int) -> int | None:
    """Keep a stored anchor inside the volume it belongs to.

    Archives are re-scanned and hand-typed page numbers are guesses, so an
    anchor can outlive the volume it was chosen for. Pairing always starts at
    page two or later, since the cover stands alone.
    """
    if anchor is None or page_count < FIRST_PAIRED_PAGE:
        return None
    return min(max(anchor, FIRST_PAIRED_PAGE), page_count)


class ReaderService:
    """Build reader state without coupling the HTTP layer to persistence."""

    def __init__(self, catalog: CatalogRepository, progress: ReadingRepository) -> None:
        self._catalog = catalog
        self._progress = progress

    def publication(self, publication_id: str, scope: ReadScope) -> Publication | None:
        """The authorized publication, without its series neighbours."""
        return self._catalog.publication_by_id(publication_id, scope)

    def context(self, publication_id: str, scope: ReadScope) -> ReaderContext | None:
        publication = self._catalog.publication_by_id(publication_id, scope)
        if not publication or not publication.series_id:
            return None
        publications = self.series_publications(publication.series_id, scope)
        try:
            position = next(
                index
                for index, item in enumerate(publications)
                if item.id == publication.id
            )
        except StopIteration:
            return None
        return ReaderContext(
            publication=publication,
            publications=tuple(publications),
            position=position,
            previous=publications[position - 1] if position else None,
            next=publications[position + 1]
            if position + 1 < len(publications)
            else None,
        )

    def series_publications(
        self, series_id: str, scope: ReadScope
    ) -> list[Publication]:
        return sorted(
            self._catalog.publications_in_series(series_id, scope),
            key=publication_order_key,
        )

    def progress_for_publications(
        self, user_id: str, publications: list[Publication]
    ) -> dict[str, ReadingProgress]:
        return self._progress.reading_progress_for_publications(
            user_id, [publication.id for publication in publications]
        )

    def reading_state(self, user_id: str, publication_id: str) -> ReadingState:
        saved = self._progress.reading_progress(user_id, publication_id)
        preference = self._progress.latest_reading_progress(user_id)
        return ReadingState(
            page=saved.page if saved else 1,
            mode=preference.mode if preference else "single",
            completed=saved.completed if saved else False,
            progress_updated_at=saved.updated_at if saved else None,
            mode_updated_at=preference.updated_at if preference else None,
        )

    def save_progress(
        self,
        user_id: str,
        publication: Publication,
        page: int,
        mode: str,
        completed: bool,
    ) -> ReadingProgress:
        if mode not in READING_MODES:
            raise ValueError("Unsupported reading mode")
        if not 1 <= page <= publication.page_count:
            raise ValueError("Page is outside the publication")
        if completed and page != publication.page_count:
            raise ValueError("Only the final page can complete a publication")
        return self._progress.save_reading_progress(
            user_id, publication.id, page, mode, completed
        )

    def mark_as_read(self, user_id: str, publication: Publication) -> ReadingProgress:
        state = self.reading_state(user_id, publication.id)
        return self.save_progress(
            user_id,
            publication,
            publication.page_count,
            state.mode,
            True,
        )

    def mark_as_unread(self, user_id: str, publication_id: str) -> None:
        self._progress.delete_reading_progress(user_id, publication_id)


def publication_order_key(publication: Publication) -> tuple[object, ...]:
    """Sort numbered issues naturally, with deterministic fallbacks."""
    primary = publication.number or publication.title or publication.filename
    return (
        0 if publication.number else 1,
        _natural_key(primary),
        _natural_key(publication.title),
        _natural_key(publication.filename),
        publication.id,
    )


def reading_direction(category: str) -> str:
    return "rtl" if category.casefold() == "manga" else "ltr"


def _natural_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_PARTS.split(value)
        if part
    )
