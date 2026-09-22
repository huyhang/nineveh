from __future__ import annotations

import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import image_bytes, write_cbz
from PIL import Image

from nineveh.archives import (
    ArchiveChanged,
    ArchivePool,
    ArchiveService,
    ArchiveUnavailable,
    DiskCacheBudget,
    PageCacheService,
    PageRenditionService,
    PillowThumbnailRenderer,
    ThumbnailService,
)
from nineveh.catalog import ArchiveInspector


def _scan(settings, archive: Path):
    scanned = ArchiveInspector(settings).inspect(
        archive, archive.relative_to(settings.data_dir).as_posix(), "pub-1"
    )
    return scanned.publication, list(scanned.pages)


# --- ArchivePool -----------------------------------------------------------


def test_pool_reuses_one_open_handle_per_revision(library):
    _, archive = library
    pool = ArchivePool(capacity=2)
    with (
        pool.acquire(archive, "rev1") as first,
        pool.acquire(archive, "rev1") as second,
    ):
        assert first is second
    pool.close()


def test_pool_evicts_the_least_recently_used_archive(tmp_path: Path, library):
    _, archive = library
    other = tmp_path / "other.cbz"
    write_cbz(other)
    pool = ArchivePool(capacity=1)
    with pool.acquire(archive, "rev1"):
        pass
    with pool.acquire(other, "rev1"):
        pass
    assert len(pool._entries) == 1
    pool.close()


def test_pool_never_evicts_an_archive_that_is_still_in_use(tmp_path: Path, library):
    _, archive = library
    other = tmp_path / "other.cbz"
    write_cbz(other)
    pool = ArchivePool(capacity=1)
    with pool.acquire(archive, "rev1") as held:
        with pool.acquire(other, "rev1"):
            pass
        assert held.read("pages/1.png")  # the held handle is still usable
    pool.close()


def test_pool_with_zero_capacity_opens_and_closes_each_time(library):
    _, archive = library
    pool = ArchivePool(capacity=0)
    with pool.acquire(archive, "rev1") as handle:
        assert handle.namelist()
    pool.close()


# --- DiskCacheBudget -------------------------------------------------------


def _write(directory: Path, name: str, size: int) -> Path:
    path = directory / name
    path.write_bytes(bytes(size))
    return path


def test_budget_evicts_until_it_is_under_the_limit(tmp_path: Path):
    budget = DiskCacheBudget(tmp_path, "*.page", limit_bytes=100)
    first = _write(tmp_path, "a.page", 60)
    budget.added(first, 60)
    second = _write(tmp_path, "b.page", 60)
    budget.added(second, 60)
    assert second.exists()
    assert not first.exists()


def test_budget_never_evicts_the_file_it_was_just_told_about(tmp_path: Path):
    """Regression: the newest entry must survive even when timestamps tie."""
    budget = DiskCacheBudget(tmp_path, "*.page", limit_bytes=50)
    for index in range(6):
        path = _write(tmp_path, f"p{index}.page", 40)
        budget.added(path, 40)
        assert path.exists(), f"p{index}.page was evicted by its own write"


def test_budget_ignores_files_outside_its_pattern(tmp_path: Path):
    budget = DiskCacheBudget(tmp_path, "*.page", limit_bytes=100)
    unrelated = _write(tmp_path, "keep-me.cbz", 500)
    budget.added(_write(tmp_path, "a.page", 60), 60)
    budget.added(_write(tmp_path, "b.page", 60), 60)
    assert unrelated.exists()


def test_budget_does_not_double_count_an_overwritten_entry(tmp_path: Path):
    budget = DiskCacheBudget(tmp_path, "*.page", limit_bytes=100)
    keep = _write(tmp_path, "a.page", 40)
    budget.added(keep, 40)
    for _ in range(5):
        budget.added(_write(tmp_path, "a.page", 40), 40)
    assert keep.exists()


# --- PageCacheService ------------------------------------------------------


def test_page_cache_materialises_and_reuses_a_page(library):
    settings, archive = library
    publication, pages = _scan(settings, archive)
    settings.page_cache_dir.mkdir(parents=True, exist_ok=True)
    cache = PageCacheService(settings, ArchiveService(settings))

    first = cache.page(publication, pages[0])
    assert first is not None and first.is_file()
    assert first.stat().st_size == pages[0].uncompressed_size
    assert cache.page(publication, pages[0]) == first


def test_extract_workers_bound_concurrent_materialisation(library):
    settings, _ = library
    cache = PageCacheService(replace(settings, extract_workers=4), None)
    assert all(cache._slots.acquire(blocking=False) for _ in range(4))
    assert cache._slots.acquire(blocking=False) is False


def test_extract_workers_default_to_two(library):
    settings, _ = library
    cache = PageCacheService(settings, None)
    assert all(cache._slots.acquire(blocking=False) for _ in range(2))
    assert cache._slots.acquire(blocking=False) is False


def test_a_single_extract_worker_still_caches(library):
    settings, archive = library
    settings = replace(settings, extract_workers=1)
    publication, pages = _scan(settings, archive)
    settings.page_cache_dir.mkdir(parents=True, exist_ok=True)
    cache = PageCacheService(settings, ArchiveService(settings))
    assert cache.page(publication, pages[0]) is not None


def test_page_cache_is_bypassed_when_disabled(library):
    settings, archive = library
    settings = replace(settings, page_cache_mb=0)
    publication, pages = _scan(settings, archive)
    cache = PageCacheService(settings, ArchiveService(settings))
    assert cache.page(publication, pages[0]) is None


def test_page_cache_skips_pages_larger_than_the_whole_budget(library):
    settings, archive = library
    publication, pages = _scan(settings, archive)
    tiny = replace(settings, page_cache_mb=1)
    huge = replace(pages[0], uncompressed_size=2 << 20)
    assert PageCacheService(tiny, ArchiveService(tiny)).page(publication, huge) is None


# --- ArchiveService --------------------------------------------------------


def test_archive_path_rejects_a_changed_archive(library):
    settings, archive = library
    publication, _ = _scan(settings, archive)
    archive.write_bytes(archive.read_bytes() + b"tail")
    with pytest.raises(ArchiveChanged):
        ArchiveService(settings).archive_path(publication)


def test_archive_path_rejects_a_missing_archive(library):
    settings, archive = library
    publication, _ = _scan(settings, archive)
    archive.unlink()
    with pytest.raises(ArchiveUnavailable):
        ArchiveService(settings).archive_path(publication)


def test_write_range_copies_only_the_requested_pages(library, tmp_path: Path):
    settings, archive = library
    publication, pages = _scan(settings, archive)
    destination = tmp_path / "range.cbz"

    ArchiveService(settings).write_range(publication, pages[1:], destination)

    with zipfile.ZipFile(destination) as produced:
        assert produced.namelist() == ["pages/2.png", "pages/10.png"]
        assert len(produced.read("pages/2.png")) == pages[1].uncompressed_size


def test_page_dimensions_are_measured_from_the_image(library):
    settings, archive = library
    publication, pages = _scan(settings, archive)
    measured = ArchiveService(settings).page_dimensions_many(publication, pages)
    assert measured == {1: (40, 60), 2: (40, 60), 3: (40, 60)}


# --- ThumbnailService ------------------------------------------------------


def test_thumbnail_rejects_an_unsupported_width(library):
    settings, archive = library
    publication, pages = _scan(settings, archive)
    service = ThumbnailService(
        settings, ArchiveService(settings), PillowThumbnailRenderer(10_000_000)
    )
    with pytest.raises(ValueError):
        service.cover(publication, pages[0], width=999)


def test_thumbnail_is_rendered_once_and_then_reused(library):
    settings, archive = library
    publication, pages = _scan(settings, archive)
    settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)
    service = ThumbnailService(
        settings, ArchiveService(settings), PillowThumbnailRenderer(10_000_000)
    )
    first = service.cover(publication, pages[0], width=160)
    assert first.is_file()
    assert service.cover(publication, pages[0], width=160) == first


def test_renderer_refuses_an_oversized_image(tmp_path: Path):
    from io import BytesIO

    renderer = PillowThumbnailRenderer(max_image_pixels=10)
    with pytest.raises(ArchiveUnavailable):
        renderer.render(BytesIO(image_bytes((1, 2, 3))), tmp_path / "out.webp", 160)


# --- PageRenditionService --------------------------------------------------


def _wide_library(tmp_path: Path, settings):
    """A library whose pages are larger than any rendition width."""
    archive = settings.data_dir / "Main Library" / "comics" / "Big" / "Issue 1.cbz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as handle:
        for number in (1, 2):
            handle.writestr(f"{number}.png", image_bytes((10, 20, 30), (2000, 3000)))
    return archive


def _renditions(settings):
    return PageRenditionService(
        settings, ArchiveService(settings), PillowThumbnailRenderer(100_000_000)
    )


def test_a_rendition_is_narrower_and_smaller_than_the_original(tmp_path, library):
    settings, _ = library
    archive = _wide_library(tmp_path, settings)
    publication, pages = _scan(settings, archive)
    settings.rendition_dir.mkdir(parents=True, exist_ok=True)

    path = _renditions(settings).rendition(publication, pages[0], width=640)

    assert path is not None and path.is_file()
    with Image.open(path) as rendered:
        assert rendered.width == 640
        assert rendered.format == "WEBP"
    assert path.stat().st_size < pages[0].uncompressed_size


def test_a_rendition_is_rendered_once_and_then_reused(tmp_path, library):
    settings, _ = library
    archive = _wide_library(tmp_path, settings)
    publication, pages = _scan(settings, archive)
    settings.rendition_dir.mkdir(parents=True, exist_ok=True)
    service = _renditions(settings)

    first = service.rendition(publication, pages[0], width=960)
    assert first == service.rendition(publication, pages[0], width=960)


def test_a_page_already_small_enough_is_served_as_the_original(library):
    settings, archive = library
    publication, pages = _scan(settings, archive)
    settings.rendition_dir.mkdir(parents=True, exist_ok=True)
    # Re-encoding a measured 40x60 page at 640 wide costs bytes and gains
    # nothing, so the caller is told to serve the original instead.
    measured = replace(pages[0], width=40, height=60)

    assert _renditions(settings).rendition(publication, measured, width=640) is None


def test_a_page_of_unknown_size_is_resized_rather_than_guessed_about(tmp_path, library):
    """Dimensions are filled in lazily, so a page can arrive unmeasured."""
    settings, _ = library
    archive = _wide_library(tmp_path, settings)
    publication, pages = _scan(settings, archive)
    settings.rendition_dir.mkdir(parents=True, exist_ok=True)
    assert pages[0].width is None

    assert _renditions(settings).rendition(publication, pages[0], width=640) is not None


def test_renditions_can_be_switched_off_with_a_zero_budget(tmp_path, library):
    settings, _ = library
    archive = _wide_library(tmp_path, settings)
    disabled = replace(settings, rendition_cache_mb=0)
    publication, pages = _scan(disabled, archive)

    assert _renditions(disabled).rendition(publication, pages[0], width=640) is None


@pytest.mark.parametrize("width", [0, 320, 999, 2048])
def test_an_unsupported_rendition_width_is_refused(library, width):
    settings, archive = library
    publication, pages = _scan(settings, archive)
    with pytest.raises(ValueError):
        _renditions(settings).rendition(publication, pages[0], width=width)


def test_an_unreadable_page_reports_the_archive_as_unavailable(tmp_path, library):
    settings, _ = library
    archive = settings.data_dir / "Main Library" / "comics" / "Bad" / "Issue 1.cbz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("1.png", image_bytes((1, 2, 3), (2000, 3000)))
    publication, pages = _scan(settings, archive)
    settings.rendition_dir.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"not an archive at all")

    with pytest.raises(ArchiveUnavailable):
        _renditions(settings).rendition(publication, pages[0], width=640)
