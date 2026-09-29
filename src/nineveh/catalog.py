from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol
from xml.etree import ElementTree

from .config import Settings
from .domain import (
    DEFAULT_MOUNT_ID,
    CatalogSeries,
    ManagedLibrary,
    Page,
    Publication,
    ScannedPublication,
    ScanReport,
    ScanStatus,
)
from .ports import CatalogRepository, LibraryRepository, MountRepository
from .storage import StorageError, StoragePathResolver


class LibraryManagementRepository(LibraryRepository, MountRepository, Protocol):
    pass


class CatalogManagementRepository(
    CatalogRepository, LibraryManagementRepository, Protocol
):
    pass


class SeriesManagementRepository(Protocol):
    def set_series_private(
        self, series_id: str, is_private: bool
    ) -> CatalogSeries | None: ...


class SeriesService:
    """Administrative series changes, independent of the HTTP transport."""

    def __init__(self, repository: SeriesManagementRepository) -> None:
        self._repository = repository

    def set_private(self, series_id: str, is_private: bool) -> CatalogSeries | None:
        return self._repository.set_series_private(series_id, is_private)


__all__ = [
    "ArchiveInspector",
    "CatalogScanner",
    "InvalidLibrary",
    "LibraryService",
    "ScanStatus",
    "SeriesService",
    "UnsafeArchive",
]

LOGGER = logging.getLogger(__name__)

IMAGE_TYPES = {
    ".avif": "image/avif",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
NATURAL_PARTS = re.compile(r"(\d+)")
LIBRARY_NAME_LIMIT = 80


class UnsafeArchive(ValueError):
    pass


class InvalidLibrary(ValueError):
    pass


class LibraryService:
    """Manages safe, direct children of each registered data mount."""

    def __init__(
        self,
        repository: LibraryManagementRepository,
        paths: StoragePathResolver,
    ) -> None:
        self._repository = repository
        self._paths = paths

    def initialize(self) -> None:
        """Adopt the configured root's directories, once, on a fresh install.

        Only the original mount: a mount an administrator registers later has
        its libraries chosen by hand, and silently adopting everything on it
        would undo a deliberate omission at the next restart.
        """
        mount = self._repository.data_mount(DEFAULT_MOUNT_ID)
        if mount is None:  # pragma: no cover - ensured before the app starts
            return
        self._repository.initialize_libraries(
            [entry.name for entry in _directories(Path(mount.path))], mount.id
        )

    def available(self, mount_id: str = DEFAULT_MOUNT_ID) -> list[str]:
        mount = self._repository.data_mount(mount_id)
        if mount is None:
            return []
        managed = {
            item.relative_path.casefold()
            for item in self._repository.managed_libraries(mount_id=mount_id)
        }
        return [
            entry.name
            for entry in _directories(Path(mount.path))
            if entry.name.casefold() not in managed
        ]

    def available_by_mount(self) -> dict[str, list[str]]:
        return {
            mount.id: self.available(mount.id)
            for mount in self._repository.data_mounts(include_disabled=False)
        }

    def add(
        self,
        relative_path: str,
        mount_id: str = DEFAULT_MOUNT_ID,
        name: str | None = None,
    ) -> ManagedLibrary:
        candidate = _direct_child(relative_path)
        mount = self._repository.data_mount(mount_id)
        if mount is None or not mount.enabled:
            raise InvalidLibrary("Select a connected data mount")
        if candidate not in {entry.name for entry in _directories(Path(mount.path))}:
            raise InvalidLibrary(f"No directory named {candidate} on {mount.name}")
        try:
            return self._repository.add_library(
                candidate, mount_id, _display_name(name or candidate)
            )
        except ValueError as error:
            raise InvalidLibrary(str(error)) from error

    def remove(self, library_id: str) -> ManagedLibrary:
        library = self._repository.remove_library(library_id)
        if not library:
            raise InvalidLibrary("Managed library not found")
        return library


def _direct_child(relative_path: str) -> str:
    candidate = relative_path.strip()
    if not candidate or Path(candidate).name != candidate or candidate in {".", ".."}:
        raise InvalidLibrary("Select a top-level directory on a data mount")
    return candidate


def _display_name(value: str) -> str:
    cleaned = " ".join(value.split())
    if not 1 <= len(cleaned) <= LIBRARY_NAME_LIMIT:
        raise InvalidLibrary(f"Library name must be 1-{LIBRARY_NAME_LIMIT} characters")
    return cleaned


class ArchiveInspector:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def inspect(
        self,
        path: Path,
        relative_path: str,
        publication_id: str,
        *,
        library_name: str | None = None,
        library_id: str | None = None,
    ) -> ScannedPublication:
        before = path.stat()
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            self._validate_archive(infos)
            image_infos = self._ordered_images(infos)
            metadata = self._metadata(archive, infos)
            revision = self._revision(before.st_size, infos)

        after = path.stat()
        if before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
            raise UnsafeArchive("archive changed while it was being indexed")

        publication = self._publication(
            path,
            relative_path,
            publication_id,
            after,
            revision,
            image_infos,
            metadata,
            library_name=library_name,
            library_id=library_id,
        )
        return ScannedPublication(
            publication,
            self._pages(image_infos, frozenset(metadata.get("spread_pages", ()))),
        )

    def _ordered_images(self, infos: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
        image_infos = [
            info
            for info in infos
            if not info.is_dir() and self._image_type(info.filename)
        ]
        image_infos.sort(
            key=lambda info: _natural_key(_safe_member_name(info.filename))
        )
        if not image_infos:
            raise UnsafeArchive("archive contains no supported images")
        return image_infos

    def _publication(
        self,
        path: Path,
        relative_path: str,
        publication_id: str,
        stat: os.stat_result,
        revision: str,
        image_infos: list[zipfile.ZipInfo],
        metadata: dict[str, object],
        *,
        library_name: str | None = None,
        library_id: str | None = None,
    ) -> Publication:
        # The folder name only names the library when the scanner did not say
        # which one it was reading: a library may be displayed under an alias.
        folder, category, series, _ = PurePosixPath(relative_path).parts
        cover_page = metadata.get("cover_page", 1)
        if not isinstance(cover_page, int) or not 1 <= cover_page <= len(image_infos):
            cover_page = 1
        return Publication(
            id=publication_id,
            relative_path=relative_path,
            library=library_name or folder,
            category=category,
            series=series,
            filename=path.name,
            title=metadata.get("title") or path.stem,
            number=metadata.get("number") or None,
            description=metadata.get("description") or None,
            authors=tuple(metadata.get("authors", ())),
            modified_ns=stat.st_mtime_ns,
            size=stat.st_size,
            revision=revision,
            page_count=len(image_infos),
            cover_page=cover_page,
            library_id=library_id,
        )

    def _pages(
        self, image_infos: list[zipfile.ZipInfo], spread_pages: frozenset[int]
    ) -> tuple[Page, ...]:
        return tuple(
            Page(
                number=index,
                member_name=_safe_member_name(info.filename),
                media_type=self._image_type(info.filename)
                or "application/octet-stream",
                compressed_size=info.compress_size,
                uncompressed_size=info.file_size,
                crc=info.CRC,
                is_spread=index in spread_pages,
            )
            for index, info in enumerate(image_infos, start=1)
        )

    def _validate_archive(self, infos: list[zipfile.ZipInfo]) -> None:
        if len(infos) > self._settings.max_archive_entries:
            raise UnsafeArchive("archive contains too many entries")
        total_size = 0
        seen: set[str] = set()
        for info in infos:
            name = _safe_member_name(info.filename)
            if name in seen:
                raise UnsafeArchive(f"archive contains a duplicate entry: {name}")
            seen.add(name)
            if info.flag_bits & 0x1:
                raise UnsafeArchive("encrypted archives are not supported")
            total_size += info.file_size
            if total_size > self._settings.max_archive_uncompressed_bytes:
                raise UnsafeArchive("archive expands beyond the configured limit")
            if self._image_type(name):
                if info.file_size > self._settings.max_page_uncompressed_bytes:
                    raise UnsafeArchive(f"page exceeds the configured limit: {name}")
                if info.file_size >= 10 * 1024 * 1024:
                    ratio = info.file_size / max(info.compress_size, 1)
                    if ratio > self._settings.max_compression_ratio:
                        raise UnsafeArchive(
                            f"page has a suspicious compression ratio: {name}"
                        )

    @staticmethod
    def _metadata(
        archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]
    ) -> dict[str, object]:
        info = next(
            (
                item
                for item in infos
                if PurePosixPath(_safe_member_name(item.filename)).name.casefold()
                == "comicinfo.xml"
            ),
            None,
        )
        if not info or info.file_size > 1024 * 1024:
            return {}
        try:
            root = ElementTree.fromstring(archive.read(info))
        except (ElementTree.ParseError, UnicodeError, RuntimeError, zipfile.BadZipFile):
            return {}

        def text(name: str) -> str | None:
            value = root.findtext(name)
            return value.strip() if value and value.strip() else None

        authors: list[str] = []
        for field in ("Writer", "Penciller", "Inker", "Colorist"):
            value = text(field)
            if value and value not in authors:
                authors.append(value)
        cover_page = 1
        cover_found = False
        spread_pages: set[int] = set()
        for page in root.findall("./Pages/Page"):
            try:
                number = int(page.attrib["Image"]) + 1
            except (KeyError, ValueError):
                continue
            if (
                not cover_found
                and "frontcover" in page.attrib.get("Type", "").casefold()
            ):
                cover_page = number
                cover_found = True
            if page.attrib.get("DoublePage", "").casefold() in {
                "1",
                "true",
                "yes",
            }:
                spread_pages.add(number)
        return {
            "title": text("Title"),
            "number": text("Number"),
            "description": text("Summary"),
            "authors": authors,
            "cover_page": cover_page,
            "spread_pages": spread_pages,
        }

    @staticmethod
    def _revision(size: int, infos: list[zipfile.ZipInfo]) -> str:
        digest = hashlib.sha256(str(size).encode("ascii"))
        for info in infos:
            digest.update(info.filename.encode("utf-8", errors="surrogatepass"))
            digest.update(
                f":{info.CRC}:{info.file_size}:{info.compress_size}".encode("ascii")
            )
        return digest.hexdigest()[:24]

    @staticmethod
    def _image_type(name: str) -> str | None:
        return IMAGE_TYPES.get(PurePosixPath(name).suffix.casefold())


class CatalogScanner:
    def __init__(
        self,
        repository: CatalogManagementRepository,
        inspector: ArchiveInspector,
        paths: StoragePathResolver,
        libraries: LibraryService,
    ) -> None:
        self._repository = repository
        self._inspector = inspector
        self._paths = paths
        self._libraries = libraries
        self._run_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status = ScanStatus()

    @property
    def status(self) -> ScanStatus:
        with self._status_lock:
            return self._status

    def scan(self, library_id: str | None = None) -> ScanReport:
        return self._run(library_id, lambda: self._scan_once(library_id))

    def scan_mount(self, mount_id: str) -> ScanReport:
        """Scan one mount's libraries, leaving every other mount untouched."""
        if self._repository.data_mount(mount_id) is None:
            raise InvalidLibrary("Data mount not found")
        return self._run(None, lambda: self._scan_libraries(self._on_mount(mount_id)))

    def _on_mount(self, mount_id: str) -> list[ManagedLibrary]:
        return self._repository.managed_libraries(mount_id=mount_id)

    def _run(
        self, library_id: str | None, work: Callable[[], ScanReport]
    ) -> ScanReport:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("A catalog scan is already running")
        started = _now_iso()
        previous = self.status
        self._set_status(
            replace(
                previous,
                running=True,
                started_at=started,
                error=None,
                library_id=library_id,
            )
        )
        try:
            report = work()
        except Exception as error:
            LOGGER.exception("Catalog scan failed")
            self._set_status(
                replace(
                    previous,
                    running=False,
                    started_at=started,
                    completed_at=_now_iso(),
                    error=str(error),
                    library_id=library_id,
                )
            )
            raise
        else:
            self._set_status(_completed_status(previous, started, report, library_id))
            return report
        finally:
            self._run_lock.release()

    def _scan_once(self, library_id: str | None) -> ScanReport:
        if not self._repository.managed_libraries(include_disabled=True):
            self._libraries.initialize()
        return self._scan_libraries(self._libraries_for_scan(library_id))

    def _scan_libraries(self, libraries: list[ManagedLibrary]) -> ScanReport:
        """Scan each library on its own mount, and report what was skipped.

        A library whose storage is unavailable is left exactly as it was
        indexed: unplugging a disk must never look like deleting a library.
        """
        totals = ScanReport(0, 0, 0, 0, 0)
        for library in libraries:
            try:
                report = self._scan_library(library)
            except (OSError, StorageError) as error:
                LOGGER.warning("Leaving %s indexed: %s", library.name, error)
                totals = _add_reports(totals, ScanReport(0, 0, 0, 0, 1))
                continue
            totals = _add_reports(totals, report)
        return totals

    def _scan_library(self, library: ManagedLibrary) -> ScanReport:
        root = self._library_root(library)
        paths = list(self._discover(library, root))
        mount_root = Path(self._paths.mount_for_library(library).path)
        seen: set[str] = set()
        indexed = unchanged = failed = 0
        for path in paths:
            relative_path = path.relative_to(mount_root).as_posix()
            seen.add(relative_path)
            try:
                stat = path.stat()
                existing = self._repository.publication_by_path(
                    relative_path, library.id
                )
                if (
                    existing
                    and existing.modified_ns == stat.st_mtime_ns
                    and existing.size == stat.st_size
                ):
                    unchanged += 1
                    continue
                publication_id = existing.id if existing else str(uuid.uuid4())
                scanned = self._inspector.inspect(
                    path,
                    relative_path,
                    publication_id,
                    library_name=library.name,
                    library_id=library.id,
                )
                self._repository.upsert_publication(scanned)
                indexed += 1
            except (OSError, UnsafeArchive, zipfile.BadZipFile) as error:
                failed += 1
                LOGGER.warning("Skipping %s: %s", relative_path, error)
        removed = self._repository.remove_publications_except(seen, library.id)
        return ScanReport(len(paths), indexed, unchanged, removed, failed)

    def _library_root(self, library: ManagedLibrary) -> Path:
        mount = self._paths.mount_for_library(library)
        if not mount.enabled:
            raise StorageError(f"{mount.name} is disconnected")
        if not mount.scan_enabled:
            raise StorageError(f"{mount.name} is excluded from scans")
        root = self._paths.library_root(library)
        if not root.is_dir() or root.is_symlink():
            raise FileNotFoundError(f"library directory is unavailable: {root}")
        return root

    def _libraries_for_scan(self, library_id: str | None) -> list[ManagedLibrary]:
        if library_id is None:
            return self._repository.managed_libraries()
        library = self._repository.managed_library(library_id)
        if not library or not library.enabled:
            raise InvalidLibrary("Managed library not found")
        return [library]

    def _discover(self, library: ManagedLibrary, root: Path):
        for category_name in ("comics", "manga"):
            category = root / category_name
            if not category.is_dir() or category.is_symlink():
                continue
            for series in _directories(category):
                try:
                    entries = list(os.scandir(series.path))
                except OSError as error:
                    LOGGER.warning(
                        "Cannot read series directory %s: %s", series.path, error
                    )
                    continue
                for entry in sorted(entries, key=lambda item: _natural_key(item.name)):
                    if entry.name.casefold().endswith(".cbz") and entry.is_file(
                        follow_symlinks=False
                    ):
                        yield Path(entry.path)

    def _set_status(self, status: ScanStatus) -> None:
        with self._status_lock:
            self._status = status


def _add_reports(left: ScanReport, right: ScanReport) -> ScanReport:
    return ScanReport(
        left.discovered + right.discovered,
        left.indexed + right.indexed,
        left.unchanged + right.unchanged,
        left.removed + right.removed,
        left.failed + right.failed,
    )


def _completed_status(
    previous: ScanStatus,
    started: str,
    report: ScanReport,
    library_id: str | None,
) -> ScanStatus:
    changed = bool(report.indexed or report.removed)
    return ScanStatus(
        running=False,
        started_at=started,
        completed_at=_now_iso(),
        catalog_modified_at=_now_iso()
        if changed or previous.catalog_modified_at is None
        else previous.catalog_modified_at,
        report=report,
        library_id=library_id,
    )


@dataclass(frozen=True, slots=True)
class _Directory:
    name: str
    path: Path


def _directories(path: Path) -> list[_Directory]:
    try:
        entries = os.scandir(path)
    except OSError as error:
        LOGGER.warning("Cannot read directory %s: %s", path, error)
        return []
    with entries:
        directories = [
            _Directory(entry.name, Path(entry.path))
            for entry in entries
            if entry.is_dir(follow_symlinks=False)
        ]
    return sorted(directories, key=lambda item: _natural_key(item.name))


def _safe_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or "\x00" in normalized
    ):
        raise UnsafeArchive(f"unsafe archive entry: {name!r}")
    return path.as_posix()


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in NATURAL_PARTS.split(value)
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
