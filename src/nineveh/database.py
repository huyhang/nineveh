from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .domain import (
    AccessGrant,
    CatalogSeries,
    CategoryUsage,
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
    SeriesMetadata,
    SeriesMetadataState,
    SeriesMetadataSummary,
    SeriesUsage,
    SpreadAnalysis,
    User,
)

SCHEMA_VERSION = 6
# The release that began recording `ComicInfo.xml` spread markers. Databases
# older than this need one reinspection pass to pick them up.
SPREAD_MARKER_VERSION = 4
# The release that replaced dimension-based spread detection with the gutter
# reader. An anchor stored by the old heuristic was the first wide page, which
# is not where pairing should start, so those rows have to be recomputed rather
# than trusted.
SEAM_DETECTION_VERSION = 6

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

CREATE TABLE IF NOT EXISTS managed_libraries (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    relative_path TEXT NOT NULL COLLATE NOCASE UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_series (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL REFERENCES managed_libraries(id) ON DELETE CASCADE,
    category TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL COLLATE NOCASE,
    UNIQUE(library_id, category, name)
);

CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
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
    series_id TEXT REFERENCES catalog_series(id)
);
CREATE INDEX IF NOT EXISTS publications_hierarchy
    ON publications(library, category, series, title);

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
"""

# Applied after `SCHEMA`, because a v1 database only grows the columns it indexes
# once `_ensure_scope_columns` has added them.
SCOPE_INDEX = """
CREATE INDEX IF NOT EXISTS publications_scope
    ON publications(library_id, category, series_id);
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
            self._ensure_scope_columns(connection)
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
            if 0 < version < SCHEMA_VERSION:
                self._backfill_scope(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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

    def initialize_libraries(self, relative_paths: list[str]) -> None:
        """Register the initial data-root children once, preserving later removals."""
        with self._connect() as connection:
            initialized = connection.execute(
                "SELECT 1 FROM application_metadata WHERE key = 'libraries_initialized'"
            ).fetchone()
            if initialized:
                return
            now = _now_iso()
            connection.executemany(
                """
                INSERT INTO managed_libraries(id, name, relative_path, enabled, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                [(str(uuid.uuid4()), path, path, now) for path in relative_paths],
            )
            connection.execute(
                """
                INSERT INTO application_metadata(key, value)
                VALUES ('libraries_initialized', '1')
                """
            )

    def managed_libraries(
        self, *, include_disabled: bool = False
    ) -> list[ManagedLibrary]:
        where = "" if include_disabled else " WHERE enabled = 1"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM managed_libraries{where} ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._library(row) for row in rows]

    def managed_library(self, library_id: str) -> ManagedLibrary | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM managed_libraries WHERE id = ?", (library_id,)
            ).fetchone()
        return self._library(row) if row else None

    def add_library(self, relative_path: str) -> ManagedLibrary:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM managed_libraries
                WHERE relative_path = ? COLLATE NOCASE
                """,
                (relative_path,),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE managed_libraries SET enabled = 1 WHERE id = ?",
                    (row["id"],),
                )
                library_id = row["id"]
            else:
                library_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO managed_libraries(
                        id, name, relative_path, enabled, created_at
                    ) VALUES (?, ?, ?, 1, ?)
                    """,
                    (library_id, relative_path, relative_path, _now_iso()),
                )
        library = self.managed_library(library_id)
        if library is None:
            raise RuntimeError(f"Managed library disappeared during add: {library_id}")
        return library

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
        metadata = self.series_metadata(series_id)
        if metadata is None:  # pragma: no cover - guarded by rowcount
            raise RuntimeError("Series metadata disappeared after update")
        return metadata

    def delete_series_metadata(self, series_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM series_metadata WHERE series_id = ?", (series_id,)
            )

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

    def publication_by_path(self, relative_path: str) -> Publication | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM publications WHERE relative_path = ?", (relative_path,)
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

    def latest_reading_progress(self, user_id: str) -> ReadingProgress | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM reading_progress
                WHERE user_id = ?
                ORDER BY updated_at DESC, publication_id
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return self._reading_progress(row) if row else None

    def save_reading_progress(
        self,
        user_id: str,
        publication_id: str,
        page: int,
        mode: str,
        completed: bool,
    ) -> ReadingProgress:
        updated_at = _now_iso()
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO reading_progress(
                    user_id, publication_id, page_number, mode, completed, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, publication_id) DO UPDATE SET
                    page_number=excluded.page_number,
                    mode=excluded.mode,
                    completed=excluded.completed,
                    updated_at=excluded.updated_at
                RETURNING *
                """,
                (user_id, publication_id, page, mode, int(completed), updated_at),
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
                ON CONFLICT(relative_path) DO UPDATE SET
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
                    INSERT INTO managed_libraries(id, name, relative_path, enabled, created_at)
                    VALUES (?, ?, ?, 1, ?)
                    """,
                    (library_id, item.library, item.library, _now_iso()),
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
            connection.execute("CREATE TEMP TABLE seen_paths(path TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO seen_paths(path) VALUES (?)",
                ((path,) for path in relative_paths),
            )
            # `cursor.rowcount` counts only rows this statement deleted; using the
            # connection's `total_changes` would also count the cascaded page rows.
            library_clause = " AND library_id = ?" if library_id else ""
            parameters = (library_id,) if library_id else ()
            cursor = connection.execute(
                "DELETE FROM publications "
                "WHERE relative_path NOT IN (SELECT path FROM seen_paths)"
                f"{library_clause}",
                parameters,
            )
            return cursor.rowcount

    def catalog_series(
        self,
        *,
        series_id: str | None = None,
        library_id: str | None = None,
        category: str | None = None,
        query: str | None = None,
        scope: ReadScope | None = None,
    ) -> list[CatalogSeries]:
        clauses = ["managed_libraries.enabled = 1"]
        parameters: list[object] = []
        if series_id:
            clauses.append("catalog_series.id = ?")
            parameters.append(series_id)
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
        items = self.catalog_series(series_id=series_id, scope=scope)
        return items[0] if items else None

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

    @staticmethod
    def _catalog_series(row: sqlite3.Row) -> CatalogSeries:
        return CatalogSeries(
            id=row["id"],
            library_id=row["library_id"],
            library=row["library"],
            category=row["category"],
            name=row["name"],
            publication_count=row["publication_count"],
            first_publication_id=row["publication_id"],
            first_publication_revision=row["publication_revision"],
        )

    def libraries(self, scope: ReadScope | None = None) -> list[tuple[str, int]]:
        return self._grouped("library", (), scope)

    def categories(
        self, library: str, scope: ReadScope | None = None
    ) -> list[tuple[str, int]]:
        return self._grouped("category", ("library = ?", library), scope)

    def series(
        self, library: str, category: str, scope: ReadScope | None = None
    ) -> list[tuple[str, int]]:
        return self._grouped(
            "series", ("library = ? AND category = ?", library, category), scope
        )

    def _grouped(
        self, column: str, where: tuple[object, ...], scope: ReadScope | None
    ) -> list[tuple[str, int]]:
        allowed = {"library", "category", "series"}
        if column not in allowed:
            raise ValueError("unsupported grouping")
        clauses = [str(where[0])] if where else []
        parameters = list(where[1:]) if where else []
        scope_clause, scope_parameters = _scope_predicate(scope)
        clauses.append(scope_clause)
        parameters.extend(scope_parameters)
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
    ) -> tuple[list[Publication], int]:
        where, parameters = _publication_filter(library, category, series, query, scope)
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
) -> tuple[str, list[object]]:
    """Build the WHERE fragment and bound parameters for a publication search."""
    clauses: list[str] = []
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
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), parameters


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
