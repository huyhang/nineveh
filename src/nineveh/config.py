from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Protocol

LOGGER = logging.getLogger(__name__)


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
    EDITABLE_INTEGERS: ClassVar[dict[str, tuple[int, int]]] = {
        "session_hours": (1, 24 * 365),
        "scan_interval_seconds": (0, 30 * 24 * 3600),
        "page_range_limit": (1, 1_000),
        "feed_page_size": (1, 200),
        "archive_cache_size": (0, 128),
        "thumbnail_cache_mb": (1, 1024 * 1024),
        "page_cache_mb": (0, 1024 * 1024),
        "hash_workers": (1, 32),
        "extract_workers": (1, 32),
        "max_image_pixels": (1, 1_000_000_000),
    }
    # `public_base_url` is deliberately absent: it is a deployment fact owned by
    # whatever publishes the service, and it gates the browser login origin
    # check. Editing it from the UI is the one change that can lock an
    # administrator out of the UI they would need to undo it.
    EDITABLE_TEXT: ClassVar[set[str]] = {"service_title"}

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
    deployment_memory_limit: str | None = None
    restart_enabled: bool = False
    forwarded_allow_ips: str = "127.0.0.1"

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
            deployment_memory_limit=os.getenv("NINEVEH_MEMORY_LIMIT") or None,
            restart_enabled=_bool_env("NINEVEH_RESTART_ENABLED", False),
            forwarded_allow_ips=os.getenv("NINEVEH_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        )

    def with_overrides(self, values: Mapping[str, str]) -> Settings:
        unknown = set(values) - (set(self.EDITABLE_INTEGERS) | self.EDITABLE_TEXT)
        if unknown:
            raise ValueError(f"Unsupported setting: {min(unknown)}")
        updates: dict[str, object] = {}
        for name, value in values.items():
            if name in self.EDITABLE_TEXT:
                cleaned = value.strip()
                if name == "service_title" and not 1 <= len(cleaned) <= 80:
                    raise ValueError("Service title must be 1-80 characters")
                updates[name] = cleaned
                continue
            try:
                number = int(value)
            except ValueError as error:
                raise ValueError(f"{name} must be an integer") from error
            minimum, maximum = self.EDITABLE_INTEGERS[name]
            if not minimum <= number <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
            updates[name] = number
        return replace(self, **updates)

    def editable_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for name in sorted(set(self.EDITABLE_INTEGERS) | self.EDITABLE_TEXT):
            value = getattr(self, name)
            values[name] = "" if value is None else str(value)
        return values

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


class SettingsStore(Protocol):
    def settings(self) -> dict[str, str]: ...

    def replace_settings(self, values: dict[str, str]) -> None: ...


class SettingsService:
    """Administrator overrides layered over the deployment's own defaults.

    Only a value that actually differs from the deployment default is persisted.
    That keeps `docker/.env` authoritative for everything an administrator has
    not deliberately pinned in the UI — saving one field must not silently
    freeze the other eleven at whatever they happened to be that day.
    """

    def __init__(self, defaults: Settings, store: SettingsStore) -> None:
        self._defaults = defaults
        self._store = store
        self._startup = defaults

    def activate(self) -> Settings:
        """Drop overrides that are retired or match the defaults, then settle.

        Retired keys are discarded rather than rejected: a setting that moves
        back to the environment between releases must not leave an existing
        database unbootable.
        """
        stored = self._store.settings()
        live = {name: value for name, value in stored.items() if name in self._editable}
        for retired in sorted(set(stored) - set(live)):
            LOGGER.info("Discarding override for retired setting %s", retired)
        wanted = self._divergent(self._defaults.with_overrides(live))
        if wanted != stored:
            self._store.replace_settings(wanted)
        self._startup = self._defaults.with_overrides(wanted)
        return self._startup

    @property
    def _editable(self) -> set[str]:
        return set(Settings.EDITABLE_INTEGERS) | Settings.EDITABLE_TEXT

    def saved(self) -> Settings:
        return self._defaults.with_overrides(self._store.settings())

    def update(self, values: Mapping[str, str]) -> Settings:
        candidate = self.saved().with_overrides(values)
        self._store.replace_settings(self._divergent(candidate))
        return candidate

    def pending_restart(self) -> bool:
        return self.saved().editable_values() != self._startup.editable_values()

    def _divergent(self, candidate: Settings) -> dict[str, str]:
        defaults = self._defaults.editable_values()
        return {
            name: value
            for name, value in candidate.editable_values().items()
            if value != defaults[name]
        }
