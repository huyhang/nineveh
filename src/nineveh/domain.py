from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class User:
    id: str
    username: str
    password_hash: str
    is_admin: bool
    enabled: bool
    created_at: datetime


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


@dataclass(frozen=True, slots=True)
class ScannedPublication:
    publication: Publication
    pages: tuple[Page, ...]


@dataclass(frozen=True, slots=True)
class PublicationPage:
    publication: Publication
    page: Page


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
