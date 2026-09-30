from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import (
    DEFAULT_MOUNT_ID,
    READ_COMPLETED,
    READ_IN_PROGRESS,
    READ_UNREAD,
    SEVERITY_ORDER,
    AccessGrant,
    CatalogSeries,
    CatalogVisibility,
    CategoryUsage,
    DataMount,
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
    SearchDocument,
    SearchFacet,
    SearchFacets,
    SearchFilters,
    SearchSuggestion,
    SearchVolume,
    SecurityEvent,
    SeriesMetadata,
    SeriesMetadataState,
    SeriesMetadataSummary,
    SeriesUsage,
    SpreadAnalysis,
    User,
)

SCHEMA_VERSION = 10
# The release that began recording `ComicInfo.xml` spread markers. Databases
# older than this need one reinspection pass to pick them up.
SPREAD_MARKER_VERSION = 4
# The release that replaced dimension-based spread detection with the gutter
# reader. An anchor stored by the old heuristic was the first wide page, which
# is not where pairing should start, so those rows have to be recomputed rather
# than trusted.
SEAM_DETECTION_VERSION = 6
# The release that made a stitched spread outrank the gutter. A volume with a
# wide page may have stored whatever the gutter read first, so every anchor but
# a wide page's own has to be recomputed.
WIDE_PAGE_FIRST_VERSION = 9
# The release that introduced data mounts and the search index.
MULTI_MOUNT_VERSION = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    -- One pending notice per session, consumed by the next page render. Kept
    -- here rather than in the redirect URL so a banner cannot be forged by
    -- handing an administrator a link, and never reaches the access log.
    flash_message TEXT,
    flash_error TEXT
);
CREATE INDEX IF NOT EXISTS sessions_expires_at ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS data_mounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    path TEXT NOT NULL UNIQUE,
    allow_ingest INTEGER NOT NULL DEFAULT 0,
    scan_enabled INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- A library's directory name is unique per mount, but its display name is
-- unique everywhere: it is what OPDS feeds, grants and the reader show, so two
-- libraries called "Manga" would be indistinguishable to a reader.
CREATE TABLE IF NOT EXISTS managed_libraries (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    relative_path TEXT NOT NULL COLLATE NOCASE,
    mount_id TEXT NOT NULL DEFAULT 'default' REFERENCES data_mounts(id),
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(mount_id, relative_path)
);

CREATE TABLE IF NOT EXISTS catalog_series (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL REFERENCES managed_libraries(id) ON DELETE CASCADE,
    category TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL COLLATE NOCASE,
    is_private INTEGER NOT NULL DEFAULT 0,
    UNIQUE(library_id, category, name)
);

-- Two mounts may each hold a "Manga/comics/Akira" path, so a stored path is
-- only unique within the library it was found in.
CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    library TEXT NOT NULL,
    category TEXT NOT NULL,
    series TEXT NOT NULL,
    filename TEXT NOT NULL,
    title TEXT NOT NULL,
    number TEXT,
    description TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    modified_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    revision TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    cover_page INTEGER NOT NULL DEFAULT 1,
    library_id TEXT REFERENCES managed_libraries(id),
    series_id TEXT REFERENCES catalog_series(id),
    UNIQUE(library_id, relative_path)
);
CREATE INDEX IF NOT EXISTS publications_hierarchy
    ON publications(library, category, series, title);

-- One row per series, rebuilt whenever its publications or metadata change.
-- Retrieval only: the index narrows the catalog to a candidate set, and
-- `search.py` scores and explains those candidates.
CREATE VIRTUAL TABLE IF NOT EXISTS catalog_search USING fts5(
    series_id UNINDEXED,
    local_title,
    canonical_title,
    alternate_titles,
    creators,
    publishers,
    tags,
    description,
    volume_titles,
    filenames,
    tokenize='unicode61 remove_diacritics 2'
);

-- The index's own term list, used to correct a misspelled query. Reading it
-- cannot leak anything: the corrected query still runs under the reader's
-- grants, so a term nobody may see simply returns nothing.
CREATE VIRTUAL TABLE IF NOT EXISTS catalog_search_terms
    USING fts5vocab(catalog_search, 'row');

-- Facet values are stored beside the index rather than parsed out of JSON on
-- every query, so counting them is a plain indexed join.
CREATE TABLE IF NOT EXISTS catalog_search_facets (
    series_id TEXT NOT NULL REFERENCES catalog_series(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized TEXT NOT NULL,
    PRIMARY KEY(series_id, kind, normalized)
);
CREATE INDEX IF NOT EXISTS catalog_search_facets_lookup
    ON catalog_search_facets(kind, normalized, series_id);

CREATE TABLE IF NOT EXISTS pages (
    publication_id TEXT NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    member_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    compressed_size INTEGER NOT NULL,
    uncompressed_size INTEGER NOT NULL,
    crc INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    is_spread INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(publication_id, number)
);

CREATE TABLE IF NOT EXISTS reading_progress (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    publication_id TEXT NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK(page_number > 0),
    mode TEXT NOT NULL CHECK(mode IN ('single', 'double', 'scroll')),
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, publication_id)
);
CREATE INDEX IF NOT EXISTS reading_progress_recent
    ON reading_progress(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS access_grants (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    library_id TEXT NOT NULL REFERENCES managed_libraries(id) ON DELETE CASCADE,
    category TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    series_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(user_id, library_id, category, series_id),
    CHECK(category IN ('', 'comics', 'manga')),
    CHECK(series_id = '' OR category != '')
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_metadata (
    series_id TEXT PRIMARY KEY REFERENCES catalog_series(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id INTEGER NOT NULL,
    canonical_url TEXT NOT NULL,
    values_json TEXT NOT NULL,
    overrides_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    provider_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS metadata_lookups (
    series_id TEXT PRIMARY KEY REFERENCES catalog_series(id) ON DELETE CASCADE,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    searched_at TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS metadata_rate_events (
    requested_at REAL PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS metadata_rate_events_time
    ON metadata_rate_events(requested_at);

CREATE TABLE IF NOT EXISTS metadata_auto_match_jobs (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL REFERENCES managed_libraries(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('running', 'complete')),
    total INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    linked INTEGER NOT NULL DEFAULT 0,
    review INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS metadata_auto_match_jobs_library
    ON metadata_auto_match_jobs(library_id, created_at DESC);

CREATE TABLE IF NOT EXISTS metadata_auto_match_items (
    job_id TEXT NOT NULL REFERENCES metadata_auto_match_jobs(id) ON DELETE CASCADE,
    series_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'linked', 'review', 'failed')),
    detail TEXT,
    PRIMARY KEY(job_id, series_id)
);

CREATE TABLE IF NOT EXISTS series_reader_settings (
    series_id TEXT PRIMARY KEY REFERENCES catalog_series(id) ON DELETE CASCADE,
    auto_spread_detection INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_spread_analysis (
    publication_id TEXT PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
    revision TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('detected', 'none')),
    anchor_page INTEGER,
    -- Which detector answered. Null on rows written before it was recorded.
    source TEXT,
    updated_at TEXT NOT NULL
);

-- An administrator's own answer for one volume, kept apart from the detected
-- one so re-analysis never overwrites it and clearing it restores detection.
CREATE TABLE IF NOT EXISTS publication_spread_overrides (
    publication_id TEXT PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
    anchor_page INTEGER NOT NULL CHECK(anchor_page >= 2),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- `token_hash` is nullable so revocation can clear it: the row must survive to
-- keep activity rows attributable, but the secret must stop authenticating even
-- if some future caller forgets to filter on `revoked_at`. SQLite permits many
-- NULLs under a UNIQUE constraint, so cleared hashes do not collide.
CREATE TABLE IF NOT EXISTS librarian_tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT UNIQUE,
    scopes TEXT NOT NULL,
    library_ids TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

-- Lifecycle and usage share one table. A reader wants a permission grant and
-- the first write it enabled on adjacent lines, which a single indexed scan
-- gives for free; retention is driven by `severity`, not by which table a row
-- landed in. `token_name` is denormalised so a row stays readable even if its
-- token is ever hard-deleted -- an orphaned id records an action by an agent
-- nobody can identify.
CREATE TABLE IF NOT EXISTS librarian_events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    token_id TEXT REFERENCES librarian_tokens(id) ON DELETE SET NULL,
    token_name TEXT NOT NULL DEFAULT '',
    actor TEXT,
    correlation_id TEXT,
    scopes_at_time TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    severity TEXT NOT NULL,
    outcome TEXT NOT NULL,
    subject_type TEXT,
    subject_id TEXT,
    subject_label TEXT,
    summary TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS librarian_events_recent
    ON librarian_events(created_at DESC);
CREATE INDEX IF NOT EXISTS librarian_events_token
    ON librarian_events(token_id, created_at DESC);
-- Agent reads are useful context but must not grow the database without
-- bound. Permission history (severity 'security') is never trimmed.
CREATE TRIGGER IF NOT EXISTS librarian_events_bound
AFTER INSERT ON librarian_events
WHEN (SELECT COUNT(*) FROM librarian_events WHERE severity != 'security') > 10000
BEGIN
    DELETE FROM librarian_events WHERE id IN (
        SELECT id FROM librarian_events WHERE severity != 'security'
        ORDER BY created_at ASC LIMIT 1000
    );
END;

-- Abuse-facing signals, kept apart from the agent feed: a password spray is
-- exactly the noise that would bury the permission history worth keeping.
CREATE TABLE IF NOT EXISTS security_events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS security_events_recent
    ON security_events(created_at DESC);
-- A spray may bury old signals, but it can never grow the table.
CREATE TRIGGER IF NOT EXISTS security_events_bound
AFTER INSERT ON security_events
WHEN (SELECT COUNT(*) FROM security_events) > 5000
BEGIN
    DELETE FROM security_events WHERE id IN (
        SELECT id FROM security_events ORDER BY created_at ASC LIMIT 500
    );
END;
"""

# Applied after `SCHEMA`, because a v1 database only grows the columns it indexes
# once `_ensure_scope_columns` has added them.
SCOPE_INDEX = """
CREATE INDEX IF NOT EXISTS publications_scope
    ON publications(library_id, category, series_id);
-- Search asks "does this series have a reachable volume?" once per candidate
-- row, and the composite index above cannot answer it without a library to
-- lead with.
CREATE INDEX IF NOT EXISTS publications_series
    ON publications(series_id);
"""


class SQLiteRepository:
    """Small persistence adapter shared by the catalog and authentication services."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than this Nineveh release"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            # Every statement below is replayable. `executescript` commits as it
            # goes, so an upgrade interrupted before `user_version` is bumped has
            # to survive being run again on the next start rather than wedging
            # the database on "table already exists".
            connection.executescript(SCHEMA)
            # Order matters on an old database: the identity columns have to
            # exist before the table that carries them is rebuilt, and the
            # default mount has to exist before a library can reference it.
            self._ensure_scope_columns(connection)
            self._ensure_default_mount_row(connection)
            self._ensure_library_mount_column(connection)
            self._ensure_publication_path_scope(connection)
            self._ensure_series_private_column(connection)
            self._ensure_progress_mode_column(connection)
            self._ensure_session_flash_columns(connection)
            self._ensure_page_spread_column(connection)
            self._ensure_spread_source_column(connection)
            connection.executescript(SCOPE_INDEX)
            if 0 < version < SPREAD_MARKER_VERSION:
                # Reinspect existing ComicInfo files once so the new spread
                # marker is populated without changing publication identities.
                connection.execute("UPDATE publications SET modified_ns = -1")
            if 0 < version < SEAM_DETECTION_VERSION:
                # Administrator overrides survive: only the detected anchors
                # are discarded, and the next scan recomputes them.
                connection.execute("DELETE FROM publication_spread_analysis")
            if 0 < version < WIDE_PAGE_FIRST_VERSION:
                # A wide page's anchor is what the new order finds first
                # anyway. Overrides live elsewhere and survive, as above.
                connection.execute(
                    "DELETE FROM publication_spread_analysis"
                    " WHERE source IS NOT 'wide page'"
                )
            if 0 < version < SCHEMA_VERSION:
                self._backfill_scope(connection)
            if version < MULTI_MOUNT_VERSION:
                # The search index is derived, so it is always safe to build
                # from scratch; a fresh database starts empty either way.
                self._rebuild_search_index(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _ensure_default_mount_row(connection: sqlite3.Connection) -> None:
        """Every library belongs to a mount, so one always exists.

        The path here is only a placeholder; `ensure_default_mount` replaces it
        with the directory this deployment is actually configured with.
        """
        now = _now_iso()
        connection.execute(
            """
            INSERT INTO data_mounts(
                id, name, path, allow_ingest, scan_enabled, enabled,
                created_at, updated_at
            ) VALUES (?, 'Primary', '/data', 1, 1, 1, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (DEFAULT_MOUNT_ID, now, now),
        )

    @staticmethod
    def _ensure_library_mount_column(connection: sqlite3.Connection) -> None:
        """Pre-mount libraries all lived under the single configured root.

        They move to the default mount, and uniqueness moves from the bare
        directory name to the mount it was found on: two disks may each hold a
        directory called "Manga".
        """
        present = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(managed_libraries)"
            ).fetchall()
        }
        has_mount = "mount_id" in present
        if has_mount and not _unique_index_on(
            connection, "managed_libraries", ["relative_path"]
        ):
            return
        # The column and the constraint move together, in one rebuild: SQLite
        # will not `ALTER TABLE ADD COLUMN` a REFERENCES column that has a
        # non-NULL default, and it cannot drop the old UNIQUE either.
        mount = "mount_id" if has_mount else f"'{DEFAULT_MOUNT_ID}'"
        _rebuild_table(
            connection,
            "managed_libraries",
            f"""
            CREATE TABLE managed_libraries_rebuilt (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                relative_path TEXT NOT NULL COLLATE NOCASE,
                mount_id TEXT NOT NULL DEFAULT '{DEFAULT_MOUNT_ID}'
                    REFERENCES data_mounts(id),
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(mount_id, relative_path)
            );
            INSERT INTO managed_libraries_rebuilt
                SELECT id, name, relative_path, {mount}, enabled, created_at
                FROM managed_libraries;
            """,
        )

    @staticmethod
    def _ensure_publication_path_scope(connection: sqlite3.Connection) -> None:
        """A stored path is unique within its library, not across the catalog."""
        if not _unique_index_on(connection, "publications", ["relative_path"]):
            return
        _rebuild_table(
            connection,
            "publications",
            """
            CREATE TABLE publications_rebuilt (
                id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL,
                library TEXT NOT NULL,
                category TEXT NOT NULL,
                series TEXT NOT NULL,
                filename TEXT NOT NULL,
                title TEXT NOT NULL,
                number TEXT,
                description TEXT,
                authors_json TEXT NOT NULL DEFAULT '[]',
                modified_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                revision TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                cover_page INTEGER NOT NULL DEFAULT 1,
                library_id TEXT REFERENCES managed_libraries(id),
                series_id TEXT REFERENCES catalog_series(id),
                UNIQUE(library_id, relative_path)
            );
            INSERT INTO publications_rebuilt
                SELECT id, relative_path, library, category, series, filename,
                       title, number, description, authors_json, modified_ns,
                       size, revision, page_count, cover_page, library_id,
                       series_id
                FROM publications;
            CREATE INDEX IF NOT EXISTS publications_hierarchy
                ON publications(library, category, series, title);
            """,
        )

    @staticmethod
    def _ensure_scope_columns(connection: sqlite3.Connection) -> None:
        """v1 publications predate the library and series identity columns."""
        present = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(publications)").fetchall()
        }
        if "library_id" not in present:
            connection.execute(
                "ALTER TABLE publications "
                "ADD COLUMN library_id TEXT REFERENCES managed_libraries(id)"
            )
        if "series_id" not in present:
            connection.execute(
                "ALTER TABLE publications "
                "ADD COLUMN series_id TEXT REFERENCES catalog_series(id)"
            )

    @staticmethod
    def _ensure_series_private_column(connection: sqlite3.Connection) -> None:
        """Existing series remain public when collection privacy is introduced."""
        present = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(catalog_series)"
            ).fetchall()
        }
        if "is_private" not in present:
            connection.execute(
                "ALTER TABLE catalog_series "
                "ADD COLUMN is_private INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _ensure_progress_mode_column(connection: sqlite3.Connection) -> None:
        """A pre-release v8 build dropped the legacy mode; older apps still read it."""
        present = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(reading_progress)"
            ).fetchall()
        }
        if "mode" not in present:
            connection.execute(
                "ALTER TABLE reading_progress ADD COLUMN mode TEXT NOT NULL "
                "DEFAULT 'single' CHECK(mode IN ('single', 'double', 'scroll'))"
            )

    @staticmethod
    def _ensure_session_flash_columns(connection: sqlite3.Connection) -> None:
        """Sessions created before notices moved out of the redirect URL."""
        present = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        for column in ("flash_message", "flash_error"):
            if column not in present:
                connection.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")

    @staticmethod
    def _ensure_spread_source_column(connection: sqlite3.Connection) -> None:
        """Anchors detected before this release did not record their evidence."""
        present = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(publication_spread_analysis)"
            ).fetchall()
        }
        if "source" not in present:
            connection.execute(
                "ALTER TABLE publication_spread_analysis ADD COLUMN source TEXT"
            )

    @staticmethod
    def _ensure_page_spread_column(connection: sqlite3.Connection) -> None:
        """Pages indexed before v4 did not retain ComicInfo spread markers."""
        present = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(pages)").fetchall()
        }
        if "is_spread" not in present:
            connection.execute(
                "ALTER TABLE pages ADD COLUMN is_spread INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _backfill_scope(connection: sqlite3.Connection) -> None:
        """Give pre-v2 rows their identities, preserving existing reader access."""
        now = _now_iso()
        connection.executemany(
            """
            INSERT OR IGNORE INTO managed_libraries(
                id, name, relative_path, enabled, created_at
            ) VALUES (?, ?, ?, 1, ?)
            """,
            [
                (str(uuid.uuid4()), row["library"], row["library"], now)
                for row in connection.execute(
                    "SELECT DISTINCT library FROM publications WHERE library_id IS NULL"
                ).fetchall()
            ],
        )
        connection.execute(
            """
            UPDATE publications SET library_id = (
                SELECT id FROM managed_libraries
                WHERE managed_libraries.name = publications.library COLLATE NOCASE
            ) WHERE library_id IS NULL
            """
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO catalog_series(id, library_id, category, name)
            VALUES (?, ?, ?, ?)
            """,
            [
                (str(uuid.uuid4()), row["library_id"], row["category"], row["series"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT library_id, category, series FROM publications
                    WHERE series_id IS NULL AND library_id IS NOT NULL
                    """
                ).fetchall()
            ],
        )
        connection.execute(
            """
            UPDATE publications SET series_id = (
                SELECT id FROM catalog_series
                WHERE catalog_series.library_id = publications.library_id
                  AND catalog_series.category = publications.category COLLATE NOCASE
                  AND catalog_series.name = publications.series COLLATE NOCASE
            ) WHERE series_id IS NULL
            """
        )
        # Seeding readers happens exactly once. The marker is what makes that
        # true even if the upgrade is replayed, and it stops a later replay from
        # handing back access an administrator has since revoked.
        seeded = connection.execute(
            "SELECT 1 FROM application_metadata WHERE key = 'libraries_initialized'"
        ).fetchone()
        if seeded:
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO access_grants(user_id, library_id, category, series_id)
            SELECT users.id, managed_libraries.id, '', ''
            FROM users CROSS JOIN managed_libraries
            WHERE users.is_admin = 0
            """
        )
        connection.execute(
            "INSERT INTO application_metadata(key, value)"
            " VALUES ('libraries_initialized', '1')"
        )

    def ping(self) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1

    def initialize_libraries(
        self, relative_paths: list[str], mount_id: str = DEFAULT_MOUNT_ID
    ) -> None:
        """Register a mount's children once, preserving later removals.

        A mount is only auto-populated the first time it is seen. After that
        an administrator's decision to remove a library has to stick, even
        across a rescan.
        """
        marker = f"libraries_initialized:{mount_id}"
        with self._connect() as connection:
            if self._already_initialized(connection, mount_id, marker):
                return
            now = _now_iso()
            # Two mounts can hold the same directory name, and display names
            # are unique, so the second one waits for an administrator to name
            # it rather than failing the whole registration.
            connection.executemany(
                """
                INSERT OR IGNORE INTO managed_libraries(
                    id, name, relative_path, mount_id, enabled, created_at
                ) VALUES (?, ?, ?, ?, 1, ?)
                """,
                [
                    (str(uuid.uuid4()), path, path, mount_id, now)
                    for path in relative_paths
                ],
            )
            connection.execute(
                "INSERT OR IGNORE INTO application_metadata(key, value) VALUES (?, '1')",
                (marker,),
            )

    @staticmethod
    def _already_initialized(
        connection: sqlite3.Connection, mount_id: str, marker: str
    ) -> bool:
        keys = [marker]
        if mount_id == DEFAULT_MOUNT_ID:
            # Databases from before mounts recorded the unqualified marker.
            keys.append("libraries_initialized")
        placeholders = ", ".join("?" for _ in keys)
        return bool(
            connection.execute(
                f"SELECT 1 FROM application_metadata WHERE key IN ({placeholders})",
                keys,
            ).fetchone()
        )

    def ensure_default_mount(self, path: str) -> DataMount:
        """Point the original mount at the configured data directory."""
        with self._connect() as connection:
            self._ensure_default_mount_row(connection)
            connection.execute(
                "UPDATE data_mounts SET path = ?, updated_at = ? "
                "WHERE id = ? AND path <> ?",
                (path, _now_iso(), DEFAULT_MOUNT_ID, path),
            )
        mount = self.data_mount(DEFAULT_MOUNT_ID)
        if mount is None:  # pragma: no cover - guaranteed by the insert above
            raise RuntimeError("The default data mount disappeared")
        return mount

    def data_mounts(self, *, include_disabled: bool = True) -> list[DataMount]:
        where = "" if include_disabled else " WHERE enabled = 1"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM data_mounts{where} "
                "ORDER BY id <> 'default', name COLLATE NOCASE"
            ).fetchall()
        return [self._data_mount(row) for row in rows]

    def data_mount(self, mount_id: str) -> DataMount | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_mounts WHERE id = ?", (mount_id,)
            ).fetchone()
        return self._data_mount(row) if row else None

    def add_data_mount(
        self,
        name: str,
        path: str,
        *,
        allow_ingest: bool = False,
        scan_enabled: bool = True,
    ) -> DataMount:
        mount_id = str(uuid.uuid4())
        now = _now_iso()
        with self._connect() as connection:
            self._insert_data_mount(
                connection, mount_id, name, path, allow_ingest, scan_enabled, now
            )
        mount = self.data_mount(mount_id)
        if mount is None:  # pragma: no cover - guaranteed by the insert above
            raise RuntimeError("The new data mount disappeared")
        return mount

    @staticmethod
    def _insert_data_mount(
        connection: sqlite3.Connection,
        mount_id: str,
        name: str,
        path: str,
        allow_ingest: bool,
        scan_enabled: bool,
        now: str,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO data_mounts(
                    id, name, path, allow_ingest, scan_enabled, enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (mount_id, name, path, int(allow_ingest), int(scan_enabled), now, now),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(_mount_conflict(connection, name, path)) from error

    def update_data_mount(
        self,
        mount_id: str,
        *,
        name: str,
        path: str,
        allow_ingest: bool,
        scan_enabled: bool,
    ) -> DataMount | None:
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    UPDATE data_mounts SET name = ?, path = ?, allow_ingest = ?,
                        scan_enabled = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        name,
                        path,
                        int(allow_ingest),
                        int(scan_enabled),
                        _now_iso(),
                        mount_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    _mount_conflict(connection, name, path, exclude_id=mount_id)
                ) from error
            changed = bool(cursor.rowcount)
        return self.data_mount(mount_id) if changed else None

    def set_data_mount_enabled(self, mount_id: str, enabled: bool) -> DataMount | None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE data_mounts SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), _now_iso(), mount_id),
            )
            changed = bool(cursor.rowcount)
        return self.data_mount(mount_id) if changed else None

    def forget_data_mount(self, mount_id: str) -> DataMount | None:
        """Drop the mount and everything the catalog recorded about it.

        Media files are never touched: only index rows, grants and the derived
        search entries go, which is exactly what re-adding the mount rebuilds.
        """
        mount = self.data_mount(mount_id)
        if mount is None:
            return None
        with self._connect() as connection:
            library_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM managed_libraries WHERE mount_id = ?", (mount_id,)
                ).fetchall()
            ]
            for library_id in library_ids:
                self._forget_library_rows(connection, library_id)
            connection.execute(
                "DELETE FROM managed_libraries WHERE mount_id = ?", (mount_id,)
            )
            connection.execute("DELETE FROM data_mounts WHERE id = ?", (mount_id,))
        return mount

    @staticmethod
    def _forget_library_rows(connection: sqlite3.Connection, library_id: str) -> None:
        series_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM catalog_series WHERE library_id = ?", (library_id,)
            ).fetchall()
        ]
        connection.executemany(
            "DELETE FROM catalog_search WHERE series_id = ?",
            ((series_id,) for series_id in series_ids),
        )
        connection.execute(
            "DELETE FROM publications WHERE library_id = ?", (library_id,)
        )
        connection.execute(
            "DELETE FROM access_grants WHERE library_id = ?", (library_id,)
        )
        connection.execute(
            "DELETE FROM catalog_series WHERE library_id = ?", (library_id,)
        )

    def mount_usage(self) -> dict[str, tuple[int, int, int]]:
        """Library count, publication count and indexed bytes per mount."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT managed_libraries.mount_id AS mount_id,
                       COUNT(DISTINCT managed_libraries.id) AS libraries,
                       COUNT(publications.id) AS publications,
                       COALESCE(SUM(publications.size), 0) AS size
                FROM managed_libraries
                LEFT JOIN publications
                  ON publications.library_id = managed_libraries.id
                WHERE managed_libraries.enabled = 1
                GROUP BY managed_libraries.mount_id
                """
            ).fetchall()
        return {
            row["mount_id"]: (row["libraries"], row["publications"], row["size"])
            for row in rows
        }

    def managed_libraries(
        self, *, include_disabled: bool = False, mount_id: str | None = None
    ) -> list[ManagedLibrary]:
        clauses = []
        parameters: list[object] = []
        if not include_disabled:
            # A disconnected mount hides its libraries from every reader-facing
            # query without touching a single stored row.
            clauses.append("managed_libraries.enabled = 1 AND data_mounts.enabled = 1")
        if mount_id is not None:
            clauses.append("managed_libraries.mount_id = ?")
            parameters.append(mount_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT managed_libraries.* FROM managed_libraries "
                "JOIN data_mounts ON data_mounts.id = managed_libraries.mount_id"
                f"{where} ORDER BY managed_libraries.name COLLATE NOCASE",
                parameters,
            ).fetchall()
        return [self._library(row) for row in rows]

    def managed_library(self, library_id: str) -> ManagedLibrary | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM managed_libraries WHERE id = ?", (library_id,)
            ).fetchone()
        return self._library(row) if row else None

    def add_library(
        self,
        relative_path: str,
        mount_id: str = DEFAULT_MOUNT_ID,
        name: str | None = None,
    ) -> ManagedLibrary:
        display_name = name or relative_path
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM managed_libraries
                WHERE mount_id = ? AND relative_path = ? COLLATE NOCASE
                """,
                (mount_id, relative_path),
            ).fetchone()
            try:
                library_id = self._upsert_library(
                    connection, row, relative_path, mount_id, display_name
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    f"Another library is already called “{display_name}”. "
                    "Give this one a different display name."
                ) from error
        library = self.managed_library(library_id)
        if library is None:  # pragma: no cover - guaranteed by the upsert above
            raise RuntimeError(f"Managed library disappeared during add: {library_id}")
        return library

    @staticmethod
    def _upsert_library(
        connection: sqlite3.Connection,
        row: sqlite3.Row | None,
        relative_path: str,
        mount_id: str,
        display_name: str,
    ) -> str:
        """Re-adding a removed library keeps its identity, grants and history."""
        if row:
            connection.execute(
                "UPDATE managed_libraries SET enabled = 1, name = ? WHERE id = ?",
                (display_name, row["id"]),
            )
            return str(row["id"])
        library_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO managed_libraries(
                id, name, relative_path, mount_id, enabled, created_at
            ) VALUES (?, ?, ?, ?, 1, ?)
            """,
            (library_id, display_name, relative_path, mount_id, _now_iso()),
        )
        return library_id

    def remove_library(self, library_id: str) -> ManagedLibrary | None:
        library = self.managed_library(library_id)
        if not library or not library.enabled:
            return None
        with self._connect() as connection:
            connection.execute(
                "UPDATE managed_libraries SET enabled = 0 WHERE id = ?", (library_id,)
            )
            connection.execute(
                "DELETE FROM access_grants WHERE library_id = ?", (library_id,)
            )
            connection.execute(
                "DELETE FROM publications WHERE library_id = ?", (library_id,)
            )
        return self.managed_library(library_id)

    def library_usage(self) -> list[LibraryUsage]:
        """Capacity for every managed library, aggregated in a single pass."""
        libraries = self.managed_libraries()
        if not libraries:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT publications.library_id AS library_id, catalog_series.id AS id,
                       publications.category AS category,
                       publications.series AS series,
                       COUNT(publications.id) AS item_count,
                       COALESCE(SUM(publications.size), 0) AS total_size
                FROM publications
                JOIN catalog_series ON catalog_series.id = publications.series_id
                GROUP BY publications.library_id, catalog_series.id
                ORDER BY publications.category COLLATE NOCASE,
                         publications.series COLLATE NOCASE
                """
            ).fetchall()
        grouped: dict[str, dict[str, list[SeriesUsage]]] = {}
        for row in rows:
            by_category = grouped.setdefault(row["library_id"], {})
            by_category.setdefault(row["category"], []).append(
                SeriesUsage(
                    id=row["id"],
                    name=row["series"],
                    publication_count=row["item_count"],
                    size=row["total_size"],
                )
            )
        return [
            _library_usage(library, grouped.get(library.id, {}))
            for library in libraries
        ]

    def all_access_grants(self) -> dict[str, list[AccessGrant]]:
        """Every grant, keyed by user, for the administration console."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, library_id, category, series_id
                FROM access_grants
                ORDER BY user_id, library_id, category, series_id
                """
            ).fetchall()
        grouped: dict[str, list[AccessGrant]] = {}
        for row in rows:
            grouped.setdefault(row["user_id"], []).append(_access_grant(row))
        return grouped

    def access_grants(self, user_id: str) -> list[AccessGrant]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, library_id, category, series_id
                FROM access_grants WHERE user_id = ?
                ORDER BY library_id, category, series_id
                """,
                (user_id,),
            ).fetchall()
        return [_access_grant(row) for row in rows]

    def replace_access_grants(self, user_id: str, grants: list[AccessGrant]) -> None:
        with self._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (user_id,)
            ).fetchone():
                raise ValueError("User not found")
            for grant in grants:
                self._validate_grant(connection, grant)
            connection.execute(
                "DELETE FROM access_grants WHERE user_id = ?", (user_id,)
            )
            connection.executemany(
                """
                INSERT INTO access_grants(user_id, library_id, category, series_id)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        user_id,
                        grant.library_id,
                        grant.category or "",
                        grant.series_id or "",
                    )
                    for grant in grants
                ],
            )

    @staticmethod
    def _validate_grant(connection: sqlite3.Connection, grant: AccessGrant) -> None:
        library = connection.execute(
            "SELECT 1 FROM managed_libraries WHERE id = ? AND enabled = 1",
            (grant.library_id,),
        ).fetchone()
        if not library:
            raise ValueError("Library not found")
        if grant.category not in {None, "comics", "manga"}:
            raise ValueError("Content type must be comics or manga")
        if grant.series_id:
            series = connection.execute(
                """
                SELECT 1 FROM catalog_series
                WHERE id = ? AND library_id = ? AND category = ? COLLATE NOCASE
                """,
                (grant.series_id, grant.library_id, grant.category),
            ).fetchone()
            if not series:
                raise ValueError("Series not found in the selected content type")

    def settings(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def replace_settings(self, values: dict[str, str]) -> None:
        """The stored set *is* the override set, so absent keys fall back."""
        with self._connect() as connection:
            connection.execute("DELETE FROM app_settings")
            connection.executemany(
                "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
                [(key, value, _now_iso()) for key, value in values.items()],
            )

    def series_metadata(self, series_id: str) -> SeriesMetadata | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM series_metadata WHERE series_id = ?", (series_id,)
            ).fetchone()
        return self._series_metadata(row) if row else None

    def all_series_metadata(self) -> dict[str, SeriesMetadata]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM series_metadata").fetchall()
        return {row["series_id"]: self._series_metadata(row) for row in rows}

    def series_metadata_summaries(self) -> dict[str, SeriesMetadataSummary]:
        """Titles and match state without the retained provider payloads."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT series_id, provider_id,
                       COALESCE(
                           json_extract(overrides_json, '$.title'),
                           json_extract(values_json, '$.title')
                       ) AS title
                FROM series_metadata
                """
            ).fetchall()
        return {
            row["series_id"]: SeriesMetadataSummary(
                series_id=row["series_id"],
                provider_id=row["provider_id"],
                title=row["title"],
            )
            for row in rows
        }

    def series_metadata_states(self) -> dict[str, SeriesMetadataState]:
        """Match, edit and failure state per series, without the payloads.

        Keyed off both tables: a series can have a failed lookup and no stored
        metadata at all. `UNION` rather than `FULL OUTER JOIN`, which needs
        SQLite 3.39 and this release does not pin an interpreter that new.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH recorded AS (
                    SELECT series_id FROM series_metadata
                    UNION
                    SELECT series_id FROM metadata_lookups
                    UNION
                    SELECT series_id FROM metadata_auto_match_items
                )
                SELECT recorded.series_id AS series_id,
                       COALESCE(
                           json_extract(sm.overrides_json, '$.title'),
                           json_extract(sm.values_json, '$.title')
                       ) AS title,
                       sm.provider_id AS provider_id,
                       sm.series_id IS NOT NULL AS matched,
                       COALESCE(sm.overrides_json, '{}') != '{}' AS edited,
                       (lookups.error IS NOT NULL OR auto_item.status = 'failed') AS failed,
                       sm.fetched_at AS fetched_at,
                       lookups.searched_at AS searched_at,
                       json_array_length(
                           COALESCE(lookups.candidates_json, '[]')
                       ) AS candidate_count,
                       lookups.error AS lookup_error,
                       auto_item.status AS auto_match_status,
                       auto_item.detail AS auto_match_detail
                FROM recorded
                LEFT JOIN series_metadata sm ON sm.series_id = recorded.series_id
                LEFT JOIN metadata_lookups lookups
                    ON lookups.series_id = recorded.series_id
                LEFT JOIN metadata_auto_match_items auto_item
                    ON auto_item.rowid = (
                        SELECT item.rowid
                        FROM metadata_auto_match_items item
                        JOIN metadata_auto_match_jobs job ON job.id = item.job_id
                        WHERE item.series_id = recorded.series_id
                        ORDER BY job.created_at DESC, item.position
                        LIMIT 1
                    )
                """
            ).fetchall()
        return {
            row["series_id"]: SeriesMetadataState(
                series_id=row["series_id"],
                title=row["title"],
                provider_id=row["provider_id"],
                matched=bool(row["matched"]),
                edited=bool(row["edited"]),
                failed=bool(row["failed"]),
                fetched_at=datetime.fromisoformat(row["fetched_at"])
                if row["fetched_at"]
                else None,
                searched_at=datetime.fromisoformat(row["searched_at"])
                if row["searched_at"]
                else None,
                candidate_count=row["candidate_count"],
                lookup_error=row["lookup_error"],
                auto_match_status=row["auto_match_status"],
                auto_match_detail=row["auto_match_detail"],
            )
            for row in rows
        }

    def save_series_metadata(
        self,
        series_id: str,
        provider_id: int,
        canonical_url: str,
        values: dict[str, object],
        raw: dict[str, object],
        provider_updated_at: str | None,
    ) -> SeriesMetadata:
        """Replace provider-owned values without touching administrator overrides."""
        with self._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM catalog_series WHERE id = ?", (series_id,)
            ).fetchone():
                raise ValueError("Series not found")
            connection.execute(
                """
                INSERT INTO series_metadata(
                    series_id, provider, provider_id, canonical_url, values_json,
                    overrides_json, raw_json, fetched_at, provider_updated_at
                ) VALUES (?, 'mangabaka', ?, ?, ?, '{}', ?, ?, ?)
                ON CONFLICT(series_id) DO UPDATE SET
                    provider=excluded.provider,
                    provider_id=excluded.provider_id,
                    canonical_url=excluded.canonical_url,
                    values_json=excluded.values_json,
                    raw_json=excluded.raw_json,
                    fetched_at=excluded.fetched_at,
                    provider_updated_at=excluded.provider_updated_at
                """,
                (
                    series_id,
                    provider_id,
                    canonical_url,
                    json.dumps(values, ensure_ascii=False),
                    json.dumps(raw, ensure_ascii=False),
                    _now_iso(),
                    provider_updated_at,
                ),
            )
            self._reindex_series(connection, series_id)
        metadata = self.series_metadata(series_id)
        if metadata is None:  # pragma: no cover - guarded by the transaction
            raise RuntimeError("Series metadata disappeared after save")
        return metadata

    def replace_metadata_overrides(
        self, series_id: str, overrides: dict[str, object]
    ) -> SeriesMetadata:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE series_metadata SET overrides_json = ? WHERE series_id = ?",
                (json.dumps(overrides, ensure_ascii=False), series_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("Series metadata not found")
            self._reindex_series(connection, series_id)
        metadata = self.series_metadata(series_id)
        if metadata is None:  # pragma: no cover - guarded by rowcount
            raise RuntimeError("Series metadata disappeared after update")
        return metadata

    def delete_series_metadata(self, series_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM series_metadata WHERE series_id = ?", (series_id,)
            )
            self._reindex_series(connection, series_id)

    def metadata_lookup(self, series_id: str) -> MetadataLookup:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM metadata_lookups WHERE series_id = ?", (series_id,)
            ).fetchone()
        if not row:
            return MetadataLookup(series_id, ())
        candidates = tuple(
            MetadataCandidate(
                provider_id=int(item["provider_id"]),
                title=item["title"],
                alternative_titles=tuple(item.get("alternative_titles", ())),
                authors=tuple(item.get("authors", ())),
                artists=tuple(item.get("artists", ())),
                description=item.get("description"),
                year=item.get("year"),
                media_type=item.get("media_type"),
                status=item.get("status"),
                rating=item.get("rating"),
                publishers=tuple(item.get("publishers", ())),
                tags=tuple(item.get("tags", ())),
                cover_url=item.get("cover_url"),
                source_url=item.get("source_url"),
            )
            for item in json.loads(row["candidates_json"])
        )
        return MetadataLookup(
            series_id=series_id,
            candidates=candidates,
            searched_at=datetime.fromisoformat(row["searched_at"]),
            error=row["error"],
        )

    def replace_metadata_lookup(
        self,
        series_id: str,
        candidates: list[MetadataCandidate],
        error: str | None = None,
    ) -> MetadataLookup:
        payload = [
            {
                "provider_id": item.provider_id,
                "title": item.title,
                "alternative_titles": item.alternative_titles,
                "authors": item.authors,
                "artists": item.artists,
                "description": item.description,
                "year": item.year,
                "media_type": item.media_type,
                "status": item.status,
                "rating": item.rating,
                "publishers": item.publishers,
                "tags": item.tags,
                "cover_url": item.cover_url,
                "source_url": item.source_url,
            }
            for item in candidates
        ]
        with self._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM catalog_series WHERE id = ?", (series_id,)
            ).fetchone():
                raise ValueError("Series not found")
            connection.execute(
                """
                INSERT INTO metadata_lookups(
                    series_id, candidates_json, searched_at, error
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(series_id) DO UPDATE SET
                    candidates_json=excluded.candidates_json,
                    searched_at=excluded.searched_at,
                    error=excluded.error
                """,
                (series_id, json.dumps(payload, ensure_ascii=False), _now_iso(), error),
            )
        return self.metadata_lookup(series_id)

    def reserve_metadata_request(
        self, limit: int, now: float, window_seconds: float = 60.0
    ) -> float:
        """Reserve one outbound request or return its required wait time."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM metadata_rate_events WHERE requested_at <= ?",
                (now - window_seconds,),
            )
            rows = connection.execute(
                "SELECT requested_at FROM metadata_rate_events ORDER BY requested_at"
            ).fetchall()
            if len(rows) >= limit:
                return max(
                    0.001,
                    float(rows[0]["requested_at"]) + window_seconds - now,
                )
            timestamp = now
            while connection.execute(
                "SELECT 1 FROM metadata_rate_events WHERE requested_at = ?",
                (timestamp,),
            ).fetchone():
                timestamp += 0.000001
            connection.execute(
                "INSERT INTO metadata_rate_events(requested_at) VALUES (?)",
                (timestamp,),
            )
        return 0.0

    def create_metadata_auto_match_job(
        self, library_id: str, series_ids: list[str]
    ) -> MetadataAutoMatchJob:
        job_id = str(uuid.uuid4())
        now = _now_iso()
        ordered = list(dict.fromkeys(series_ids))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM metadata_auto_match_jobs WHERE status = 'running'"
            ).fetchone():
                raise ValueError("A metadata job is already running")
            if not connection.execute(
                "SELECT 1 FROM managed_libraries WHERE id = ? AND enabled = 1",
                (library_id,),
            ).fetchone():
                raise ValueError("Managed library not found")
            connection.execute(
                """
                INSERT INTO metadata_auto_match_jobs(
                    id, library_id, status, total, created_at, updated_at
                ) VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (job_id, library_id, len(ordered), now, now),
            )
            connection.executemany(
                """
                INSERT INTO metadata_auto_match_items(job_id, series_id, position)
                VALUES (?, ?, ?)
                """,
                [
                    (job_id, series_id, position)
                    for position, series_id in enumerate(ordered)
                ],
            )
        job = self._metadata_auto_match_job(job_id)
        assert job is not None
        return job

    def active_metadata_auto_match_job(self) -> MetadataAutoMatchJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM metadata_auto_match_jobs
                WHERE status = 'running' ORDER BY created_at LIMIT 1
                """
            ).fetchone()
        return self._auto_match_job(row) if row else None

    def latest_metadata_auto_match_job(
        self, library_id: str
    ) -> MetadataAutoMatchJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM metadata_auto_match_jobs
                WHERE library_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (library_id,),
            ).fetchone()
        return self._auto_match_job(row) if row else None

    def _metadata_auto_match_job(self, job_id: str) -> MetadataAutoMatchJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM metadata_auto_match_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._auto_match_job(row) if row else None

    @staticmethod
    def _auto_match_job(row: sqlite3.Row) -> MetadataAutoMatchJob:
        return MetadataAutoMatchJob(
            id=row["id"],
            library_id=row["library_id"],
            status=row["status"],
            total=row["total"],
            completed=row["completed"],
            linked=row["linked"],
            review=row["review"],
            failed=row["failed"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def pending_metadata_auto_match_series(self, job_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT series_id FROM metadata_auto_match_items
                WHERE job_id = ? AND status = 'pending' ORDER BY position
                """,
                (job_id,),
            ).fetchall()
        return [row["series_id"] for row in rows]

    def finish_metadata_auto_match_item(
        self, job_id: str, series_id: str, status: str, detail: str | None = None
    ) -> None:
        if status not in {"linked", "review", "failed"}:
            raise ValueError("Invalid auto-match result")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE metadata_auto_match_items SET status = ?, detail = ?
                WHERE job_id = ? AND series_id = ? AND status = 'pending'
                """,
                (status, detail, job_id, series_id),
            ).rowcount
            if changed:
                connection.execute(
                    f"""
                    UPDATE metadata_auto_match_jobs
                    SET completed = completed + 1, {status} = {status} + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (_now_iso(), job_id),
                )

    def complete_metadata_auto_match_job(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE metadata_auto_match_jobs SET status = 'complete', updated_at = ?
                WHERE id = ?
                """,
                (_now_iso(), job_id),
            )

    def series_spread_detection(self, series_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT auto_spread_detection FROM series_reader_settings
                WHERE series_id = ?
                """,
                (series_id,),
            ).fetchone()
        return bool(row and row["auto_spread_detection"])

    def set_series_spread_detection(self, series_id: str, enabled: bool) -> bool:
        with self._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM catalog_series WHERE id = ?", (series_id,)
            ).fetchone():
                return False
            connection.execute(
                """
                INSERT INTO series_reader_settings(
                    series_id, auto_spread_detection, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(series_id) DO UPDATE SET
                    auto_spread_detection=excluded.auto_spread_detection,
                    updated_at=excluded.updated_at
                """,
                (series_id, int(enabled), _now_iso()),
            )
        return True

    def spread_detection_series_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT series_id FROM series_reader_settings
                WHERE auto_spread_detection = 1 ORDER BY series_id
                """
            ).fetchall()
        return [row["series_id"] for row in rows]

    def publication_spread_analysis(
        self, publication_id: str, revision: str
    ) -> SpreadAnalysis | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM publication_spread_analysis
                WHERE publication_id = ? AND revision = ?
                """,
                (publication_id, revision),
            ).fetchone()
        return self._spread_analysis(row) if row else None

    def save_publication_spread_analysis(
        self,
        publication_id: str,
        revision: str,
        anchor_page: int | None,
        source: str | None = None,
    ) -> SpreadAnalysis:
        status = "detected" if anchor_page is not None else "none"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO publication_spread_analysis(
                    publication_id, revision, status, anchor_page, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(publication_id) DO UPDATE SET
                    revision=excluded.revision, status=excluded.status,
                    anchor_page=excluded.anchor_page, source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (publication_id, revision, status, anchor_page, source, _now_iso()),
            )
        result = self.publication_spread_analysis(publication_id, revision)
        assert result is not None
        return result

    def publication_spread_override(self, publication_id: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT anchor_page FROM publication_spread_overrides
                WHERE publication_id = ?
                """,
                (publication_id,),
            ).fetchone()
        return row["anchor_page"] if row else None

    def publication_spread_overrides(self, series_id: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.publication_id AS publication_id, o.anchor_page AS anchor_page
                FROM publication_spread_overrides o
                JOIN publications p ON p.id = o.publication_id
                WHERE p.series_id = ?
                """,
                (series_id,),
            ).fetchall()
        return {row["publication_id"]: row["anchor_page"] for row in rows}

    def set_publication_spread_override(
        self, publication_id: str, anchor_page: int | None
    ) -> bool:
        if anchor_page is not None and anchor_page < 2:
            raise ValueError("Pairing starts at page 2 or later")
        with self._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM publications WHERE id = ?", (publication_id,)
            ).fetchone():
                return False
            if anchor_page is None:
                connection.execute(
                    "DELETE FROM publication_spread_overrides WHERE publication_id = ?",
                    (publication_id,),
                )
                return True
            connection.execute(
                """
                INSERT INTO publication_spread_overrides(
                    publication_id, anchor_page, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(publication_id) DO UPDATE SET
                    anchor_page=excluded.anchor_page, updated_at=excluded.updated_at
                """,
                (publication_id, anchor_page, _now_iso()),
            )
        return True

    @staticmethod
    def _spread_analysis(row: sqlite3.Row) -> SpreadAnalysis:
        return SpreadAnalysis(
            publication_id=row["publication_id"],
            revision=row["revision"],
            status=row["status"],
            anchor_page=row["anchor_page"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            source=row["source"],
        )

    @staticmethod
    def _series_metadata(row: sqlite3.Row) -> SeriesMetadata:
        return SeriesMetadata(
            series_id=row["series_id"],
            provider=row["provider"],
            provider_id=row["provider_id"],
            canonical_url=row["canonical_url"],
            values=json.loads(row["values_json"]),
            overrides=json.loads(row["overrides_json"]),
            raw=json.loads(row["raw_json"]),
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            provider_updated_at=row["provider_updated_at"],
        )

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            is_admin=bool(row["is_admin"]),
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _publication(row: sqlite3.Row) -> Publication:
        return Publication(
            id=row["id"],
            relative_path=row["relative_path"],
            library=row["library"],
            category=row["category"],
            series=row["series"],
            filename=row["filename"],
            title=row["title"],
            number=row["number"],
            description=row["description"],
            authors=tuple(json.loads(row["authors_json"])),
            modified_ns=row["modified_ns"],
            size=row["size"],
            revision=row["revision"],
            page_count=row["page_count"],
            cover_page=row["cover_page"],
            library_id=row["library_id"],
            series_id=row["series_id"],
        )

    @staticmethod
    def _library(row: sqlite3.Row) -> ManagedLibrary:
        return ManagedLibrary(
            id=row["id"],
            name=row["name"],
            relative_path=row["relative_path"],
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            mount_id=row["mount_id"],
        )

    @staticmethod
    def _data_mount(row: sqlite3.Row) -> DataMount:
        return DataMount(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            allow_ingest=bool(row["allow_ingest"]),
            scan_enabled=bool(row["scan_enabled"]),
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def user_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def user_by_username(self, username: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
        return self._user(row) if row else None

    def user_by_id(self, user_id: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._user(row) if row else None

    def users(self) -> list[User]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return [self._user(row) for row in rows]

    def create_user(self, username: str, password_hash: str, is_admin: bool) -> User:
        user_id = str(uuid.uuid4())
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, password_hash, is_admin, enabled, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (user_id, username, password_hash, int(is_admin), created_at),
            )
        user = self.user_by_id(user_id)
        assert user is not None
        return user

    def update_user(
        self,
        user_id: str,
        *,
        enabled: bool | None = None,
        password_hash: str | None = None,
    ) -> User | None:
        updates: list[str] = []
        values: list[object] = []
        if enabled is not None:
            updates.append("enabled = ?")
            values.append(int(enabled))
        if password_hash is not None:
            updates.append("password_hash = ?")
            values.append(password_hash)
        if not updates:
            return self.user_by_id(user_id)
        values.append(user_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id = ?", values
            )
            if password_hash is not None or enabled is False:
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return self.user_by_id(user_id)

    def enabled_admin_count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND enabled = 1"
                ).fetchone()[0]
            )

    def create_session(
        self, user_id: str, token_hash: str, csrf_token: str, expires_at: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (_now_iso(),)
            )
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, csrf_token, expires_at),
            )

    def session(self, token_hash: str, now: str) -> tuple[User, str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT users.*, sessions.csrf_token, sessions.expires_at
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.enabled = 1
                """,
                (token_hash, now),
            ).fetchone()
        if not row:
            return None
        return self._user(row), row["csrf_token"], row["expires_at"]

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
            )

    def set_session_flash(
        self, token_hash: str, message: str | None, error: str | None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET flash_message = ?, flash_error = ? "
                "WHERE token_hash = ?",
                (message, error, token_hash),
            )

    def take_session_flash(self, token_hash: str) -> tuple[str | None, str | None]:
        """Read the pending notice and clear it in one transaction."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT flash_message, flash_error FROM sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if not row or (row["flash_message"] is None and row["flash_error"] is None):
                return None, None
            connection.execute(
                "UPDATE sessions SET flash_message = NULL, flash_error = NULL "
                "WHERE token_hash = ?",
                (token_hash,),
            )
        return row["flash_message"], row["flash_error"]

    def publication_by_id(
        self, publication_id: str, scope: ReadScope | None = None
    ) -> Publication | None:
        predicate, parameters = _scope_predicate(scope)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM publications WHERE id = ? AND ({predicate})",
                (publication_id, *parameters),
            ).fetchone()
        return self._publication(row) if row else None

    def publication_by_path(
        self, relative_path: str, library_id: str | None = None
    ) -> Publication | None:
        clause = " AND library_id = ?" if library_id else ""
        parameters = (relative_path, library_id) if library_id else (relative_path,)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM publications WHERE relative_path = ?{clause}",
                parameters,
            ).fetchone()
        return self._publication(row) if row else None

    def page(
        self, publication_id: str, number: int, scope: ReadScope | None = None
    ) -> PublicationPage | None:
        publication = self.publication_by_id(publication_id, scope)
        if not publication:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pages WHERE publication_id = ? AND number = ?",
                (publication_id, number),
            ).fetchone()
        return PublicationPage(publication, self._page(row)) if row else None

    @staticmethod
    def _page(row: sqlite3.Row) -> Page:
        return Page(
            number=row["number"],
            member_name=row["member_name"],
            media_type=row["media_type"],
            compressed_size=row["compressed_size"],
            uncompressed_size=row["uncompressed_size"],
            crc=row["crc"],
            width=row["width"],
            height=row["height"],
            is_spread=bool(row["is_spread"]),
        )

    def pages(self, publication_id: str, start: int, end: int) -> list[Page]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pages
                WHERE publication_id = ? AND number BETWEEN ? AND ?
                ORDER BY number
                """,
                (publication_id, start, end),
            ).fetchall()
        return [self._page(row) for row in rows]

    def update_page_dimensions(
        self, publication_id: str, dimensions: list[tuple[int, int, int]]
    ) -> None:
        with self._connect() as connection:
            connection.executemany(
                "UPDATE pages SET width = ?, height = ? WHERE publication_id = ? AND number = ?",
                [
                    (width, height, publication_id, number)
                    for number, width, height in dimensions
                ],
            )

    def reading_progress(
        self, user_id: str, publication_id: str
    ) -> ReadingProgress | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM reading_progress
                WHERE user_id = ? AND publication_id = ?
                """,
                (user_id, publication_id),
            ).fetchone()
        return self._reading_progress(row) if row else None

    def reading_progress_for_publications(
        self, user_id: str, publication_ids: list[str]
    ) -> dict[str, ReadingProgress]:
        identifiers = list(dict.fromkeys(publication_ids))
        if not identifiers:
            return {}
        placeholders = ", ".join("?" for _ in identifiers)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM reading_progress
                WHERE user_id = ? AND publication_id IN ({placeholders})
                """,
                (user_id, *identifiers),
            ).fetchall()
        return {row["publication_id"]: self._reading_progress(row) for row in rows}

    def save_reading_progress(
        self,
        user_id: str,
        publication_id: str,
        page: int,
        mode: str | None,
        completed: bool,
    ) -> ReadingProgress:
        """Save a position; `mode` only records what a legacy client sent.

        Callers that no longer send a mode leave the stored one untouched, so
        an older app sharing the account keeps reading back what it last saved.
        """
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO reading_progress(
                    user_id, publication_id, page_number, mode, completed, updated_at
                ) VALUES (
                    :user_id, :publication_id, :page, COALESCE(:mode, 'single'),
                    :completed, :updated_at
                )
                ON CONFLICT(user_id, publication_id) DO UPDATE SET
                    page_number=excluded.page_number,
                    mode=COALESCE(:mode, reading_progress.mode),
                    completed=excluded.completed,
                    updated_at=excluded.updated_at
                RETURNING *
                """,
                {
                    "user_id": user_id,
                    "publication_id": publication_id,
                    "page": page,
                    "mode": mode,
                    "completed": int(completed),
                    "updated_at": _now_iso(),
                },
            ).fetchone()
        return self._reading_progress(row)

    def delete_reading_progress(self, user_id: str, publication_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM reading_progress
                WHERE user_id = ? AND publication_id = ?
                """,
                (user_id, publication_id),
            )

    @staticmethod
    def _reading_progress(row: sqlite3.Row) -> ReadingProgress:
        return ReadingProgress(
            user_id=row["user_id"],
            publication_id=row["publication_id"],
            page=row["page_number"],
            mode=row["mode"],
            completed=bool(row["completed"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def upsert_publication(self, scanned: ScannedPublication) -> None:
        with self._connect() as connection:
            self._write_publication(connection, scanned.publication)
            self._replace_pages(connection, scanned.publication.id, scanned.pages)
            series_id = connection.execute(
                "SELECT series_id FROM publications WHERE id = ?",
                (scanned.publication.id,),
            ).fetchone()
            if series_id and series_id["series_id"]:
                self._reindex_series(connection, series_id["series_id"])

    @staticmethod
    def _write_publication(connection: sqlite3.Connection, item: Publication) -> None:
        library_id, series_id = SQLiteRepository._ensure_catalog_hierarchy(
            connection, item
        )
        connection.execute(
            """
                INSERT INTO publications(
                    id, relative_path, library, category, series, filename, title, number,
                    description, authors_json, modified_ns, size, revision, page_count,
                    cover_page, library_id, series_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(library_id, relative_path) DO UPDATE SET
                    library=excluded.library, category=excluded.category, series=excluded.series,
                    filename=excluded.filename, title=excluded.title, number=excluded.number,
                    description=excluded.description, authors_json=excluded.authors_json,
                    modified_ns=excluded.modified_ns, size=excluded.size, revision=excluded.revision,
                    page_count=excluded.page_count, cover_page=excluded.cover_page,
                    library_id=excluded.library_id, series_id=excluded.series_id
                """,
            (
                item.id,
                item.relative_path,
                item.library,
                item.category,
                item.series,
                item.filename,
                item.title,
                item.number,
                item.description,
                json.dumps(item.authors),
                item.modified_ns,
                item.size,
                item.revision,
                item.page_count,
                item.cover_page,
                library_id,
                series_id,
            ),
        )

    @staticmethod
    def _ensure_catalog_hierarchy(
        connection: sqlite3.Connection, item: Publication
    ) -> tuple[str, str]:
        library_id = item.library_id
        if not library_id:
            row = connection.execute(
                "SELECT id FROM managed_libraries WHERE name = ? COLLATE NOCASE",
                (item.library,),
            ).fetchone()
            if row:
                library_id = row["id"]
            else:
                library_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO managed_libraries(
                        id, name, relative_path, mount_id, enabled, created_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (
                        library_id,
                        item.library,
                        item.library,
                        DEFAULT_MOUNT_ID,
                        _now_iso(),
                    ),
                )
        series_id = item.series_id
        if not series_id:
            row = connection.execute(
                """
                SELECT id FROM catalog_series
                WHERE library_id = ? AND category = ? COLLATE NOCASE
                    AND name = ? COLLATE NOCASE
                """,
                (library_id, item.category, item.series),
            ).fetchone()
            if row:
                series_id = row["id"]
            else:
                series_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO catalog_series(id, library_id, category, name)
                    VALUES (?, ?, ?, ?)
                    """,
                    (series_id, library_id, item.category, item.series),
                )
        return library_id, series_id

    @staticmethod
    def _replace_pages(
        connection: sqlite3.Connection,
        publication_id: str,
        pages: tuple[Page, ...],
    ) -> None:
        connection.execute(
            "DELETE FROM pages WHERE publication_id = ?", (publication_id,)
        )
        connection.executemany(
            """
            INSERT INTO pages(
                publication_id, number, member_name, media_type, compressed_size,
                uncompressed_size, crc, width, height, is_spread
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    publication_id,
                    page.number,
                    page.member_name,
                    page.media_type,
                    page.compressed_size,
                    page.uncompressed_size,
                    page.crc,
                    page.width,
                    page.height,
                    int(page.is_spread),
                )
                for page in pages
            ],
        )

    def remove_publications_except(
        self, relative_paths: set[str], library_id: str | None = None
    ) -> int:
        with self._connect() as connection:
            # Take the write lock before reading the catalog snapshot below. A
            # deferred transaction that reads first cannot upgrade to a writer
            # once a concurrent session or audit write commits in between
            # (SQLITE_BUSY_SNAPSHOT), and busy_timeout does not retry that.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TEMP TABLE seen_paths(path TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO seen_paths(path) VALUES (?)",
                ((path,) for path in relative_paths),
            )
            library_clause = " AND library_id = ?" if library_id else ""
            parameters = (library_id,) if library_id else ()
            touched = [
                row["series_id"]
                for row in connection.execute(
                    "SELECT DISTINCT series_id FROM publications "
                    "WHERE relative_path NOT IN (SELECT path FROM seen_paths)"
                    f"{library_clause} AND series_id IS NOT NULL",
                    parameters,
                ).fetchall()
            ]
            # `cursor.rowcount` counts only rows this statement deleted; using the
            # connection's `total_changes` would also count the cascaded page rows.
            cursor = connection.execute(
                "DELETE FROM publications "
                "WHERE relative_path NOT IN (SELECT path FROM seen_paths)"
                f"{library_clause}",
                parameters,
            )
            for series_id in touched:
                self._reindex_series(connection, series_id)
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Search index
    #
    # The index is derived from publications and stored metadata, and is
    # rebuilt for one series whenever either changes. It answers "which
    # series could match?" only; `search.py` decides how well they match.
    # ------------------------------------------------------------------

    def rebuild_search_index(self) -> None:
        with self._connect() as connection:
            self._rebuild_search_index(connection)

    @staticmethod
    def _rebuild_search_index(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM catalog_search")
        connection.execute("DELETE FROM catalog_search_facets")
        rows = connection.execute(_SEARCH_SOURCE.format(where="")).fetchall()
        SQLiteRepository._write_index_rows(connection, rows)

    @staticmethod
    def _reindex_series(connection: sqlite3.Connection, series_id: str) -> None:
        connection.execute(
            "DELETE FROM catalog_search WHERE series_id = ?", (series_id,)
        )
        connection.execute(
            "DELETE FROM catalog_search_facets WHERE series_id = ?", (series_id,)
        )
        rows = connection.execute(
            _SEARCH_SOURCE.format(where="WHERE catalog_series.id = ?"), (series_id,)
        ).fetchall()
        SQLiteRepository._write_index_rows(connection, rows)

    @staticmethod
    def _write_index_rows(
        connection: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> None:
        connection.executemany(
            """
            INSERT INTO catalog_search(
                series_id, local_title, canonical_title, alternate_titles,
                creators, publishers, tags, description, volume_titles, filenames
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_index_row(row) for row in rows],
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO
                catalog_search_facets(series_id, kind, value, normalized)
            VALUES (?, ?, ?, ?)
            """,
            [facet for row in rows for facet in _facet_rows(row)],
        )

    def search_vocabulary(self) -> list[str]:
        with self._connect() as connection:
            return [
                row["term"]
                for row in connection.execute(
                    "SELECT term FROM catalog_search_terms WHERE length(term) >= 4"
                ).fetchall()
            ]

    def search_candidates(
        self,
        terms: tuple[str, ...],
        *,
        filters: SearchFilters,
        scope: ReadScope,
        user_id: str,
        limit: int,
    ) -> tuple[list[SearchDocument], int]:
        source, parameters = self._search_source(terms, filters, scope, user_id)
        with self._connect() as connection:
            total = int(
                connection.execute(f"SELECT COUNT(*) {source}", parameters).fetchone()[
                    0
                ]
            )
            if not total:
                return [], 0
            rows = connection.execute(
                f"""
                SELECT catalog_series.id AS series_id
                {source}
                ORDER BY {_candidate_order(terms)}
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
            documents = self._documents(
                connection, [row["series_id"] for row in rows], scope, user_id
            )
        return documents, total

    def search_facets(
        self,
        terms: tuple[str, ...],
        *,
        filters: SearchFilters,
        scope: ReadScope,
        user_id: str,
    ) -> SearchFacets:
        """Count each family against every *other* active filter.

        Leaving a family's own selection out is what makes the counts usable:
        they always say how many results choosing that value would give, so a
        count is never an invitation to an empty page.
        """
        with self._connect() as connection:

            def count(family: str, sql: str, label_sql: str | None = None) -> tuple:
                source, parameters = self._search_source(
                    terms, filters, scope, user_id, without=family
                )
                return tuple(
                    SearchFacet(row["value"], row["label"], row["total"])
                    for row in connection.execute(
                        f"SELECT {sql} AS value, {label_sql or sql} AS label, "
                        f"COUNT(DISTINCT catalog_series.id) AS total "
                        f"{source} "
                        f"GROUP BY value HAVING value IS NOT NULL AND value <> '' "
                        f"ORDER BY total DESC, label COLLATE NOCASE",
                        parameters,
                    ).fetchall()
                )

            return SearchFacets(
                libraries=count(
                    "library",
                    "catalog_series.library_id",
                    "(SELECT name FROM managed_libraries "
                    " WHERE id = catalog_series.library_id)",
                ),
                categories=count("category", "catalog_series.category"),
                collections=count(
                    "collection",
                    "(CASE WHEN catalog_series.is_private THEN 'private' "
                    " ELSE 'public' END)",
                ),
                **{
                    family: self._metadata_facets(
                        connection, terms, filters, scope, user_id, kind
                    )
                    for kind, family in _METADATA_FACETS
                },
            )

    def _metadata_facets(
        self,
        connection: sqlite3.Connection,
        terms: tuple[str, ...],
        filters: SearchFilters,
        scope: ReadScope,
        user_id: str,
        kind: str,
    ) -> tuple[SearchFacet, ...]:
        source, parameters = self._search_source(
            terms,
            filters,
            scope,
            user_id,
            without=kind,
            join="JOIN catalog_search_facets AS facet"
            " ON facet.series_id = catalog_series.id AND facet.kind = ?",
            join_parameters=(kind,),
        )
        rows = connection.execute(
            f"""
            SELECT facet.normalized AS value, MIN(facet.value) AS label,
                   COUNT(DISTINCT catalog_series.id) AS total
            {source}
            GROUP BY facet.normalized
            ORDER BY total DESC, label COLLATE NOCASE
            LIMIT {_FACET_LIMIT}
            """,
            parameters,
        ).fetchall()
        return tuple(
            SearchFacet(row["value"], row["label"], row["total"]) for row in rows
        )

    def search_suggestions(
        self, terms: tuple[str, ...], *, scope: ReadScope, limit: int = 8
    ) -> list[SearchSuggestion]:
        source, parameters = self._search_source(
            terms, SearchFilters(), scope, user_id="", without="reading"
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT catalog_series.id AS id, catalog_series.category AS category,
                       catalog_series.is_private AS is_private,
                       (SELECT name FROM managed_libraries
                        WHERE id = catalog_series.library_id) AS library,
                       (SELECT {_METADATA_TITLE} FROM series_metadata
                        WHERE series_metadata.series_id = catalog_series.id) AS matched,
                       catalog_series.name AS local_title
                {source}
                ORDER BY {_candidate_order(terms)}
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
        return [
            SearchSuggestion(
                kind="series",
                title=row["matched"] or row["local_title"],
                subtitle=f"{row['library']} · {row['category']}"
                + (" · Private" if row["is_private"] else ""),
                url=f"/series/{row['id']}",
            )
            for row in rows
        ]

    def _search_source(
        self,
        terms: tuple[str, ...],
        filters: SearchFilters,
        scope: ReadScope,
        user_id: str,
        *,
        without: str | None = None,
        join: str = "",
        join_parameters: tuple[object, ...] = (),
    ) -> tuple[str, list[object]]:
        """The `FROM … WHERE …` every search query shares.

        A query with terms is *driven* by the full-text index: it runs once and
        hands back the matching series, instead of being asked again for each
        row of the catalog. `without` drops a single filter family so a facet
        can count what choosing one of its values would actually return.
        """
        clauses = [_SERIES_REACHABLE, _SERIES_HAS_VOLUMES]
        # Bound values are positional, and the joins are written before the
        # WHERE, so theirs have to come first.
        parameters: list[object] = list(join_parameters)
        if terms:
            source = (
                "FROM catalog_search JOIN catalog_series"
                " ON catalog_series.id = catalog_search.series_id"
            )
            clauses.insert(0, "catalog_search MATCH ?")
            parameters.append(_match_expression(terms))
        else:
            source = "FROM catalog_series"
        source = f"{source} {join}" if join else source
        for family, column, values in (
            ("library", "catalog_series.library_id", filters.library_ids),
            ("category", "catalog_series.category", filters.categories),
        ):
            if family == without or not values:
                continue
            clauses.append(f"{column} IN ({_placeholders(values)})")
            parameters.extend(values)
        if without != "collection" and filters.collections:
            wanted = {1 if item == "private" else 0 for item in filters.collections}
            clauses.append(f"catalog_series.is_private IN ({_placeholders(wanted)})")
            parameters.extend(sorted(wanted))
        for kind, values in (
            ("creator", filters.creators),
            ("publisher", filters.publishers),
            ("tag", filters.tags),
            ("status", filters.statuses),
            ("year", filters.years),
        ):
            if kind == without or not values:
                continue
            normalized = tuple(item.casefold() for item in values)
            clauses.append(
                "EXISTS (SELECT 1 FROM catalog_search_facets AS chosen"
                " WHERE chosen.series_id = catalog_series.id AND chosen.kind = ?"
                f"   AND chosen.normalized IN ({_placeholders(normalized)}))"
            )
            parameters.extend((kind, *normalized))
        if without != "reading" and filters.reading_state:
            reading, reading_parameters = _reading_predicate(
                filters.reading_state, user_id
            )
            clauses.append(reading)
            parameters.extend(reading_parameters)
        scope_clause, scope_parameters = _scope_predicate(scope)
        clauses.append(
            "EXISTS (SELECT 1 FROM publications"
            f" WHERE publications.series_id = catalog_series.id AND ({scope_clause}))"
        )
        parameters.extend(scope_parameters)
        where = " AND ".join(f"({item})" for item in clauses)
        return f"{source} WHERE {where}", parameters

    def _documents(
        self,
        connection: sqlite3.Connection,
        series_ids: list[str],
        scope: ReadScope,
        user_id: str,
    ) -> list[SearchDocument]:
        """Load the candidate series and everything ranking needs, in two reads."""
        if not series_ids:
            return []
        series = {
            item.id: item
            for item in self.catalog_series(
                series_ids=tuple(series_ids),
                scope=scope,
                visibility=CatalogVisibility.ALL,
            )
        }
        placeholders = _placeholders(tuple(series_ids))
        rows = connection.execute(
            f"""
            SELECT catalog_series.id AS series_id,
                   {_METADATA_TITLE} AS metadata_title,
                   series_metadata.values_json AS values_json,
                   series_metadata.overrides_json AS overrides_json,
                   publications.id AS publication_id,
                   publications.title AS volume_title,
                   publications.filename AS filename,
                   publications.modified_ns AS modified_ns,
                   reading_progress.completed AS completed
            FROM catalog_series
            LEFT JOIN series_metadata
              ON series_metadata.series_id = catalog_series.id
            LEFT JOIN publications
              ON publications.series_id = catalog_series.id
            LEFT JOIN reading_progress
              ON reading_progress.publication_id = publications.id
             AND reading_progress.user_id = ?
            WHERE catalog_series.id IN ({placeholders})
            ORDER BY publications.number COLLATE NOCASE,
                     publications.title COLLATE NOCASE
            """,
            (user_id, *series_ids),
        ).fetchall()
        return _assemble_documents(rows, series, series_ids)

    def catalog_series(
        self,
        *,
        series_id: str | None = None,
        series_ids: tuple[str, ...] | None = None,
        library_id: str | None = None,
        category: str | None = None,
        query: str | None = None,
        scope: ReadScope | None = None,
        visibility: CatalogVisibility = CatalogVisibility.PUBLIC,
    ) -> list[CatalogSeries]:
        # A disconnected mount hides its whole catalog from readers without
        # any of it being deleted.
        clauses = ["managed_libraries.enabled = 1", "data_mounts.enabled = 1"]
        parameters: list[object] = []
        visibility_clause = _visibility_predicate(visibility, "catalog_series")
        if visibility_clause:
            clauses.append(visibility_clause)
        if series_id:
            clauses.append("catalog_series.id = ?")
            parameters.append(series_id)
        if series_ids is not None:
            clauses.append(f"catalog_series.id IN ({_placeholders(series_ids)})")
            parameters.extend(series_ids)
        if library_id:
            clauses.append("catalog_series.library_id = ?")
            parameters.append(library_id)
        if category:
            clauses.append("catalog_series.category = ? COLLATE NOCASE")
            parameters.append(category)
        if query:
            escaped = (
                query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            # The EXISTS keeps volume titles searchable now that browsing is
            # series-first; matching them in the outer join instead would drop
            # every non-matching volume and understate the series' own counts.
            clauses.append(
                "(catalog_series.name LIKE ? ESCAPE '\\' "
                "OR json_extract(series_metadata.values_json, '$.title') LIKE ? ESCAPE '\\' "
                "OR json_extract(series_metadata.overrides_json, '$.title') LIKE ? ESCAPE '\\' "
                "OR series_metadata.values_json LIKE ? ESCAPE '\\' "
                "OR series_metadata.overrides_json LIKE ? ESCAPE '\\' "
                "OR EXISTS (SELECT 1 FROM publications AS matched "
                "WHERE matched.series_id = catalog_series.id "
                "AND (matched.title LIKE ? ESCAPE '\\' "
                "OR matched.filename LIKE ? ESCAPE '\\')))"
            )
            parameters.extend([pattern] * 7)
        scope_clause, scope_parameters = _scope_predicate(scope)
        clauses.append(scope_clause)
        parameters.extend(scope_parameters)
        where = " AND ".join(f"({clause})" for clause in clauses)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                WITH visible AS (
                    SELECT catalog_series.id, catalog_series.library_id,
                           managed_libraries.name AS library,
                           catalog_series.category, catalog_series.name,
                           catalog_series.is_private,
                           publications.id AS publication_id,
                           publications.revision AS publication_revision,
                           COUNT(*) OVER (
                               PARTITION BY catalog_series.id
                           ) AS publication_count,
                           ROW_NUMBER() OVER (
                               PARTITION BY catalog_series.id
                               ORDER BY publications.number COLLATE NOCASE,
                                        publications.title COLLATE NOCASE,
                                        publications.id
                           ) AS cover_order
                    FROM catalog_series
                    JOIN managed_libraries
                      ON managed_libraries.id = catalog_series.library_id
                    JOIN data_mounts
                      ON data_mounts.id = managed_libraries.mount_id
                    JOIN publications
                      ON publications.series_id = catalog_series.id
                    LEFT JOIN series_metadata
                      ON series_metadata.series_id = catalog_series.id
                    WHERE {where}
                )
                SELECT * FROM visible WHERE cover_order = 1
                ORDER BY library COLLATE NOCASE, category COLLATE NOCASE,
                         name COLLATE NOCASE
                """,
                parameters,
            ).fetchall()
        return [self._catalog_series(row) for row in rows]

    def catalog_series_by_id(
        self, series_id: str, scope: ReadScope | None = None
    ) -> CatalogSeries | None:
        items = self.catalog_series(
            series_id=series_id, scope=scope, visibility=CatalogVisibility.ALL
        )
        return items[0] if items else None

    def set_series_private(
        self, series_id: str, is_private: bool
    ) -> CatalogSeries | None:
        with self._connect() as connection:
            current = connection.execute(
                "SELECT is_private FROM catalog_series WHERE id = ?", (series_id,)
            ).fetchone()
            if not current:
                return None
            if bool(current["is_private"]) != is_private:
                connection.execute(
                    "UPDATE catalog_series SET is_private = ? WHERE id = ?",
                    (int(is_private), series_id),
                )
                connection.execute(
                    """
                    INSERT INTO application_metadata(key, value)
                    VALUES ('catalog_visibility_modified_at', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (_now_iso(),),
                )
        return self.catalog_series_by_id(series_id)

    def catalog_visibility_modified_at(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM application_metadata "
                "WHERE key = 'catalog_visibility_modified_at'"
            ).fetchone()
        return row["value"] if row else None

    def publications_in_series(
        self, series_id: str, scope: ReadScope | None = None
    ) -> list[Publication]:
        scope_clause, parameters = _scope_predicate(scope)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM publications
                WHERE series_id = ? AND ({scope_clause})
                """,
                (series_id, *parameters),
            ).fetchall()
        return [self._publication(row) for row in rows]

    def create_librarian_token(
        self,
        name: str,
        token_hash: str,
        scopes: tuple[str, ...],
        library_ids: tuple[str, ...],
    ) -> LibrarianToken:
        token = LibrarianToken(
            id=str(uuid.uuid4()),
            name=name,
            scopes=scopes,
            library_ids=library_ids,
            created_at=datetime.now(UTC),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO librarian_tokens(
                    id, name, token_hash, scopes, library_ids, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    token.id,
                    token.name,
                    token_hash,
                    _join(scopes),
                    _join(library_ids),
                    token.created_at.isoformat(),
                ),
            )
        return token

    def librarian_token_by_hash(self, token_hash: str) -> LibrarianToken | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM librarian_tokens
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
        return self._librarian_token(row) if row else None

    def librarian_token(self, token_id: str) -> LibrarianToken | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM librarian_tokens WHERE id = ?", (token_id,)
            ).fetchone()
        return self._librarian_token(row) if row else None

    def librarian_tokens(
        self, *, include_revoked: bool = False
    ) -> list[LibrarianToken]:
        where = "" if include_revoked else " WHERE revoked_at IS NULL"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM librarian_tokens{where} ORDER BY created_at DESC"
            ).fetchall()
        return [self._librarian_token(row) for row in rows]

    def update_librarian_token(
        self,
        token_id: str,
        *,
        name: str | None = None,
        scopes: tuple[str, ...] | None = None,
        library_ids: tuple[str, ...] | None = None,
    ) -> LibrarianToken | None:
        assignments: list[str] = []
        values: list[object] = []
        if name is not None:
            assignments.append("name = ?")
            values.append(name)
        if scopes is not None:
            assignments.append("scopes = ?")
            values.append(_join(scopes))
        if library_ids is not None:
            assignments.append("library_ids = ?")
            values.append(_join(library_ids))
        if not assignments:
            return self.librarian_token(token_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE librarian_tokens SET {', '.join(assignments)}"
                " WHERE id = ? AND revoked_at IS NULL",
                (*values, token_id),
            )
            if not cursor.rowcount:
                return None
        return self.librarian_token(token_id)

    def revoke_librarian_token(self, token_id: str) -> LibrarianToken | None:
        # Clearing `token_hash` is the part that matters: the row stays so the
        # activity feed keeps a resolvable owner, but the secret can never
        # authenticate again even if a lookup forgets the `revoked_at` filter.
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE librarian_tokens
                SET revoked_at = ?, token_hash = NULL
                WHERE id = ? AND revoked_at IS NULL
                """,
                (_now_iso(), token_id),
            )
            if not cursor.rowcount:
                return None
        return self.librarian_token(token_id)

    def touch_librarian_token(self, token_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE librarian_tokens SET last_used_at = ? WHERE id = ?",
                (_now_iso(), token_id),
            )

    def record_security_event(self, event: SecurityEvent) -> SecurityEvent:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO security_events(id, kind, summary, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.kind,
                    event.summary,
                    json.dumps(event.detail) if event.detail is not None else None,
                    event.created_at.isoformat(),
                ),
            )
        return event

    def security_events(
        self, *, kind: str | None = None, limit: int = 100
    ) -> list[SecurityEvent]:
        where, values = ("WHERE kind = ?", [kind]) if kind else ("", [])
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM security_events {where}"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*values, max(1, min(limit, 1000))),
            ).fetchall()
        return [
            SecurityEvent(
                id=row["id"],
                kind=row["kind"],
                summary=row["summary"],
                created_at=_parse_time(row["created_at"]),
                detail=json.loads(row["detail"]) if row["detail"] else None,
            )
            for row in rows
        ]

    def record_librarian_event(self, event: LibrarianEvent) -> LibrarianEvent:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO librarian_events(
                    id, kind, token_id, token_name, actor, correlation_id,
                    scopes_at_time, action, severity, outcome, subject_type,
                    subject_id, subject_label, summary, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.kind,
                    event.token_id,
                    event.token_name,
                    event.actor,
                    event.correlation_id,
                    _join(event.scopes_at_time),
                    event.action,
                    event.severity,
                    event.outcome,
                    event.subject_type,
                    event.subject_id,
                    event.subject_label,
                    event.summary,
                    json.dumps(event.detail) if event.detail is not None else None,
                    event.created_at.isoformat(),
                ),
            )
        return event

    def purge_librarian_events(
        self, before: str, *, keep_severities: tuple[str, ...] = ()
    ) -> int:
        """Drop activity older than `before`, except the severities named.

        The exemption is the point: an audit trail that a single button can
        erase is not one. Callers keep `security` so who-was-granted-what
        survives however aggressively the read noise is trimmed.
        """
        clauses = ["created_at < ?"]
        values: list[object] = [before]
        if keep_severities:
            placeholders = ",".join("?" * len(keep_severities))
            clauses.append(f"severity NOT IN ({placeholders})")
            values.extend(keep_severities)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM librarian_events WHERE {' AND '.join(clauses)}",
                values,
            )
            return cursor.rowcount

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
    ) -> list[LibrarianEvent]:
        clauses: list[str] = []
        values: list[object] = []
        for column, value in (
            ("token_id", token_id),
            ("action", action),
            ("outcome", outcome),
            ("correlation_id", correlation_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        if severity is not None:
            # Severity is a floor, not an equality match: asking for `notice`
            # means "notice and anything louder", which is how the admin view
            # hides read noise without hiding writes.
            ranked = SEVERITY_ORDER.get(severity)
            if ranked is None:
                raise ValueError(f"Unknown severity: {severity}")
            allowed = [name for name, rank in SEVERITY_ORDER.items() if rank >= ranked]
            clauses.append(f"severity IN ({','.join('?' * len(allowed))})")
            values.extend(allowed)
        if since is not None:
            clauses.append("created_at >= ?")
            values.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            values.append(until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM librarian_events{where}"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*values, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._librarian_event(row) for row in rows]

    @staticmethod
    def _librarian_token(row: sqlite3.Row) -> LibrarianToken:
        return LibrarianToken(
            id=row["id"],
            name=row["name"],
            scopes=_split(row["scopes"]),
            library_ids=_split(row["library_ids"]),
            created_at=_parse_time(row["created_at"]),
            last_used_at=_parse_optional_time(row["last_used_at"]),
            revoked_at=_parse_optional_time(row["revoked_at"]),
        )

    @staticmethod
    def _librarian_event(row: sqlite3.Row) -> LibrarianEvent:
        return LibrarianEvent(
            id=row["id"],
            kind=row["kind"],
            action=row["action"],
            severity=row["severity"],
            outcome=row["outcome"],
            summary=row["summary"],
            created_at=_parse_time(row["created_at"]),
            token_id=row["token_id"],
            token_name=row["token_name"],
            actor=row["actor"],
            correlation_id=row["correlation_id"],
            scopes_at_time=_split(row["scopes_at_time"]),
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            subject_label=row["subject_label"],
            detail=json.loads(row["detail"]) if row["detail"] else None,
        )

    @staticmethod
    def _catalog_series(row: sqlite3.Row) -> CatalogSeries:
        return CatalogSeries(
            id=row["id"],
            library_id=row["library_id"],
            library=row["library"],
            category=row["category"],
            name=row["name"],
            is_private=bool(row["is_private"]),
            publication_count=row["publication_count"],
            first_publication_id=row["publication_id"],
            first_publication_revision=row["publication_revision"],
        )

    def libraries(
        self,
        scope: ReadScope | None = None,
        visibility: CatalogVisibility = CatalogVisibility.PUBLIC,
    ) -> list[tuple[str, int]]:
        return self._grouped("library", (), scope, visibility)

    def categories(
        self,
        library: str,
        scope: ReadScope | None = None,
        visibility: CatalogVisibility = CatalogVisibility.PUBLIC,
    ) -> list[tuple[str, int]]:
        return self._grouped("category", ("library = ?", library), scope, visibility)

    def series(
        self,
        library: str,
        category: str,
        scope: ReadScope | None = None,
        visibility: CatalogVisibility = CatalogVisibility.PUBLIC,
    ) -> list[tuple[str, int]]:
        return self._grouped(
            "series",
            ("library = ? AND category = ?", library, category),
            scope,
            visibility,
        )

    def _grouped(
        self,
        column: str,
        where: tuple[object, ...],
        scope: ReadScope | None,
        visibility: CatalogVisibility,
    ) -> list[tuple[str, int]]:
        allowed = {"library", "category", "series"}
        if column not in allowed:
            raise ValueError("unsupported grouping")
        clauses = [str(where[0])] if where else []
        parameters = list(where[1:]) if where else []
        scope_clause, scope_parameters = _scope_predicate(scope)
        clauses.append(scope_clause)
        parameters.extend(scope_parameters)
        visibility_clause = _visibility_predicate(visibility)
        if visibility_clause:
            clauses.append(visibility_clause)
        clause = f" WHERE {' AND '.join(f'({item})' for item in clauses)}"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {column} AS name, COUNT(*) AS count
                FROM publications{clause}
                GROUP BY {column} COLLATE NOCASE
                ORDER BY {column} COLLATE NOCASE
                """,
                parameters,
            ).fetchall()
        return [(row["name"], row["count"]) for row in rows]

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
        visibility: CatalogVisibility = CatalogVisibility.PUBLIC,
    ) -> tuple[list[Publication], int]:
        where, parameters = _publication_filter(
            library, category, series, query, scope, visibility
        )
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM publications{where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM publications{where}
                ORDER BY library COLLATE NOCASE, category COLLATE NOCASE,
                         series COLLATE NOCASE, title COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()
        return [self._publication(row) for row in rows], total


def _access_grant(row: sqlite3.Row) -> AccessGrant:
    return AccessGrant(
        user_id=row["user_id"],
        library_id=row["library_id"],
        category=row["category"] or None,
        series_id=row["series_id"] or None,
    )


def _library_usage(
    library: ManagedLibrary, series_by_category: dict[str, list[SeriesUsage]]
) -> LibraryUsage:
    categories = tuple(
        CategoryUsage(
            name=name,
            publication_count=sum(item.publication_count for item in items),
            size=sum(item.size for item in items),
            series=tuple(items),
        )
        for name, items in series_by_category.items()
    )
    return LibraryUsage(
        library=library,
        publication_count=sum(item.publication_count for item in categories),
        size=sum(item.size for item in categories),
        categories=categories,
    )


def _publication_filter(
    library: str | None,
    category: str | None,
    series: str | None,
    query: str | None,
    scope: ReadScope | None = None,
    visibility: CatalogVisibility = CatalogVisibility.PUBLIC,
) -> tuple[str, list[object]]:
    """Build the WHERE fragment and bound parameters for a publication search."""
    # Removing a library deletes its rows, but disconnecting a mount keeps
    # them, so reachability has to be asked rather than assumed.
    clauses: list[str] = [_REACHABLE]
    parameters: list[object] = []
    for column, value in (
        ("library", library),
        ("category", category),
        ("series", series),
    ):
        if value is not None:
            clauses.append(f"{column} = ? COLLATE NOCASE")
            parameters.append(value)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(
            "(title LIKE ? ESCAPE '\\' OR series LIKE ? ESCAPE '\\' "
            "OR filename LIKE ? ESCAPE '\\')"
        )
        parameters.extend([f"%{escaped}%"] * 3)
    scope_clause, scope_parameters = _scope_predicate(scope)
    clauses.append(scope_clause)
    parameters.extend(scope_parameters)
    visibility_clause = _visibility_predicate(visibility)
    if visibility_clause:
        clauses.append(visibility_clause)
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), parameters


_FACET_LIMIT = 40
# Facet kind as stored, and the field of `SearchFacets` it fills.
_METADATA_FACETS = (
    ("creator", "creators"),
    ("publisher", "publishers"),
    ("tag", "tags"),
    ("status", "statuses"),
    ("year", "years"),
)
# Fields are concatenated with the unit separator, which cannot appear in a
# filename or a title, so splitting them apart again is unambiguous.
_UNIT = "\x1f"

# The administrator's edits win over the provider's values, field by field.
_METADATA_TITLE = (
    "COALESCE(json_extract(series_metadata.overrides_json, '$.title'),"
    " json_extract(series_metadata.values_json, '$.title'))"
)

_SEARCH_SOURCE = """
    SELECT catalog_series.id AS series_id,
           catalog_series.name AS local_title,
           series_metadata.values_json AS values_json,
           series_metadata.overrides_json AS overrides_json,
           GROUP_CONCAT(publications.title, char(31)) AS volume_titles,
           GROUP_CONCAT(publications.filename, char(31)) AS filenames
    FROM catalog_series
    JOIN publications ON publications.series_id = catalog_series.id
    LEFT JOIN series_metadata ON series_metadata.series_id = catalog_series.id
    {where}
    GROUP BY catalog_series.id
"""


# A publication a reader can actually reach: its library is managed and its
# mount is connected. Written against `publications.library_id` so it can be
# dropped into any query over that table.
_REACHABLE = """
    EXISTS (
        SELECT 1 FROM managed_libraries
        JOIN data_mounts ON data_mounts.id = managed_libraries.mount_id
        WHERE managed_libraries.id = publications.library_id
          AND managed_libraries.enabled = 1 AND data_mounts.enabled = 1
    )
"""


# The same reachability rule as `_REACHABLE`, asked of a series instead.
_SERIES_REACHABLE = """
    EXISTS (
        SELECT 1 FROM managed_libraries
        JOIN data_mounts ON data_mounts.id = managed_libraries.mount_id
        WHERE managed_libraries.id = catalog_series.library_id
          AND managed_libraries.enabled = 1 AND data_mounts.enabled = 1
    )
"""
# An emptied series keeps its row but has nothing to show.
_SERIES_HAS_VOLUMES = """
    EXISTS (SELECT 1 FROM publications WHERE publications.series_id = catalog_series.id)
"""


def _placeholders(values: tuple[object, ...] | set[object]) -> str:
    return ", ".join("?" for _ in values)


def _candidate_order(terms: tuple[str, ...]) -> str:
    """Best-first, so a truncated candidate window still holds the best matches.

    The weights follow the fields of `catalog_search`: a local title beats a
    canonical one, both beat a creator, and a summary counts for least.
    """
    if not terms:
        return "catalog_series.name COLLATE NOCASE"
    return (
        "bm25(catalog_search, 0, 12, 11, 9, 7, 5, 4, 2, 10, 6),"
        " catalog_series.name COLLATE NOCASE"
    )


def _match_expression(terms: tuple[str, ...]) -> str:
    """An FTS5 query that requires every term, matching on prefixes.

    Terms arrive from `search.tokenize`, which keeps only word characters, so
    nothing here can carry FTS operator syntax. The quoting is belt and braces.
    """
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)


def _metadata_values(row: sqlite3.Row) -> dict[str, Any]:
    values = json.loads(row["values_json"] or "{}")
    overrides = json.loads(row["overrides_json"] or "{}")
    return {**values, **overrides}


def _strings(values: object) -> tuple[str, ...]:
    if not isinstance(values, list | tuple):
        return ()
    return tuple(str(item).strip() for item in values if str(item).strip())


def _text(value: object) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(item for item in (value or "").split(_UNIT) if item)


def _index_row(row: sqlite3.Row) -> tuple[str, ...]:
    metadata = _metadata_values(row)
    creators = _strings(metadata.get("authors")) + _strings(metadata.get("artists"))
    return (
        row["series_id"],
        row["local_title"],
        _text(metadata.get("title")),
        " ".join(_strings(metadata.get("alternative_titles"))),
        " ".join(creators),
        " ".join(_strings(metadata.get("publishers"))),
        " ".join(_strings(metadata.get("tags"))),
        _text(metadata.get("description")),
        " ".join(_split(row["volume_titles"])),
        " ".join(_split(row["filenames"])),
    )


def _facet_rows(row: sqlite3.Row) -> list[tuple[str, str, str, str]]:
    metadata = _metadata_values(row)
    year = _text(metadata.get("published_start"))[:4]
    families = (
        (
            "creator",
            _strings(metadata.get("authors")) + _strings(metadata.get("artists")),
        ),
        ("publisher", _strings(metadata.get("publishers"))),
        ("tag", _strings(metadata.get("tags"))),
        ("status", (_text(metadata.get("status")),)),
        ("year", (year,) if year.isdigit() else ()),
    )
    return [
        (row["series_id"], kind, value, value.casefold())
        for kind, values in families
        for value in values
        if value
    ]


def _reading_predicate(state: str, user_id: str) -> tuple[str, list[object]]:
    """Whether a reader has finished, started, or not opened a series."""
    started = (
        "EXISTS (SELECT 1 FROM publications AS read_item"
        " JOIN reading_progress ON reading_progress.publication_id = read_item.id"
        " WHERE read_item.series_id = catalog_series.id"
        "   AND reading_progress.user_id = ?)"
    )
    unfinished = (
        "EXISTS (SELECT 1 FROM publications AS open_item"
        " LEFT JOIN reading_progress"
        "   ON reading_progress.publication_id = open_item.id"
        "  AND reading_progress.user_id = ?"
        " WHERE open_item.series_id = catalog_series.id"
        "   AND COALESCE(reading_progress.completed, 0) = 0)"
    )
    if state == READ_UNREAD:
        return f"NOT {started}", [user_id]
    if state == READ_COMPLETED:
        return f"{started} AND NOT {unfinished}", [user_id, user_id]
    return f"{started} AND {unfinished}", [user_id, user_id]


def _assemble_documents(
    rows: list[sqlite3.Row],
    series: dict[str, CatalogSeries],
    order: list[str],
) -> list[SearchDocument]:
    """Fold the per-volume rows back into one document per series."""
    drafts: dict[str, _DocumentDraft] = {}
    for row in rows:
        series_id = row["series_id"]
        if series_id not in series:
            continue
        draft = drafts.get(series_id)
        if draft is None:
            draft = _DocumentDraft.begin(row)
            drafts[series_id] = draft
        draft.add_volume(row)
    return [drafts[key].finish(series[key]) for key in order if key in drafts]


@dataclass
class _DocumentDraft:
    metadata: dict[str, Any]
    display_title: str | None
    volumes: list[SearchVolume]
    newest_modified_ns: int
    progress: list[bool]
    publications: int

    @classmethod
    def begin(cls, row: sqlite3.Row) -> _DocumentDraft:
        return cls(
            metadata=_metadata_values(row),
            display_title=row["metadata_title"],
            volumes=[],
            newest_modified_ns=0,
            progress=[],
            publications=0,
        )

    def add_volume(self, row: sqlite3.Row) -> None:
        if row["publication_id"] is None:
            return
        self.publications += 1
        self.volumes.append(
            SearchVolume(
                id=row["publication_id"],
                title=row["volume_title"],
                filename=row["filename"],
            )
        )
        self.newest_modified_ns = max(self.newest_modified_ns, row["modified_ns"] or 0)
        if row["completed"] is not None:
            self.progress.append(bool(row["completed"]))

    def finish(self, series: CatalogSeries) -> SearchDocument:
        titles = [series.name]
        if self.display_title and self.display_title != series.name:
            titles.insert(0, self.display_title)
        titles.extend(_strings(self.metadata.get("alternative_titles")))
        year = _text(self.metadata.get("published_start"))[:4]
        return SearchDocument(
            series=series,
            titles=tuple(dict.fromkeys(titles)),
            volumes=tuple(self.volumes),
            creators=_strings(self.metadata.get("authors"))
            + _strings(self.metadata.get("artists")),
            publishers=_strings(self.metadata.get("publishers")),
            tags=_strings(self.metadata.get("tags")),
            description=_text(self.metadata.get("description")) or None,
            status=_text(self.metadata.get("status")) or None,
            year=year if year.isdigit() else None,
            read_state=self._read_state(),
            newest_modified_ns=self.newest_modified_ns,
        )

    def _read_state(self) -> str:
        if not self.progress:
            return READ_UNREAD
        if len(self.progress) == self.publications and all(self.progress):
            return READ_COMPLETED
        return READ_IN_PROGRESS


def _mount_conflict(
    connection: sqlite3.Connection,
    name: str,
    path: str,
    *,
    exclude_id: str | None = None,
) -> str:
    """Say which of the two unique columns collided, not just that one did."""
    clause = " AND id <> ?" if exclude_id else ""
    parameters: list[object] = [name, path]
    if exclude_id:
        parameters.append(exclude_id)
    row = connection.execute(
        "SELECT name, path FROM data_mounts "
        f"WHERE (name = ? COLLATE NOCASE OR path = ?){clause} LIMIT 1",
        parameters,
    ).fetchone()
    if row and row["path"] == path:
        return f"{row['name']} is already registered at that path"
    return f"Another data mount is already called “{name}”"


def _unique_index_on(
    connection: sqlite3.Connection, table: str, columns: list[str]
) -> bool:
    """Whether exactly these columns still carry a UNIQUE constraint."""
    for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if not index["unique"]:
            continue
        present = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (index["name"],),
            ).fetchall()
        ]
        if present == columns:
            return True
    return False


def _rebuild_table(connection: sqlite3.Connection, table: str, script: str) -> None:
    """Swap a table for one the script builds as `<table>_rebuilt`.

    SQLite cannot drop a UNIQUE constraint, so changing one means copying the
    rows. Foreign keys are suspended for the swap because dependent tables
    point at the name, not the underlying table.
    """
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(
            f"BEGIN IMMEDIATE;\n"
            f"DROP TABLE IF EXISTS {table}_rebuilt;\n"
            f"{script}\n"
            f"DROP TABLE {table};\n"
            f"ALTER TABLE {table}_rebuilt RENAME TO {table};\n"
            f"COMMIT;"
        )
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _visibility_predicate(
    visibility: CatalogVisibility, series_table: str | None = None
) -> str:
    if visibility == CatalogVisibility.ALL:
        return ""
    expected = 1 if visibility == CatalogVisibility.PRIVATE else 0
    if series_table:
        return f"{series_table}.is_private = {expected}"
    return (
        "EXISTS (SELECT 1 FROM catalog_series AS visible_series "
        "WHERE visible_series.id = publications.series_id "
        f"AND visible_series.is_private = {expected})"
    )


def _scope_predicate(scope: ReadScope | None) -> tuple[str, list[object]]:
    if scope is None or scope.unrestricted:
        return "1", []
    if scope.user_id:
        return (
            """
            EXISTS (
                SELECT 1 FROM access_grants
                WHERE access_grants.user_id = ?
                  AND access_grants.library_id = publications.library_id
                  AND (
                    access_grants.category = ''
                    OR (
                      access_grants.category = publications.category COLLATE NOCASE
                      AND (
                        access_grants.series_id = ''
                        OR access_grants.series_id = publications.series_id
                      )
                    )
                  )
            )
            """,
            [scope.user_id],
        )
    return "0", []


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _join(values: tuple[str, ...]) -> str:
    return "\x1f".join(values)


def _split(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.split("\x1f") if part)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_optional_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
