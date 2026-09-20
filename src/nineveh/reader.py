from __future__ import annotations

import re

from .domain import Publication, ReaderContext, ReadingProgress, ReadingState, ReadScope
from .ports import CatalogRepository, ReadingRepository

READING_MODES = frozenset({"single", "double", "scroll"})
_NATURAL_PARTS = re.compile(r"(\d+)")


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
