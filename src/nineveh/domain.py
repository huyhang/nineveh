from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class User:
    id: str
    username: str
    password_hash: str
    is_admin: bool
    enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ManagedLibrary:
    id: str
    name: str
    relative_path: str
    enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AccessGrant:
    user_id: str
    library_id: str
    category: str | None = None
    series_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReadScope:
    unrestricted: bool = False
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class SeriesUsage:
    id: str
    name: str
    publication_count: int
    size: int


@dataclass(frozen=True, slots=True)
class CatalogSeries:
    """A locally indexed series and the publication used for its fallback cover."""

    id: str
    library_id: str
    library: str
    category: str
    name: str
    publication_count: int
    first_publication_id: str
    first_publication_revision: str


@dataclass(frozen=True, slots=True)
class MetadataCandidate:
    provider_id: int
    title: str
    alternative_titles: tuple[str, ...]
    authors: tuple[str, ...]
    artists: tuple[str, ...]
    description: str | None
    year: int | None
    media_type: str | None
    status: str | None
    rating: float | None
    publishers: tuple[str, ...]
    tags: tuple[str, ...]
    cover_url: str | None
    source_url: str | None


@dataclass(frozen=True, slots=True)
class MetadataLookup:
    series_id: str
    candidates: tuple[MetadataCandidate, ...]
    searched_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SeriesMetadataSummary:
    """Just enough stored metadata to label a series in a list.

    Listing pages want a title and "is this matched?", not the retained
    provider payload, which runs to six figures of JSON per series.
    """

    series_id: str
    provider_id: int
    title: str | None


@dataclass(frozen=True, slots=True)
class SeriesMetadataState:
    """Everything the administration list filters on, in one cheap row.

    Deliberately separate from `SeriesMetadataSummary`: a series whose only
    record is a failed lookup belongs here but has no metadata, and letting it
    into the summary would mark it as matched on the reader-facing pages.
    """

    series_id: str
    title: str | None = None
    provider_id: int | None = None
    matched: bool = False
    edited: bool = False
    failed: bool = False
    fetched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SeriesMetadata:
    """Provider data plus sparse administrator-owned field overrides."""

    series_id: str
    provider: str
    provider_id: int
    canonical_url: str
    values: dict[str, Any]
    overrides: dict[str, Any]
    raw: dict[str, Any]
    fetched_at: datetime
    provider_updated_at: str | None = None

    @property
    def effective(self) -> dict[str, Any]:
        return {**self.values, **self.overrides}

    @property
    def title(self) -> str | None:
        """The display title, so summaries, states and records all read alike."""
        value = self.effective.get("title")
        return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class CategoryUsage:
    name: str
    publication_count: int
    size: int
    series: tuple[SeriesUsage, ...]


@dataclass(frozen=True, slots=True)
class LibraryUsage:
    library: ManagedLibrary
    publication_count: int
    size: int
    categories: tuple[CategoryUsage, ...]


@dataclass(frozen=True, slots=True)
class Session:
    token: str
    csrf_token: str
    user: User
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    member_name: str
    media_type: str
    compressed_size: int
    uncompressed_size: int
    crc: int
    width: int | None = None
    height: int | None = None
    is_spread: bool = False


@dataclass(frozen=True, slots=True)
class Publication:
    id: str
    relative_path: str
    library: str
    category: str
    series: str
    filename: str
    title: str
    number: str | None
    description: str | None
    authors: tuple[str, ...]
    modified_ns: int
    size: int
    revision: str
    page_count: int
    cover_page: int
    library_id: str | None = None
    series_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScannedPublication:
    publication: Publication
    pages: tuple[Page, ...]


@dataclass(frozen=True, slots=True)
class PublicationPage:
    publication: Publication
    page: Page


@dataclass(frozen=True, slots=True)
class ReadingProgress:
    user_id: str
    publication_id: str
    page: int
    mode: str
    completed: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReadingState:
    page: int
    mode: str
    completed: bool
    progress_updated_at: datetime | None
    mode_updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReaderContext:
    publication: Publication
    publications: tuple[Publication, ...]
    position: int
    previous: Publication | None
    next: Publication | None


@dataclass(frozen=True, slots=True)
class ScanReport:
    discovered: int
    indexed: int
    unchanged: int
    removed: int
    failed: int


@dataclass(frozen=True, slots=True)
class ScanStatus:
    running: bool = False
    started_at: str | None = None
    completed_at: str | None = None
    catalog_modified_at: str | None = None
    report: ScanReport | None = None
    error: str | None = None
    library_id: str | None = None
