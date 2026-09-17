"""In-memory stand-ins for the I/O ports, used to exercise the HTTP layer alone."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from nineveh.domain import Page, Publication, ScanReport, ScanStatus

PAGE_BYTES = b"\x89PNG\r\n\x1a\nfake-page-bytes"


class FakeArchives:
    """An `ArchiveSource` that serves canned bytes; no CBZ ever touches disk."""

    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.closed = False

    def archive_path(self, publication: Publication) -> Path:
        return Path("/nowhere") / publication.relative_path

    def open_page(self, publication: Publication, page: Page) -> Iterator[bytes]:
        return iter([PAGE_BYTES])

    @contextmanager
    def open_page_file(self, publication: Publication, page: Page):
        yield io.BytesIO(PAGE_BYTES)

    def page_dimensions_many(
        self, publication: Publication, pages: list[Page]
    ) -> dict[int, tuple[int, int]]:
        return {page.number: (120, 180) for page in pages}

    def write_range(
        self, publication: Publication, pages: list[Page], destination: Path
    ) -> None:
        with zipfile.ZipFile(destination, "w") as archive:
            for page in pages:
                archive.writestr(page.member_name, PAGE_BYTES)

    def close(self) -> None:
        self.closed = True


class FakeCovers:
    def __init__(self, path: Path) -> None:
        self._path = path

    def cover(self, publication: Publication, page: Page, width: int) -> Path:
        return self._path


class FakePageStore:
    """A `PageStore` that never caches, so responses take the streaming path."""

    def page(self, publication: Publication, page: Page) -> Path | None:
        return None


class FakeScanner:
    def __init__(self) -> None:
        self.runs = 0
        self.status = ScanStatus(
            completed_at="2026-01-01T00:00:00+00:00",
            catalog_modified_at="2026-01-01T00:00:00+00:00",
            report=ScanReport(0, 0, 0, 0, 0),
        )

    def scan(self) -> ScanReport:
        self.runs += 1
        return ScanReport(0, 0, 0, 0, 0)


def publication(publication_id: str = "fake-id", pages: int = 3) -> Publication:
    return Publication(
        id=publication_id,
        relative_path="Lib/comics/Series/Issue 1.cbz",
        library="Lib",
        category="comics",
        series="Series",
        filename="Issue 1.cbz",
        title="Issue 1",
        number="1",
        description=None,
        authors=("A. Writer",),
        modified_ns=1_700_000_000_000_000_000,
        size=4096,
        revision="rev1",
        page_count=pages,
        cover_page=1,
    )


def page(number: int) -> Page:
    return Page(
        number=number,
        member_name=f"pages/{number}.png",
        media_type="image/png",
        compressed_size=len(PAGE_BYTES),
        uncompressed_size=len(PAGE_BYTES),
        crc=number,
    )
