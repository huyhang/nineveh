from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, ClassVar

from PIL import Image

from .config import Settings
from .domain import Page, Publication
from .image_worker import EXIT_TOO_LARGE
from .imaging import ImageTooLarge, render_webp
from .ports import ThumbnailRenderer
from .storage import StorageError, StoragePathResolver

LOGGER = logging.getLogger(__name__)


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
    def __init__(self, settings: Settings, paths: StoragePathResolver) -> None:
        self._settings = settings
        self._paths = paths
        self._pool = ArchivePool(settings.archive_cache_size)
        Image.MAX_IMAGE_PIXELS = settings.max_image_pixels

    def close(self) -> None:
        self._pool.close()

    def archive_path(self, publication: Publication) -> Path:
        try:
            root = self._paths.publication_root(publication).resolve()
            candidate = self._paths.publication_path(publication)
        except StorageError as error:
            raise ArchiveUnavailable(str(error)) from error
        walked = root
        for part in PurePosixPath(publication.relative_path).parts:
            walked = walked / part
            if walked.is_symlink():
                raise ArchiveUnavailable("symbolic links are not served")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
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
    """Decodes in this process: fast, and the default for library pages."""

    def __init__(self, max_image_pixels: int) -> None:
        self._max_image_pixels = max_image_pixels

    def render(self, source: BinaryIO, destination: Path, width: int) -> None:
        try:
            self.render_box(source, destination, (width, width * 3), 82)
        except ImageTooLarge as error:
            raise ArchiveUnavailable(str(error)) from error

    def render_box(
        self, source: BinaryIO, destination: Path, box: tuple[int, int], quality: int
    ) -> None:
        render_webp(
            source,
            destination,
            box=box,
            quality=quality,
            max_pixels=self._max_image_pixels,
        )


class SubprocessThumbnailRenderer:
    """Decodes each image in a short-lived worker under memory and CPU limits.

    A malformed image can then crash or exhaust only the worker, never the
    service. The price is a process start per image -- several times the
    latency of an in-process render -- so it is reserved for input Nineveh does
    not control unless an operator opts library pages in too.
    """

    def __init__(
        self, max_image_pixels: int, memory_bytes: int, timeout_seconds: int
    ) -> None:
        self._max_image_pixels = max_image_pixels
        self._memory_bytes = memory_bytes
        self._timeout = timeout_seconds
        self._warned = False

    def render(self, source: BinaryIO, destination: Path, width: int) -> None:
        try:
            self.render_box(source, destination, (width, width * 3), 82)
        except ImageTooLarge as error:
            raise ArchiveUnavailable(str(error)) from error

    def render_box(
        self, source: BinaryIO, destination: Path, box: tuple[int, int], quality: int
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix="image-source-", dir=destination.parent, delete=False
        ) as spooled:
            input_path = Path(spooled.name)
            shutil.copyfileobj(source, spooled, 128 * 1024)
        try:
            self._run(input_path, destination, box, quality)
        finally:
            input_path.unlink(missing_ok=True)

    def _run(
        self, source: Path, destination: Path, box: tuple[int, int], quality: int
    ) -> None:
        command = [
            sys.executable,
            "-m",
            "nineveh.image_worker",
            str(source),
            str(destination),
            *(str(value) for value in (*box, quality, self._max_image_pixels)),
            str(self._memory_bytes),
            str(self._timeout),
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout + 2,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise OSError("image worker timed out") from error
        self._note_limits(completed.stdout.decode(errors="replace"))
        if completed.returncode == EXIT_TOO_LARGE:
            raise ImageTooLarge("image dimensions exceed the configured limit")
        if completed.returncode != 0 or not destination.is_file():
            detail = completed.stderr.decode(errors="replace").strip()[-300:]
            raise OSError(f"image worker exited {completed.returncode}: {detail}")

    def _note_limits(self, stdout: str) -> None:
        report = stdout.partition("\n")[0].removeprefix("limits: ")
        if report != "applied" and not self._warned:
            self._warned = True
            LOGGER.warning("Image worker is running without some limits: %s", report)


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

    def cached(self, publication: Publication, page: Page, width: int) -> Path | None:
        if width not in self.ALLOWED_WIDTHS:
            return None
        destination = self._destination(publication, page, width)
        if not destination.is_file():
            return None
        os.utime(destination, None)
        return destination

    def cover(self, publication: Publication, page: Page, width: int) -> Path:
        if width not in self.ALLOWED_WIDTHS:
            raise ValueError("thumbnail width must be 160, 320, or 640")
        if cached := self.cached(publication, page, width):
            return cached
        destination = self._destination(publication, page, width)
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

    def _destination(self, publication: Publication, page: Page, width: int) -> Path:
        return (
            self._settings.thumbnail_dir
            / publication.id
            / f"{publication.revision}-{page.crc:08x}-{width}.webp"
        )


class PageRenditionService:
    """Screen-sized WebP copies of individual pages, for continuous scroll.

    Scrolling keeps several pages alive at once, and a high-resolution scan
    costs far more as a decoded bitmap than it does on the wire. Serving a copy
    no wider than the reader's viewport bounds both. A zero budget switches
    renditions off, and callers fall back to the original bytes.
    """

    ALLOWED_WIDTHS: ClassVar[frozenset[int]] = frozenset({640, 960, 1280})

    def __init__(
        self,
        settings: Settings,
        archives: ArchiveService,
        renderer: ThumbnailRenderer,
    ) -> None:
        self._settings = settings
        self._archives = archives
        self._renderer = renderer
        self._slots = threading.BoundedSemaphore(settings.extract_workers)
        # Striped rather than one global lock: a scroll window asks for several
        # neighbouring pages at once, and they have no reason to queue.
        self._locks = tuple(threading.Lock() for _ in range(16))
        self._budget = DiskCacheBudget(
            settings.rendition_dir,
            "*.webp",
            settings.rendition_cache_mb * 1024 * 1024,
        )

    def cached(self, publication: Publication, page: Page, width: int) -> Path | None:
        if width not in self.ALLOWED_WIDTHS or not self._worth_it(page, width):
            return None
        destination = self._destination(publication, page, width)
        if not destination.is_file():
            return None
        os.utime(destination, None)  # keep hot pages away from the budget
        return destination

    def rendition(
        self, publication: Publication, page: Page, width: int
    ) -> Path | None:
        """A page no wider than `width`, or None to serve the original."""
        if width not in self.ALLOWED_WIDTHS:
            raise ValueError("rendition width must be 640, 960, or 1280")
        if not self._worth_it(page, width):
            return None
        if cached := self.cached(publication, page, width):
            return cached
        destination = self._destination(publication, page, width)
        lock = self._locks[hash((publication.id, page.number)) % len(self._locks)]
        with self._slots, lock:
            if destination.is_file():
                return destination
            try:
                with self._archives.open_page_file(publication, page) as source:
                    self._renderer.render(source, destination, width)
            except (OSError, Image.DecompressionBombError) as error:
                raise ArchiveUnavailable("page could not be resized") from error
            self._budget.added(destination, destination.stat().st_size)
        return destination

    def _worth_it(self, page: Page, width: int) -> bool:
        # Re-encoding a page that is already small buys nothing and can cost
        # bytes, so the original stays the better answer.
        if self._settings.rendition_cache_mb == 0:
            return False
        return page.width is None or page.width > width

    def _destination(self, publication: Publication, page: Page, width: int) -> Path:
        return (
            self._settings.rendition_dir
            / publication.id
            / f"{publication.revision}-{page.number}-{page.crc:08x}-{width}.webp"
        )


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

    def cached(self, publication: Publication, page: Page) -> Path | None:
        if not self._cacheable(page):
            return None
        destination = self._destination(publication, page)
        if not self._is_complete(destination, page):
            return None
        os.utime(destination, None)  # keep hot pages away from the budget
        return destination

    def page(self, publication: Publication, page: Page) -> Path | None:
        if not self._cacheable(page):
            return None
        if cached := self.cached(publication, page):
            return cached
        destination = self._destination(publication, page)
        lock = self._locks[hash((publication.id, page.number)) % len(self._locks)]
        with self._slots, lock:
            if self._is_complete(destination, page):
                return destination
            self._materialise(publication, page, destination)
            self._budget.added(destination, destination.stat().st_size)
        return destination

    def _cacheable(self, page: Page) -> bool:
        cache_limit = self._settings.page_cache_mb * 1024 * 1024
        return cache_limit != 0 and page.uncompressed_size <= cache_limit

    def _destination(self, publication: Publication, page: Page) -> Path:
        return (
            self._settings.page_cache_dir
            / publication.id
            / f"{publication.revision}-{page.number}-{page.crc:08x}.page"
        )

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
