from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .domain import Page, Publication, PublicationPage, ScannedPublication, User

SCHEMA_VERSION = 1

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
    cover_page INTEGER NOT NULL DEFAULT 1
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
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def ping(self) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1

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

    def publication_by_id(self, publication_id: str) -> Publication | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM publications WHERE id = ?", (publication_id,)
            ).fetchone()
        return self._publication(row) if row else None

    def publication_by_path(self, relative_path: str) -> Publication | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM publications WHERE relative_path = ?", (relative_path,)
            ).fetchone()
        return self._publication(row) if row else None

    def page(self, publication_id: str, number: int) -> PublicationPage | None:
        publication = self.publication_by_id(publication_id)
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
        connection.execute(
            """
                INSERT INTO publications(
                    id, relative_path, library, category, series, filename, title, number,
                    description, authors_json, modified_ns, size, revision, page_count, cover_page
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    library=excluded.library, category=excluded.category, series=excluded.series,
                    filename=excluded.filename, title=excluded.title, number=excluded.number,
                    description=excluded.description, authors_json=excluded.authors_json,
                    modified_ns=excluded.modified_ns, size=excluded.size, revision=excluded.revision,
                    page_count=excluded.page_count, cover_page=excluded.cover_page
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
            ),
        )

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

    def remove_publications_except(self, relative_paths: set[str]) -> int:
        with self._connect() as connection:
            connection.execute("CREATE TEMP TABLE seen_paths(path TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO seen_paths(path) VALUES (?)",
                ((path,) for path in relative_paths),
            )
            # `cursor.rowcount` counts only rows this statement deleted; using the
            # connection's `total_changes` would also count the cascaded page rows.
            cursor = connection.execute(
                "DELETE FROM publications WHERE relative_path NOT IN (SELECT path FROM seen_paths)"
            )
            return cursor.rowcount

    def libraries(self) -> list[tuple[str, int]]:
        return self._grouped("library", ())

    def categories(self, library: str) -> list[tuple[str, int]]:
        return self._grouped("category", ("library = ?", library))

    def series(self, library: str, category: str) -> list[tuple[str, int]]:
        return self._grouped(
            "series", ("library = ? AND category = ?", library, category)
        )

    def _grouped(self, column: str, where: tuple[object, ...]) -> list[tuple[str, int]]:
        allowed = {"library", "category", "series"}
        if column not in allowed:
            raise ValueError("unsupported grouping")
        clause = f" WHERE {where[0]}" if where else ""
        parameters = where[1:] if where else ()
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
    ) -> tuple[list[Publication], int]:
        where, parameters = _publication_filter(library, category, series, query)
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


def _publication_filter(
    library: str | None, category: str | None, series: str | None, query: str | None
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
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), parameters


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
