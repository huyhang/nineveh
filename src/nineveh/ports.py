from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO, Protocol

from .domain import (
    Page,
    Publication,
    PublicationPage,
    ScannedPublication,
    ScanReport,
    ScanStatus,
    User,
)


class CatalogRepository(Protocol):
    def publication_by_id(self, publication_id: str) -> Publication | None: ...

    def publication_by_path(self, relative_path: str) -> Publication | None: ...

    def page(self, publication_id: str, number: int) -> PublicationPage | None: ...

    def pages(self, publication_id: str, start: int, end: int) -> list[Page]: ...

    def update_page_dimensions(
        self, publication_id: str, dimensions: list[tuple[int, int, int]]
    ) -> None: ...

    def upsert_publication(self, scanned: ScannedPublication) -> None: ...

    def remove_publications_except(self, relative_paths: set[str]) -> int: ...

    def libraries(self) -> list[tuple[str, int]]: ...

    def categories(self, library: str) -> list[tuple[str, int]]: ...

    def series(self, library: str, category: str) -> list[tuple[str, int]]: ...

    def publications(
        self,
        *,
        library: str | None = None,
        category: str | None = None,
        series: str | None = None,
        query: str | None = None,
        limit: int = 24,
        offset: int = 0,
    ) -> tuple[list[Publication], int]: ...


class UserRepository(Protocol):
    def user_count(self) -> int: ...

    def user_by_username(self, username: str) -> User | None: ...

    def user_by_id(self, user_id: str) -> User | None: ...

    def users(self) -> list[User]: ...

    def create_user(
        self, username: str, password_hash: str, is_admin: bool
    ) -> User: ...

    def update_user(
        self,
        user_id: str,
        *,
        enabled: bool | None = None,
        password_hash: str | None = None,
    ) -> User | None: ...

    def enabled_admin_count(self) -> int: ...

    def create_session(
        self, user_id: str, token_hash: str, csrf_token: str, expires_at: str
    ) -> None: ...

    def session(self, token_hash: str, now: str) -> tuple[User, str, str] | None: ...

    def delete_session(self, token_hash: str) -> None: ...


class Repository(CatalogRepository, UserRepository, Protocol):
    """The single persistence seam the application composes against."""

    def initialize(self) -> None: ...

    def ping(self) -> bool: ...


class ArchiveSource(Protocol):
    def archive_path(self, publication: Publication) -> Path: ...

    def open_page(self, publication: Publication, page: Page) -> Iterator[bytes]: ...

    def open_page_file(
        self, publication: Publication, page: Page
    ) -> AbstractContextManager[BinaryIO]: ...

    def page_dimensions_many(
        self, publication: Publication, pages: list[Page]
    ) -> dict[int, tuple[int, int]]: ...

    def write_range(
        self, publication: Publication, pages: list[Page], destination: Path
    ) -> None: ...

    def close(self) -> None: ...


class ThumbnailRenderer(Protocol):
    def render(self, source: BinaryIO, destination: Path, width: int) -> None: ...


class CoverSource(Protocol):
    def cover(self, publication: Publication, page: Page, width: int) -> Path: ...


class PageStore(Protocol):
    def page(self, publication: Publication, page: Page) -> Path | None: ...


class CatalogScan(Protocol):
    @property
    def status(self) -> ScanStatus: ...

    def scan(self) -> ScanReport: ...
