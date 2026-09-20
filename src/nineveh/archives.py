from __future__ import annotations

import os
import shutil
import tempfile
import threading
import zipfile
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, ClassVar

from PIL import Image, ImageOps

from .config import Settings
from .domain import Page, Publication
from .ports import ThumbnailRenderer


class ArchiveUnavailable(RuntimeError):
    pass


class ArchiveChanged(ArchiveUnavailable):
    pass


@dataclass(slots=True)
class _CachedArchive:
    archive: zipfile.ZipFile
    active: int = 0


class ArchivePool:
    """A bounded, reference-counted cache for recently used ZIP central directories."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[tuple[Path, str], _CachedArchive] = OrderedDict()
        self._lock = threading.RLock()

    @contextmanager
    def acquire(self, path: Path, revision: str) -> Iterator[zipfile.ZipFile]:
        if self._capacity == 0:
            with zipfile.ZipFile(path) as archive:
                yield archive
            return

        key = (path, revision)
        with self._lock:
            cached = self._entries.get(key)
            if cached is None:
                cached = _CachedArchive(zipfile.ZipFile(path))
                self._entries[key] = cached
            cached.active += 1
            self._entries.move_to_end(key)
            self._evict()
        try:
            yield cached.archive
        finally:
            with self._lock:
                cached.active -= 1
                self._evict()

    def close(self) -> None:
        with self._lock:
            for cached in self._entries.values():
                cached.archive.close()
            self._entries.clear()

    def _evict(self) -> None:
        while len(self._entries) > self._capacity:
            removable = next(
                (
                    (key, value)
                    for key, value in self._entries.items()
                    if value.active == 0
                ),
                None,
            )
            if removable is None:
                return
            key, cached = removable
            self._entries.pop(key)
            cached.archive.close()


class ArchiveService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._data_root = settings.data_dir.resolve()
        self._pool = ArchivePool(settings.archive_cache_size)
        Image.MAX_IMAGE_PIXELS = settings.max_image_pixels

    def close(self) -> None:
        self._pool.close()

    def archive_path(self, publication: Publication) -> Path:
        relative = PurePosixPath(publication.relative_path)
        candidate = self._data_root
        for part in relative.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ArchiveUnavailable("symbolic links are not served")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._data_root)
            stat = resolved.stat()
        except (OSError, ValueError) as error:
            raise ArchiveUnavailable("archive is no longer available") from error
        if not resolved.is_file():
            raise ArchiveUnavailable("archive is not a regular file")
        if (
            stat.st_size != publication.size
            or stat.st_mtime_ns != publication.modified_ns
        ):
            raise ArchiveChanged("archive changed and must be rescanned")
        return resolved

    def open_page(self, publication: Publication, page: Page) -> Iterator[bytes]:
        path = self.archive_path(publication)
        self._validate_member(path, publication, page)

        def chunks() -> Iterator[bytes]:
            try:
                with self.open_page_file(publication, page) as source:
                    while block := source.read(128 * 1024):
                        yield block
            except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
                raise ArchiveUnavailable("page could not be read") from error

        return chunks()

    def page_dimensions(self, publication: Publication, page: Page) -> tuple[int, int]:
        return self.page_dimensions_many(publication, [page])[page.number]

    def page_dimensions_many(
        self, publication: Publication, pages: list[Page]
    ) -> dict[int, tuple[int, int]]:
        dimensions: dict[int, tuple[int, int]] = {}
        try:
            path = self.archive_path(publication)
            with self._pool.acquire(path, publication.revision) as archive:
                for page in pages:
                    with (
                        archive.open(page.member_name) as source,
                        Image.open(source) as image,
                    ):
                        width, height = image.size
                    self._validate_pixels(width, height)
                    dimensions[page.number] = (width, height)
        except ArchiveUnavailable:
            # `ArchiveChanged` is a `RuntimeError`, so the catch-all below would
            # otherwise downgrade a rescan conflict into "archive is missing".
            raise
        except (
            OSError,
            KeyError,
            RuntimeError,
            zipfile.BadZipFile,
            Image.DecompressionBombError,
        ) as error:
            raise ArchiveUnavailable("page metadata could not be read") from error
        return dimensions

    @contextmanager
    def open_page_file(
        self, publication: Publication, page: Page
    ) -> Iterator[BinaryIO]:
        path = self.archive_path(publication)
        with self._pool.acquire(path, publication.revision) as archive:
            try:
                with archive.open(page.member_name) as source:
                    yield source
            except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
                raise ArchiveUnavailable("page could not be read") from error

    def write_range(
        self, publication: Publication, pages: list[Page], destination: Path
    ) -> None:
        """Copy the requested pages into a new CBZ without buffering them in memory."""
        path = self.archive_path(publication)
        try:
            with (
                self._pool.acquire(path, publication.revision) as archive,
                zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as output,
            ):
                for page in pages:
                    self._copy_member(archive, output, page)
        except ArchiveUnavailable:
            raise
        except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
            raise ArchiveUnavailable("page range could not be read") from error

    @staticmethod
    def _copy_member(
        archive: zipfile.ZipFile, output: zipfile.ZipFile, page: Page
    ) -> None:
        info = archive.getinfo(page.member_name)
        if info.file_size != page.uncompressed_size or info.CRC != page.crc:
            raise ArchiveChanged("archive page changed and must be rescanned")
        with (
            archive.open(info) as source,
            output.open(page.member_name, "w") as target,
        ):
            shutil.copyfileobj(source, target, 128 * 1024)

    def _validate_member(
        self, path: Path, publication: Publication, page: Page
    ) -> None:
        try:
            with self._pool.acquire(path, publication.revision) as archive:
                info = archive.getinfo(page.member_name)
        except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
            raise ArchiveUnavailable(
                "page is no longer present in the archive"
            ) from error
        if (
            info.file_size != page.uncompressed_size
            or info.compress_size != page.compressed_size
            or info.CRC != page.crc
        ):
            raise ArchiveChanged("archive page changed and must be rescanned")

    def _validate_pixels(self, width: int, height: int) -> None:
        if (
            width <= 0
            or height <= 0
            or width * height > self._settings.max_image_pixels
        ):
            raise ArchiveUnavailable("image dimensions exceed the configured limit")


def _modified_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:  # evicted by another worker while we were sorting
        return 0


class PillowThumbnailRenderer:
    def __init__(self, max_image_pixels: int) -> None:
        self._max_image_pixels = max_image_pixels

    def render(self, source: BinaryIO, destination: Path, width: int) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as opened:
            if opened.width * opened.height > self._max_image_pixels:
                raise ArchiveUnavailable("image dimensions exceed the configured limit")
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((width, width * 3), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            with tempfile.NamedTemporaryFile(
                prefix="thumbnail-",
                suffix=".webp",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
            try:
                image.save(temporary_path, format="WEBP", quality=82, method=4)
                os.replace(temporary_path, destination)
            finally:
                temporary_path.unlink(missing_ok=True)


class DiskCacheBudget:
    """Keeps a directory under a byte budget, evicting the oldest files first.

    Sizes are tracked per path so that overwriting an existing entry does not
    inflate the running total, and the file that was just written is never a
    candidate for eviction.
    """

    def __init__(self, directory: Path, pattern: str, limit_bytes: int) -> None:
        self._directory = directory
        self._pattern = pattern
        self._limit_bytes = limit_bytes
        self._known: dict[Path, int] | None = None
        self._lock = threading.Lock()

    def added(self, new_path: Path, size: int) -> None:
        with self._lock:
            if self._known is None:
                self._known = self._files()
            self._known[new_path] = size
            if sum(self._known.values()) > self._limit_bytes:
                self._evict(keep=new_path)

    def _evict(self, *, keep: Path) -> None:
        files = self._files()
        files[keep] = files.get(keep, 0)
        total = sum(files.values())
        for path in sorted((item for item in files if item != keep), key=_modified_ns):
            if total <= self._limit_bytes:
                break
            path.unlink(missing_ok=True)
            total -= files.pop(path)
        self._known = files

    def _files(self) -> dict[Path, int]:
        found: dict[Path, int] = {}
        for path in self._directory.rglob(self._pattern):
            try:
                if path.is_file():
                    found[path] = path.stat().st_size
            except OSError:
                continue
        return found


class ThumbnailService:
    ALLOWED_WIDTHS: ClassVar[frozenset[int]] = frozenset({160, 320, 640})

    def __init__(
        self,
        settings: Settings,
        archives: ArchiveService,
        renderer: ThumbnailRenderer,
    ) -> None:
        self._settings = settings
        self._archives = archives
        self._renderer = renderer
        self._render_lock = threading.Lock()
        self._budget = DiskCacheBudget(
            settings.thumbnail_dir,
            "*.webp",
            settings.thumbnail_cache_mb * 1024 * 1024,
        )

    def cover(self, publication: Publication, page: Page, width: int) -> Path:
        if width not in self.ALLOWED_WIDTHS:
            raise ValueError("thumbnail width must be 160, 320, or 640")
        destination = (
            self._settings.thumbnail_dir
            / publication.id
            / f"{publication.revision}-{page.crc:08x}-{width}.webp"
        )
        if destination.is_file():
            os.utime(destination, None)
            return destination

        with self._render_lock:
            if destination.is_file():
                return destination
            try:
                with self._archives.open_page_file(publication, page) as source:
                    self._renderer.render(source, destination, width)
            except (OSError, Image.DecompressionBombError) as error:
                raise ArchiveUnavailable("cover could not be rendered") from error
            self._budget.added(destination, destination.stat().st_size)
        return destination


class PageCacheService:
    """Materializes original pages into a bounded on-disk cache without buffering in RAM."""

    def __init__(self, settings: Settings, archives: ArchiveService) -> None:
        self._settings = settings
        self._archives = archives
        self._slots = threading.BoundedSemaphore(settings.extract_workers)
        self._locks = tuple(threading.Lock() for _ in range(16))
        self._budget = DiskCacheBudget(
            settings.page_cache_dir,
            "*.page",
            settings.page_cache_mb * 1024 * 1024,
        )

    def page(self, publication: Publication, page: Page) -> Path | None:
        cache_limit = self._settings.page_cache_mb * 1024 * 1024
        if cache_limit == 0 or page.uncompressed_size > cache_limit:
            return None
        destination = (
            self._settings.page_cache_dir
            / publication.id
            / f"{publication.revision}-{page.number}-{page.crc:08x}.page"
        )
        if self._is_complete(destination, page):
            os.utime(destination, None)  # keep hot pages away from the budget
            return destination
        lock = self._locks[hash((publication.id, page.number)) % len(self._locks)]
        with self._slots, lock:
            if self._is_complete(destination, page):
                return destination
            self._materialise(publication, page, destination)
            self._budget.added(destination, destination.stat().st_size)
        return destination

    @staticmethod
    def _is_complete(destination: Path, page: Page) -> bool:
        return (
            destination.is_file()
            and destination.stat().st_size == page.uncompressed_size
        )

    def _materialise(
        self, publication: Publication, page: Page, destination: Path
    ) -> None:
        """Stream a page to a sibling temporary file, then move it into place."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix="page-", suffix=".tmp", dir=destination.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            try:
                for chunk in self._archives.open_page(publication, page):
                    temporary.write(chunk)
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
        try:
            if temporary_path.stat().st_size != page.uncompressed_size:
                raise ArchiveUnavailable("page size changed while it was being cached")
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
