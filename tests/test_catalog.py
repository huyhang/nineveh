from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from conftest import image_bytes, write_cbz

from nineveh.catalog import ArchiveInspector, CatalogScanner, UnsafeArchive
from nineveh.database import SQLiteRepository


def _scanner(settings) -> tuple[CatalogScanner, SQLiteRepository]:
    repository = SQLiteRepository(settings.database_path)
    repository.initialize()
    return CatalogScanner(
        settings.data_dir, repository, ArchiveInspector(settings)
    ), repository


def test_scans_expected_hierarchy_and_metadata(library):
    settings, _ = library
    scanner, repository = _scanner(settings)

    report = scanner.scan()
    items, total = repository.publications(limit=10)

    assert report.indexed == 1
    assert report.discovered == 1
    assert total == 1
    assert items[0].title == "The First Issue"
    assert items[0].number == "1"
    assert items[0].description == "An example publication."
    assert items[0].authors == ("A. Writer",)
    assert items[0].library == "Main Library"
    assert items[0].category == "comics"
    assert items[0].series == "Example Series"
    assert [page.member_name for page in repository.pages(items[0].id, 1, 3)] == [
        "pages/1.png",
        "pages/2.png",
        "pages/10.png",
    ]


def test_comicinfo_double_pages_are_retained(library):
    settings, archive = library
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("1.png", image_bytes((1, 2, 3)))
        handle.writestr("2.png", image_bytes((2, 3, 4), size=(120, 60)))
        handle.writestr(
            "ComicInfo.xml",
            '<ComicInfo><Pages><Page Image="0" Type="FrontCover" />'
            '<Page Image="1" DoublePage="true" /></Pages></ComicInfo>',
        )

    scanned = ArchiveInspector(settings).inspect(
        archive, "Main Library/comics/Example Series/Issue 1.cbz", "pub-1"
    )

    assert [page.is_spread for page in scanned.pages] == [False, True]


def test_a_second_scan_reports_everything_unchanged(library):
    settings, _ = library
    scanner, _ = _scanner(settings)
    scanner.scan()
    second = scanner.scan()
    assert (second.unchanged, second.indexed) == (1, 0)


def test_the_publication_identifier_survives_a_reindex(library):
    settings, archive = library
    scanner, repository = _scanner(settings)
    scanner.scan()
    original = repository.publications(limit=1)[0][0].id

    write_cbz(archive)  # same path, new bytes
    scanner.scan()
    assert repository.publications(limit=1)[0][0].id == original


def test_status_tracks_the_last_completed_scan(library):
    settings, _ = library
    scanner, _ = _scanner(settings)
    assert scanner.status.completed_at is None

    scanner.scan()
    first = scanner.status
    assert first.running is False
    assert first.completed_at is not None
    assert first.catalog_modified_at is not None
    assert first.error is None

    scanner.scan()
    assert scanner.status.catalog_modified_at == first.catalog_modified_at


def test_a_removed_file_is_dropped_from_the_catalog(library):
    settings, archive = library
    scanner, repository = _scanner(settings)
    scanner.scan()

    archive.unlink()
    report = scanner.scan()

    assert report.removed == 1
    assert repository.publications(limit=10)[1] == 0


def test_a_failed_scan_is_recorded_in_the_status(library):
    settings, _ = library
    scanner, _ = _scanner(settings)
    for entry in settings.data_dir.iterdir():
        for child in entry.rglob("*"):
            pass
    settings.data_dir.rename(settings.data_dir.with_name("gone"))

    with pytest.raises(FileNotFoundError):
        scanner.scan()
    assert scanner.status.running is False
    assert scanner.status.error is not None


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("traversal", lambda a: a.writestr("../escape.png", b"x")),
        ("absolute", lambda a: a.writestr("/etc/passwd.png", b"x")),
        ("no images", lambda a: a.writestr("readme.txt", b"x")),
    ],
)
def test_unsafe_or_empty_archives_are_skipped(library, name, build):
    settings, _ = library
    bad_dir = settings.data_dir / "Main Library" / "manga" / "Bad"
    bad_dir.mkdir(parents=True)
    with zipfile.ZipFile(bad_dir / "bad.cbz", "w") as archive:
        build(archive)

    scanner, repository = _scanner(settings)
    report = scanner.scan()

    assert report.failed == 1, name
    assert repository.publications(limit=10)[1] == 1


def test_a_duplicate_entry_is_rejected(library, tmp_path: Path):
    settings, _ = library
    inspector = ArchiveInspector(settings)
    archive = tmp_path / "dupe.cbz"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("1.png", image_bytes((1, 2, 3)))
        handle.writestr("1.png", image_bytes((3, 2, 1)))
    with pytest.raises(UnsafeArchive, match="duplicate"):
        inspector.inspect(archive, "L/comics/S/dupe.cbz", "id")


def test_an_archive_with_too_many_entries_is_rejected(library, tmp_path: Path):
    from dataclasses import replace

    settings = replace(library[0], max_archive_entries=2)
    archive = tmp_path / "many.cbz"
    with zipfile.ZipFile(archive, "w") as handle:
        for index in range(3):
            handle.writestr(f"{index}.png", image_bytes((1, 2, 3)))
    with pytest.raises(UnsafeArchive, match="too many entries"):
        ArchiveInspector(settings).inspect(archive, "L/comics/S/many.cbz", "id")


def test_an_oversized_page_is_rejected(library, tmp_path: Path):
    from dataclasses import replace

    settings = replace(library[0], max_page_uncompressed_bytes=10)
    archive = tmp_path / "big.cbz"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("1.png", image_bytes((1, 2, 3)))
    with pytest.raises(UnsafeArchive, match="exceeds the configured limit"):
        ArchiveInspector(settings).inspect(archive, "L/comics/S/big.cbz", "id")


def test_non_cbz_files_and_stray_directories_are_ignored(library):
    settings, _ = library
    series = settings.data_dir / "Main Library" / "comics" / "Example Series"
    (series / "notes.txt").write_text("ignored")
    (settings.data_dir / "Main Library" / "other").mkdir()
    (settings.data_dir / "loose.cbz").write_bytes(b"ignored")

    scanner, repository = _scanner(settings)
    report = scanner.scan()
    assert report.discovered == 1
    assert repository.publications(limit=10)[1] == 1


def test_a_malformed_comicinfo_falls_back_to_the_filename(library, tmp_path: Path):
    settings, _ = library
    archive = tmp_path / "Chapter 7.cbz"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("1.png", image_bytes((1, 2, 3)))
        handle.writestr("ComicInfo.xml", "<not xml")
    scanned = ArchiveInspector(settings).inspect(
        archive, "L/comics/S/Chapter 7.cbz", "id"
    )
    assert scanned.publication.title == "Chapter 7"
    assert scanned.publication.authors == ()


def test_two_scans_cannot_run_at_once(library):
    settings, _ = library
    scanner, _ = _scanner(settings)
    scanner._run_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            scanner.scan()
    finally:
        scanner._run_lock.release()


def test_library_scan_only_reconciles_that_library(library):
    settings, first_archive = library
    second = settings.data_dir / "Second Library" / "manga" / "Series" / "One.cbz"
    second.parent.mkdir(parents=True)
    write_cbz(second)
    scanner, repository = _scanner(settings)
    scanner.scan()
    first = next(
        item for item in repository.managed_libraries() if item.name == "Main Library"
    )

    first_archive.unlink()
    report = scanner.scan(first.id)

    assert report.removed == 1
    remaining, total = repository.publications(limit=10)
    assert total == 1
    assert remaining[0].library == "Second Library"
