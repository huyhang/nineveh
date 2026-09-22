from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from .domain import (
    AccessGrant,
    CatalogSeries,
    LibrarianEvent,
    LibrarianToken,
    LibraryUsage,
    ManagedLibrary,
    MetadataAutoMatchJob,
    MetadataCandidate,
    MetadataLookup,
    Page,
    Publication,
    PublicationPage,
    ReadingProgress,
    ReadScope,
    ScannedPublication,
    ScanReport,
    ScanStatus,
    SeriesMetadata,
    SeriesMetadataState,
    SeriesMetadataSummary,
    SpreadAnalysis,
    User,
)


class CatalogRepository(Protocol):
    def publication_by_id(
        self, publication_id: str, scope: ReadScope | None = None
    ) -> Publication | None: ...

    def publication_by_path(self, relative_path: str) -> Publication | None: ...

    def page(
        self, publication_id: str, number: int, scope: ReadScope | None = None
    ) -> PublicationPage | None: ...

    def pages(self, publication_id: str, start: int, end: int) -> list[Page]: ...

    def update_page_dimensions(
        self, publication_id: str, dimensions: list[tuple[int, int, int]]
    ) -> None: ...

    def upsert_publication(self, scanned: ScannedPublication) -> None: ...

    def remove_publications_except(
        self, relative_paths: set[str], library_id: str | None = None
    ) -> int: ...

    def libraries(self, scope: ReadScope | None = None) -> list[tuple[str, int]]: ...

    def categories(
        self, library: str, scope: ReadScope | None = None
    ) -> list[tuple[str, int]]: ...

    def series(
        self, library: str, category: str, scope: ReadScope | None = None
    ) -> list[tuple[str, int]]: ...

    def publications(
        self,
        *,
        library: str | None = None,
        category: str | None = None,
        series: str | None = None,
        query: str | None = None,
        limit: int = 24,
        offset: int = 0,
        scope: ReadScope | None = None,
    ) -> tuple[list[Publication], int]: ...

    def catalog_series(
        self,
        *,
        series_id: str | None = None,
        library_id: str | None = None,
        category: str | None = None,
        query: str | None = None,
        scope: ReadScope | None = None,
    ) -> list[CatalogSeries]: ...

    def catalog_series_by_id(
        self, series_id: str, scope: ReadScope | None = None
    ) -> CatalogSeries | None: ...

    def publications_in_series(
        self, series_id: str, scope: ReadScope | None = None
    ) -> list[Publication]: ...


class ReadingRepository(Protocol):
    def reading_progress(
        self, user_id: str, publication_id: str
    ) -> ReadingProgress | None: ...

    def reading_progress_for_publications(
        self, user_id: str, publication_ids: list[str]
    ) -> dict[str, ReadingProgress]: ...

    def latest_reading_progress(self, user_id: str) -> ReadingProgress | None: ...

    def save_reading_progress(
        self,
        user_id: str,
        publication_id: str,
        page: int,
        mode: str,
        completed: bool,
    ) -> ReadingProgress: ...

    def delete_reading_progress(self, user_id: str, publication_id: str) -> None: ...


class LibraryRepository(Protocol):
    def initialize_libraries(self, relative_paths: list[str]) -> None: ...

    def managed_libraries(
        self, *, include_disabled: bool = False
    ) -> list[ManagedLibrary]: ...

    def managed_library(self, library_id: str) -> ManagedLibrary | None: ...

    def add_library(self, relative_path: str) -> ManagedLibrary: ...

    def remove_library(self, library_id: str) -> ManagedLibrary | None: ...

    def library_usage(self) -> list[LibraryUsage]: ...


class AccessRepository(Protocol):
    def access_grants(self, user_id: str) -> list[AccessGrant]: ...

    def all_access_grants(self) -> dict[str, list[AccessGrant]]: ...

    def replace_access_grants(
        self, user_id: str, grants: list[AccessGrant]
    ) -> None: ...


class MetadataRepository(Protocol):
    def series_metadata(self, series_id: str) -> SeriesMetadata | None: ...

    def all_series_metadata(self) -> dict[str, SeriesMetadata]: ...

    def series_metadata_summaries(self) -> dict[str, SeriesMetadataSummary]: ...

    def series_metadata_states(self) -> dict[str, SeriesMetadataState]: ...

    def save_series_metadata(
        self,
        series_id: str,
        provider_id: int,
        canonical_url: str,
        values: dict[str, Any],
        raw: dict[str, Any],
        provider_updated_at: str | None,
    ) -> SeriesMetadata: ...

    def replace_metadata_overrides(
        self, series_id: str, overrides: dict[str, Any]
    ) -> SeriesMetadata: ...

    def delete_series_metadata(self, series_id: str) -> None: ...

    def metadata_lookup(self, series_id: str) -> MetadataLookup: ...

    def replace_metadata_lookup(
        self,
        series_id: str,
        candidates: list[MetadataCandidate],
        error: str | None = None,
    ) -> MetadataLookup: ...

    def reserve_metadata_request(
        self, limit: int, now: float, window_seconds: float = 60.0
    ) -> float: ...

    def create_metadata_auto_match_job(
        self, library_id: str, series_ids: list[str]
    ) -> MetadataAutoMatchJob: ...

    def active_metadata_auto_match_job(self) -> MetadataAutoMatchJob | None: ...

    def latest_metadata_auto_match_job(
        self, library_id: str
    ) -> MetadataAutoMatchJob | None: ...

    def pending_metadata_auto_match_series(self, job_id: str) -> list[str]: ...

    def finish_metadata_auto_match_item(
        self, job_id: str, series_id: str, status: str, detail: str | None = None
    ) -> None: ...

    def complete_metadata_auto_match_job(self, job_id: str) -> None: ...


class SpreadRepository(Protocol):
    def series_spread_detection(self, series_id: str) -> bool: ...

    def set_series_spread_detection(self, series_id: str, enabled: bool) -> bool: ...

    def spread_detection_series_ids(self) -> list[str]: ...

    def publication_spread_analysis(
        self, publication_id: str, revision: str
    ) -> SpreadAnalysis | None: ...

    def save_publication_spread_analysis(
        self,
        publication_id: str,
        revision: str,
        anchor_page: int | None,
        source: str | None = None,
    ) -> SpreadAnalysis: ...

    def publication_spread_override(self, publication_id: str) -> int | None: ...

    def publication_spread_overrides(self, series_id: str) -> dict[str, int]: ...

    def set_publication_spread_override(
        self, publication_id: str, anchor_page: int | None
    ) -> bool: ...


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

    def set_session_flash(
        self, token_hash: str, message: str | None, error: str | None
    ) -> None: ...

    def take_session_flash(self, token_hash: str) -> tuple[str | None, str | None]: ...


class LibrarianRepository(Protocol):
    def create_librarian_token(
        self,
        name: str,
        token_hash: str,
        scopes: tuple[str, ...],
        library_ids: tuple[str, ...],
    ) -> LibrarianToken: ...

    def librarian_token_by_hash(self, token_hash: str) -> LibrarianToken | None: ...

    def librarian_token(self, token_id: str) -> LibrarianToken | None: ...

    def librarian_tokens(
        self, *, include_revoked: bool = False
    ) -> list[LibrarianToken]: ...

    def update_librarian_token(
        self,
        token_id: str,
        *,
        name: str | None = None,
        scopes: tuple[str, ...] | None = None,
        library_ids: tuple[str, ...] | None = None,
    ) -> LibrarianToken | None: ...

    def revoke_librarian_token(self, token_id: str) -> LibrarianToken | None: ...

    def touch_librarian_token(self, token_id: str) -> None: ...

    def record_librarian_event(self, event: LibrarianEvent) -> LibrarianEvent: ...

    def purge_librarian_events(
        self, before: str, *, keep_severities: tuple[str, ...] = ()
    ) -> int: ...

    def librarian_events(
        self,
        *,
        token_id: str | None = None,
        severity: str | None = None,
        action: str | None = None,
        outcome: str | None = None,
        correlation_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[LibrarianEvent]: ...


class Repository(
    CatalogRepository,
    UserRepository,
    LibraryRepository,
    AccessRepository,
    MetadataRepository,
    ReadingRepository,
    SpreadRepository,
    LibrarianRepository,
    Protocol,
):
    """The single persistence seam the application composes against."""

    def initialize(self) -> None: ...

    def ping(self) -> bool: ...

    def settings(self) -> dict[str, str]: ...

    def replace_settings(self, values: dict[str, str]) -> None: ...


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


class RenditionSource(Protocol):
    def rendition(
        self, publication: Publication, page: Page, width: int
    ) -> Path | None: ...


class PageStore(Protocol):
    def page(self, publication: Publication, page: Page) -> Path | None: ...


class CatalogScan(Protocol):
    @property
    def status(self) -> ScanStatus: ...

    def scan(self, library_id: str | None = None) -> ScanReport: ...


class RestartController(Protocol):
    @property
    def enabled(self) -> bool: ...

    def request_restart(self) -> None: ...
