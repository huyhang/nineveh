from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fakes import page, publication

from nineveh.database import SQLiteRepository
from nineveh.domain import ReadingProgress, ReadScope, ScannedPublication
from nineveh.reader import ReaderService, publication_order_key, reading_direction


class ReaderRepositoryStub:
    def __init__(self, publications):
        self.publications = publications
        self.saved = None
        self.progress = None
        self.preferred_mode = None

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

    def latest_reading_progress(self, _user_id):
        if not self.preferred_mode:
            return None
        return ReadingProgress(
            "user",
            "preferred-publication",
            1,
            self.preferred_mode,
            False,
            datetime.now(UTC),
        )

    def save_reading_progress(self, user_id, publication_id, page, mode, completed):
        self.saved = ReadingProgress(
            user_id,
            publication_id,
            page,
            mode,
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


def test_reader_uses_the_latest_mode_as_the_cross_publication_preference():
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
    repository.preferred_mode = "scroll"

    saved = ReaderService(repository, repository).reading_state("user", "one")

    assert saved is not None
    assert (saved.page, saved.mode) == (7, "scroll")


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

    saved = service.save_progress("user", item, 12, "double", True)
    assert (saved.page, saved.mode, saved.completed) == (12, "double", True)

    with pytest.raises(ValueError, match="outside"):
        service.save_progress("user", item, 13, "single", False)
    with pytest.raises(ValueError, match="mode"):
        service.save_progress("user", item, 1, "sideways", False)
    with pytest.raises(ValueError, match="final page"):
        service.save_progress("user", item, 2, "single", True)


def test_reader_marks_publications_read_and_unread():
    item = _publication("one", "1", "Volume 1")
    repository = ReaderRepositoryStub([item])
    repository.preferred_mode = "scroll"
    service = ReaderService(repository, repository)

    saved = service.mark_as_read("user", item)

    assert (saved.page, saved.mode, saved.completed) == (12, "scroll", True)
    service.mark_as_unread("user", item.id)
    assert repository.progress is None


def test_sqlite_progress_is_private_to_each_user(tmp_path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    first_user = repository.create_user("first", "hash", False)
    second_user = repository.create_user("second", "hash", False)
    item = publication()
    repository.upsert_publication(ScannedPublication(item, (page(1), page(2))))

    saved = repository.save_reading_progress(first_user.id, item.id, 2, "double", True)

    assert (saved.page, saved.mode, saved.completed) == (2, "double", True)
    assert repository.reading_progress_for_publications(first_user.id, [item.id]) == {
        item.id: saved
    }
    assert repository.reading_progress_for_publications(first_user.id, []) == {}
    assert repository.reading_progress_for_publications(second_user.id, [item.id]) == {}
    assert repository.latest_reading_progress(first_user.id).mode == "double"
    assert repository.reading_progress(second_user.id, item.id) is None
    repository.delete_reading_progress(first_user.id, item.id)
    assert repository.reading_progress(first_user.id, item.id) is None
