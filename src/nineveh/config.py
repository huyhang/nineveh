from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path("/data")
    state_dir: Path = Path("/state")
    service_title: str = "Nineveh"
    public_base_url: str | None = None
    secure_cookies: bool = True
    session_hours: int = 168
    scan_interval_seconds: int = 900
    page_range_limit: int = 100
    feed_page_size: int = 24
    archive_cache_size: int = 4
    max_archive_entries: int = 10_000
    max_archive_uncompressed_bytes: int = 8 * 1024 * 1024 * 1024
    max_page_uncompressed_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: int = 200
    max_image_pixels: int = 200_000_000
    thumbnail_cache_mb: int = 512
    page_cache_mb: int = 1024
    # Concurrency ceilings for the two CPU/memory-heavy code paths. Each password
    # hash holds ~19 MiB for the duration of the Argon2id verification, and each
    # extraction slot streams one page into the cache.
    hash_workers: int = 2
    extract_workers: int = 2
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str | None = None
    bootstrap_admin_password_file: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        password_file = os.getenv("NINEVEH_ADMIN_PASSWORD_FILE")
        return cls(
            data_dir=Path(os.getenv("NINEVEH_DATA_DIR", "/data")),
            state_dir=Path(os.getenv("NINEVEH_STATE_DIR", "/state")),
            service_title=os.getenv("NINEVEH_TITLE", "Nineveh"),
            public_base_url=os.getenv("NINEVEH_PUBLIC_BASE_URL") or None,
            secure_cookies=_bool_env("NINEVEH_SECURE_COOKIES", True),
            session_hours=_int_env("NINEVEH_SESSION_HOURS", 168, 1),
            scan_interval_seconds=_int_env("NINEVEH_SCAN_INTERVAL_SECONDS", 900, 0),
            page_range_limit=_int_env("NINEVEH_PAGE_RANGE_LIMIT", 100, 1),
            feed_page_size=_int_env("NINEVEH_FEED_PAGE_SIZE", 24, 1),
            archive_cache_size=_int_env("NINEVEH_ARCHIVE_CACHE_SIZE", 4, 0),
            max_archive_entries=_int_env("NINEVEH_MAX_ARCHIVE_ENTRIES", 10_000, 1),
            max_archive_uncompressed_bytes=_int_env(
                "NINEVEH_MAX_ARCHIVE_UNCOMPRESSED_BYTES", 8 * 1024 * 1024 * 1024, 1
            ),
            max_page_uncompressed_bytes=_int_env(
                "NINEVEH_MAX_PAGE_UNCOMPRESSED_BYTES", 256 * 1024 * 1024, 1
            ),
            max_compression_ratio=_int_env("NINEVEH_MAX_COMPRESSION_RATIO", 200, 1),
            max_image_pixels=_int_env("NINEVEH_MAX_IMAGE_PIXELS", 200_000_000, 1),
            thumbnail_cache_mb=_int_env("NINEVEH_THUMBNAIL_CACHE_MB", 512, 1),
            page_cache_mb=_int_env("NINEVEH_PAGE_CACHE_MB", 1024, 0),
            hash_workers=_int_env("NINEVEH_HASH_WORKERS", 2, 1),
            extract_workers=_int_env("NINEVEH_EXTRACT_WORKERS", 2, 1),
            bootstrap_admin_username=os.getenv("NINEVEH_ADMIN_USERNAME", "admin"),
            bootstrap_admin_password=os.getenv("NINEVEH_ADMIN_PASSWORD") or None,
            bootstrap_admin_password_file=Path(password_file)
            if password_file
            else None,
        )

    @property
    def database_path(self) -> Path:
        return self.state_dir / "nineveh.sqlite3"

    @property
    def thumbnail_dir(self) -> Path:
        return self.state_dir / "thumbnails"

    @property
    def page_cache_dir(self) -> Path:
        return self.state_dir / "page-cache"

    @property
    def range_dir(self) -> Path:
        """Scratch space for generated page-range archives.

        Deliberately separate from `page_cache_dir`: a large generated archive
        must not count against — or be evicted by — the page cache budget.
        """
        return self.state_dir / "ranges"

    def admin_password(self) -> str | None:
        if self.bootstrap_admin_password:
            return self.bootstrap_admin_password
        if self.bootstrap_admin_password_file:
            return self.bootstrap_admin_password_file.read_text(
                encoding="utf-8"
            ).strip()
        return None
