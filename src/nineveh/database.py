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
    CategoryUsage,
    LibraryUsage,
    ManagedLibrary,
    Page,
    Publication,
    PublicationPage,
    ReadScope,
    ScannedPublication,
    SeriesUsage,
    User,
)

SCHEMA_VERSION = 2

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
    expires_at TEXT NOT NULL
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
    PRIMARY KEY(publication_id, number)
);

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
            connection.executescript(SCOPE_INDEX)
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
                uncompressed_size, crc, width, height
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
