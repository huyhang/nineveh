from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fakes import page, publication

from nineveh.database import SQLiteRepository
from nineveh.domain import (
    ReadingProgress,
    ReadScope,
    ScannedPublication,
    SpreadAnalysis,
    SpreadGuess,
)
from nineveh.reader import (
    ReaderService,
    SpreadDetectionService,
    clamp_anchor,
    publication_order_key,
    reading_direction,
)


class ReaderRepositoryStub:
    def __init__(self, publications):
        self.publications = publications
        self.saved = None
        self.progress = None

    def publication_by_id(self, publication_id, _scope=None):
        return next(
            (item for item in self.publications if item.id == publication_id), None
        )

    def publications_in_series(self, series_id, _scope=None):
        return [item for item in self.publications if item.series_id == series_id]

    def reading_progress(self, _user_id, _publication_id):
        return self.progress

    def reading_progress_for_publications(self, _user_id, publication_ids):
        if not self.progress or self.progress.publication_id not in publication_ids:
            return {}
        return {self.progress.publication_id: self.progress}

    def save_reading_progress(self, user_id, publication_id, page, mode, completed):
        self.saved = ReadingProgress(
            user_id,
            publication_id,
            page,
            mode or "single",
            completed,
            datetime.now(UTC),
        )
        self.progress = self.saved
        return self.saved

    def delete_reading_progress(self, _user_id, publication_id):
        if self.progress and self.progress.publication_id == publication_id:
            self.progress = None


def _publication(identifier: str, number: str | None, title: str):
    return replace(
        publication(identifier, pages=12),
        number=number,
        title=title,
        filename=f"{title}.cbz",
        series_id="series-1",
    )


def test_reader_orders_numbered_publications_naturally_and_finds_neighbours():
    items = [
        _publication("ten", "10", "Volume 10"),
        _publication("bonus", None, "Bonus"),
        _publication("two", "2", "Volume 2"),
        _publication("one", "1", "Volume 1"),
    ]
    repository = ReaderRepositoryStub(items)

    context = ReaderService(repository, repository).context(
        "two", ReadScope(unrestricted=True)
    )

    assert context is not None
    assert [item.id for item in context.publications] == [
        "one",
        "two",
        "ten",
        "bonus",
    ]
    assert context.previous.id == "one"
    assert context.next.id == "ten"
    assert context.position == 1


def test_publication_order_has_deterministic_title_and_filename_fallbacks():
    items = [
        _publication("b", None, "Special 10"),
        _publication("a", None, "Special 2"),
    ]
    assert [item.id for item in sorted(items, key=publication_order_key)] == ["a", "b"]


def test_manga_pairs_read_right_to_left_while_comics_read_left_to_right():
    assert reading_direction("manga") == "rtl"
    assert reading_direction("MANGA") == "rtl"
    assert reading_direction("comics") == "ltr"


@pytest.mark.parametrize(
    ("anchor", "page_count", "expected"),
    [
        (4, 20, 4),
        (None, 20, None),
        (1, 20, 2),  # The cover stands alone, so pairing cannot start before 2.
        (0, 20, 2),
        (99, 20, 20),  # A re-scanned volume may be shorter than its stored anchor.
        (4, 1, None),  # Nothing to pair in a single-page publication.
    ],
)
def test_an_anchor_is_kept_inside_the_volume_it_belongs_to(
    anchor, page_count, expected
):
    assert clamp_anchor(anchor, page_count) == expected


class SpreadRepositoryStub:
    def __init__(self, item, pages):
        self.item = item
        self.enabled = True
        self.override = None
        self.analysis = None
        self.items = pages

    def publications_in_series(self, _series_id):
        return [self.item]

    def publication_spread_analysis(self, _publication_id, revision):
        current = self.analysis
        return current if current and current.revision == revision else None

    def pages(self, _publication_id, _start, _end):
        return self.items

    def update_page_dimensions(self, _publication_id, dimensions):
        measured = {number: (width, height) for number, width, height in dimensions}
        self.items = [
            replace(
                value,
                width=measured[value.number][0],
                height=measured[value.number][1],
            )
            if value.number in measured
            else value
            for value in self.items
        ]

    def save_publication_spread_analysis(
        self, publication_id, revision, anchor, source=None
    ):
        self.analysis = SpreadAnalysis(
            publication_id,
            revision,
            "detected" if anchor else "none",
            anchor,
            datetime.now(UTC),
            source,
        )
        return self.analysis

    def series_spread_detection(self, _series_id):
        return self.enabled

    def publication_spread_override(self, _publication_id):
        return self.override


class CountingDetector:
    def __init__(self, anchor, source="gutter"):
        self.guess = SpreadGuess(anchor, source if anchor else None)
        self.calls = 0

    def detect(self, _publication, _pages, direction):
        self.calls += 1
        assert direction in {"ltr", "rtl"}
        return self.guess


class MeasuringArchives:
    def __init__(self):
        self.calls = 0

    def page_dimensions_many(self, _publication, pages):
        self.calls += 1
        return {value.number: (120, 180) for value in pages}


def _spread_service(anchor=3, pages=4):
    item = replace(publication("spread", pages=pages), series_id="series-1")
    repository = SpreadRepositoryStub(
        item, [page(number) for number in range(1, pages + 1)]
    )
    detector = CountingDetector(anchor)
    archives = MeasuringArchives()
    service = SpreadDetectionService(repository, archives, detector)
    return service, repository, detector, archives, item


def test_a_volume_is_measured_and_detected_once_per_revision():
    service, repository, detector, archives, item = _spread_service()

    service.analyze_series("series-1")
    assert service.anchor_for(item) == 3
    assert (detector.calls, archives.calls) == (1, 1)
    assert repository.items[0].width == 120  # Dimensions persisted for the reader.

    service.analyze_series("series-1")
    assert (detector.calls, archives.calls) == (1, 1)
    service.analyze_series("series-1", force=True)
    assert detector.calls == 2


def test_a_disabled_series_reports_no_anchor_at_all():
    service, repository, detector, _archives, item = _spread_service()
    repository.enabled = False
    assert service.anchor_for(item) is None
    assert detector.calls == 0


def test_a_hand_set_start_page_wins_over_detection_without_re_reading_the_archive():
    service, repository, detector, _archives, item = _spread_service()
    repository.override = 2
    assert service.anchor_for(item) == 2
    assert detector.calls == 0, "an override should not trigger image decoding"


def test_a_stale_hand_set_start_page_is_clamped_into_the_volume():
    service, repository, _detector, _archives, item = _spread_service(pages=4)
    repository.override = 99
    assert service.anchor_for(item) == 4


def test_detection_that_abstains_leaves_pairing_where_it_was():
    service, _repository, _detector, _archives, item = _spread_service(anchor=None)
    assert service.anchor_for(item) is None


def test_reader_state_contains_only_account_synced_progress():
    item = _publication("one", "1", "Volume 1")
    repository = ReaderRepositoryStub([item])
    repository.progress = ReadingProgress(
        "user",
        "one",
        7,
        "single",
        False,
        datetime.now(UTC),
    )
    saved = ReaderService(repository, repository).reading_state("user", "one")

    assert saved is not None
    assert (saved.page, saved.completed) == (7, False)


def test_reader_collects_progress_for_visible_publications():
    items = [
        _publication("one", "1", "Volume 1"),
        _publication("two", "2", "Volume 2"),
    ]
    repository = ReaderRepositoryStub(items)
    repository.progress = ReadingProgress(
        "user",
        "two",
        7,
        "single",
        False,
        datetime.now(UTC),
    )

    saved = ReaderService(repository, repository).progress_for_publications(
        "user", items
    )

    assert saved == {"two": repository.progress}


def test_reader_validates_progress_before_persisting_it():
    item = _publication("one", "1", "Volume 1")
    repository = ReaderRepositoryStub([item])
    service = ReaderService(repository, repository)

    saved = service.save_progress("user", item, 12, None, True)
    assert (saved.page, saved.completed) == (12, True)

    with pytest.raises(ValueError, match="outside"):
        service.save_progress("user", item, 13, None, False)
    with pytest.raises(ValueError, match="final page"):
        service.save_progress("user", item, 2, None, True)
    with pytest.raises(ValueError, match="reading mode"):
        service.save_progress("user", item, 1, "sideways", False)


def test_reader_marks_publications_read_and_unread():
    item = _publication("one", "1", "Volume 1")
    repository = ReaderRepositoryStub([item])
    repository.progress = ReadingProgress(
        "user", "one", 3, "double", False, datetime.now(UTC)
    )
    service = ReaderService(repository, repository)

    saved = service.mark_as_read("user", item)

    assert (saved.page, saved.completed) == (12, True)
    assert repository.saved.mode == "single", "the stub keeps what the service passes"
    service.mark_as_unread("user", item.id)
    assert repository.progress is None


def test_sqlite_progress_is_private_to_each_user(tmp_path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    first_user = repository.create_user("first", "hash", False)
    second_user = repository.create_user("second", "hash", False)
    item = publication()
    repository.upsert_publication(ScannedPublication(item, (page(1), page(2))))

    saved = repository.save_reading_progress(first_user.id, item.id, 2, None, True)

    assert (saved.page, saved.completed) == (2, True)
    assert repository.reading_progress_for_publications(first_user.id, [item.id]) == {
        item.id: saved
    }
    assert repository.reading_progress_for_publications(first_user.id, []) == {}
    assert repository.reading_progress_for_publications(second_user.id, [item.id]) == {}
    assert repository.reading_progress(second_user.id, item.id) is None
    repository.delete_reading_progress(first_user.id, item.id)
    assert repository.reading_progress(first_user.id, item.id) is None


def _progress_columns(path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            row[1] for row in connection.execute("PRAGMA table_info(reading_progress)")
        }


def test_upgrade_keeps_the_legacy_reading_mode_older_apps_read(tmp_path):
    path = tmp_path / "nineveh.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    user = repository.create_user("reader", "hash", False)
    item = publication()
    repository.upsert_publication(ScannedPublication(item, (page(1), page(2))))
    repository.save_reading_progress(user.id, item.id, 2, "double", True)
    with repository._connect() as connection:
        connection.execute("PRAGMA user_version = 7")

    repository.initialize()

    saved = repository.reading_progress(user.id, item.id)
    assert saved is not None
    assert (saved.page, saved.mode, saved.completed) == (2, "double", True)


def test_a_save_without_a_mode_keeps_the_one_an_older_app_stored(tmp_path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    user = repository.create_user("reader", "hash", False)
    item = publication()
    repository.upsert_publication(ScannedPublication(item, (page(1), page(2))))

    assert repository.save_reading_progress(user.id, item.id, 1, None, False).mode == (
        "single"
    )
    repository.save_reading_progress(user.id, item.id, 1, "scroll", False)
    saved = repository.save_reading_progress(user.id, item.id, 2, None, True)

    assert (saved.page, saved.mode, saved.completed) == (2, "scroll", True)


def test_upgrade_restores_the_mode_a_pre_release_build_dropped(tmp_path):
    """An interim v8 build removed the column; older apps fail without it."""
    path = tmp_path / "nineveh.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    user = repository.create_user("reader", "hash", False)
    item = publication()
    repository.upsert_publication(ScannedPublication(item, (page(1), page(2))))
    repository.save_reading_progress(user.id, item.id, 2, "double", False)
    with repository._connect() as connection:
        connection.execute("ALTER TABLE reading_progress DROP COLUMN mode")
    assert "mode" not in _progress_columns(path)

    repository.initialize()

    assert "mode" in _progress_columns(path)
    saved = repository.reading_progress(user.id, item.id)
    assert saved is not None and (saved.page, saved.mode) == (2, "single")
    updated = repository.save_reading_progress(user.id, item.id, 1, "scroll", False)
    assert updated.mode == "scroll"


def test_upgrade_adds_privacy_and_leaves_existing_series_public(tmp_path):
    path = tmp_path / "nineveh.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    item = publication()
    repository.upsert_publication(ScannedPublication(item, (page(1), page(2))))
    with repository._connect() as connection:
        connection.execute("ALTER TABLE catalog_series DROP COLUMN is_private")
        connection.execute("PRAGMA user_version = 7")

    repository.initialize()

    [series] = repository.catalog_series()
    assert series.is_private is False
    assert repository.set_series_private(series.id, True) is not None
    assert repository.catalog_series() == []


def test_sqlite_stores_series_setting_and_revision_bound_spread_result(tmp_path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    item = publication("spread", pages=3)
    repository.upsert_publication(
        ScannedPublication(
            item,
            (
                replace(page(1), width=120, height=180),
                replace(page(2), width=260, height=180),
                replace(page(3), width=120, height=180),
            ),
        )
    )
    stored = repository.publication_by_id(item.id)
    assert stored is not None and stored.series_id

    assert repository.set_series_spread_detection("missing", True) is False
    assert repository.set_series_spread_detection(stored.series_id, True) is True
    assert repository.series_spread_detection(stored.series_id) is True
    assert repository.spread_detection_series_ids() == [stored.series_id]

    analysis = repository.save_publication_spread_analysis(item.id, item.revision, 2)
    assert (analysis.status, analysis.anchor_page) == ("detected", 2)
    assert repository.publication_spread_analysis(item.id, "other-revision") is None
    assert repository.set_series_spread_detection(stored.series_id, False) is True
    assert repository.series_spread_detection(stored.series_id) is False


def test_upgrading_discards_anchors_from_the_old_detector_but_keeps_overrides(
    tmp_path,
):
    """The previous release stored the first wide page as the anchor, which is
    not where pairing starts. Those rows must be recomputed, not trusted."""
    from nineveh.database import SEAM_DETECTION_VERSION

    path = tmp_path / "nineveh.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    item = publication("spread", pages=9)
    repository.upsert_publication(
        ScannedPublication(item, tuple(page(number) for number in (1, 2, 3)))
    )
    repository.save_publication_spread_analysis(item.id, item.revision, 3)
    repository.set_publication_spread_override(item.id, 4)

    with repository._connect() as connection:
        connection.execute(f"PRAGMA user_version = {SEAM_DETECTION_VERSION - 1}")
    SQLiteRepository(path).initialize()

    assert repository.publication_spread_analysis(item.id, item.revision) is None
    assert repository.publication_spread_override(item.id) == 4


def test_upgrading_recomputes_every_anchor_but_a_wide_pages(tmp_path):
    """A wide page used to be read only when the gutter found nothing, and now
    decides before the gutter is read. Any other stored answer may have come
    from a gutter the new order never reaches."""
    from nineveh.database import WIDE_PAGE_FIRST_VERSION

    path = tmp_path / "nineveh.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    items = {
        source: replace(
            publication(f"spread-{source}", pages=9),
            relative_path=f"Lib/comics/Series/{source}.cbz",
        )
        for source in ("gutter", "wide page", None)
    }
    for source, item in items.items():
        repository.upsert_publication(
            ScannedPublication(item, tuple(page(number) for number in (1, 2, 3)))
        )
        repository.save_publication_spread_analysis(
            item.id, item.revision, 3 if source else None, source
        )
    repository.set_publication_spread_override(items["gutter"].id, 4)

    with repository._connect() as connection:
        connection.execute(f"PRAGMA user_version = {WIDE_PAGE_FIRST_VERSION - 1}")
    SQLiteRepository(path).initialize()

    def stored(source):
        item = items[source]
        return repository.publication_spread_analysis(item.id, item.revision)

    assert stored("gutter") is None
    assert stored(None) is None
    assert (stored("wide page").anchor_page, stored("wide page").source) == (
        3,
        "wide page",
    )
    assert repository.publication_spread_override(items["gutter"].id) == 4


def test_the_detector_that_answered_is_recorded_with_the_anchor():
    """The panel names the evidence, so the service has to carry it through."""
    service, repository, _detector, _archives, _item = _spread_service()
    service.analyze_series("series-1")
    assert repository.analysis.source == "gutter"
    assert repository.analysis.anchor_page == 3


def test_an_abstention_records_no_evidence():
    service, repository, _detector, _archives, _item = _spread_service(anchor=None)
    service.analyze_series("series-1")
    assert (repository.analysis.status, repository.analysis.source) == ("none", None)


def test_sqlite_keeps_the_detection_source_alongside_the_anchor(tmp_path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    item = publication("spread", pages=9)
    repository.upsert_publication(
        ScannedPublication(item, tuple(page(number) for number in (1, 2, 3)))
    )

    saved = repository.save_publication_spread_analysis(
        item.id, item.revision, 4, "wide page"
    )
    assert saved.source == "wide page"
    reloaded = repository.publication_spread_analysis(item.id, item.revision)
    assert (reloaded.anchor_page, reloaded.source) == (4, "wide page")

    # An anchor stored by an older release has no evidence recorded.
    plain = repository.save_publication_spread_analysis(item.id, item.revision, 4)
    assert plain.source is None
