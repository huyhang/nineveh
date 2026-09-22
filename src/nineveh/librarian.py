"""The librarian agent's service layer.

Three concerns, deliberately separated so each can be unit-tested against the
repository port rather than a database:

* `LibrarianAuth` issues, re-scopes and revokes the agent's credentials.
* `LibrarianService` answers catalog and metadata questions from stored data.
* `IngestService` stages a proposed volume, then places it on a second call.

The split between staging and committing is the safety property. Staging writes
only to `/state` and returns the exact path it *would* use; committing is a
separate request under a separate scope. An agent that decides it is confident
cannot promote its own proposal, because confidence is not the credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import BinaryIO, Protocol

from .catalog import UnsafeArchive
from .domain import (
    CatalogSeries,
    LibrarianEvent,
    LibrarianToken,
    ManagedLibrary,
    Publication,
    ScannedPublication,
    SeriesMetadata,
)
from .ports import (
    CatalogRepository,
    LibrarianRepository,
    LibraryRepository,
    MetadataRepository,
)
from .reader import publication_order_key

SCOPE_OPTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "catalog:read",
        "Read the catalog",
        "Find series by title and list the volumes held on disk.",
    ),
    (
        "metadata:read",
        "Read stored metadata",
        "Answer author, status and publication questions from saved details.",
    ),
    (
        "ingest:stage",
        "Propose uploads",
        "Validate and stage a new volume. Writes nothing into the library.",
    ),
    (
        "ingest:commit",
        "Place uploads",
        "Move a staged volume onto the library disk. Never overwrites a file.",
    ),
)
SCOPES = frozenset(option[0] for option in SCOPE_OPTIONS)

# Splitting placement out of staging is what lets a remote token propose while
# only a local one disposes. Granting both to everything would collapse the
# distinction the two-step flow exists to create.
READ_SCOPES = frozenset({"catalog:read", "metadata:read"})

# Trimming the activity feed never removes permission history. Those events
# are rare enough that they never crowd the view, and they are the ones worth
# having years later; the read noise is what actually makes the log unreadable.
RETAINED_SEVERITIES: tuple[str, ...] = ("security",)
PURGE_WINDOWS: dict[str, int] = {"month": 30, "year": 365}
PURGE_LABELS: dict[str, str] = {"month": "one month", "year": "one year"}
PURGE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("month", "More than 1 month ago"),
    ("year", "More than 1 year ago"),
)


class LibrarianError(ValueError):
    """A failure that is safe to describe to the agent."""


class LibrarianNotFound(LibrarianError):
    """The referenced library, series or staged upload does not exist."""


class LibrarianConflict(LibrarianError):
    """The target path is taken. Nineveh never overwrites."""


class LibrarianTooLarge(LibrarianError):
    """The upload exceeds the configured ceiling."""


class LibrarianCatalog(
    CatalogRepository, LibraryRepository, MetadataRepository, Protocol
):
    pass


class IngestCatalog(CatalogRepository, LibraryRepository, Protocol):
    pass


class ArchiveValidator(Protocol):
    def inspect(
        self, path: Path, relative_path: str, publication_id: str
    ) -> ScannedPublication: ...


@dataclass(frozen=True, slots=True)
class SeriesMatch:
    series: CatalogSeries
    score: float
    matched_on: str
    matched_value: str


@dataclass(frozen=True, slots=True)
class Resolution:
    """Ranked candidates plus the two flags that spare a small model a judgement.

    A weak model asked to compare raw scores picks badly. `confident_match`
    says "this one, go ahead"; `ambiguous` says "ask a human". Both are
    computed here, where the scores actually live.
    """

    candidates: tuple[SeriesMatch, ...]
    confident_match: str | None
    ambiguous: bool


@dataclass(frozen=True, slots=True)
class SeriesInventory:
    series: CatalogSeries
    publications: tuple[Publication, ...]
    metadata: SeriesMetadata | None

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.publications)

    @property
    def latest(self) -> Publication | None:
        return self.publications[-1] if self.publications else None

    @property
    def provider_totals(self) -> dict[str, object]:
        """Provider counts, so "am I missing anything?" is answerable locally."""
        if not self.metadata:
            return {}
        effective = self.metadata.effective
        return {
            name: effective[name]
            for name in ("total_chapters", "final_volume", "status")
            if effective.get(name) is not None
        }


@dataclass(frozen=True, slots=True)
class StagedUpload:
    id: str
    series_id: str
    filename: str
    suggested_filename: str
    sibling_pattern: str | None
    target_path: str
    size: int
    page_count: int
    sha256: str
    duplicate_of: tuple[str, str] | None
    created_at: datetime

    @property
    def state(self) -> str:
        return "staged"


@dataclass(frozen=True, slots=True)
class PlacedUpload:
    ingest_id: str
    series_id: str
    library_id: str
    filename: str
    relative_path: str
    size: int
    page_count: int


class AuditTrail:
    """Writes the activity feed.

    Callers supply the sentence because they hold the facts; this class owns
    the mechanical parts -- identity, timestamp, the denormalised token name
    and the scopes that were in force. Storing the rendered summary rather than
    re-rendering later keeps the log a record of what was reported at the time.
    """

    def __init__(self, repository: LibrarianRepository) -> None:
        self._repository = repository

    def record(
        self,
        *,
        kind: str,
        action: str,
        severity: str,
        outcome: str,
        summary: str,
        token: LibrarianToken | None = None,
        actor: str | None = None,
        correlation_id: str | None = None,
        subject: tuple[str, str | None, str] | None = None,
        detail: dict[str, object] | None = None,
    ) -> LibrarianEvent:
        subject_type, subject_id, subject_label = subject or (None, None, None)
        event = LibrarianEvent(
            id=str(uuid.uuid4()),
            kind=kind,
            action=action,
            severity=severity,
            outcome=outcome,
            summary=summary,
            created_at=datetime.now(UTC),
            token_id=token.id if token else None,
            token_name=token.name if token else (actor or ""),
            actor=actor,
            correlation_id=correlation_id,
            scopes_at_time=token.scopes if token else (),
            subject_type=subject_type,
            subject_id=subject_id,
            subject_label=subject_label,
            detail=detail,
        )
        return self._repository.record_librarian_event(event)

    def events(self, **filters: object) -> list[LibrarianEvent]:
        return self._repository.librarian_events(**filters)  # type: ignore[arg-type]

    def purge(self, window: str, *, actor: str | None = None) -> int:
        """Trim old activity, then record that it happened.

        Permission history is exempt. Read noise is what makes the feed
        unreadable after a few months; a token grant from last year is exactly
        the thing someone comes looking for, and a prune that quietly removed
        it would make the log worth less than not having one.

        The prune is itself a `security` event, so the trail always explains
        its own gaps.
        """
        days = PURGE_WINDOWS.get(window)
        if days is None:
            raise LibrarianError("Choose how much history to keep")
        cutoff = datetime.now(UTC) - timedelta(days=days)
        removed = self._repository.purge_librarian_events(
            cutoff.isoformat(), keep_severities=RETAINED_SEVERITIES
        )
        self.record(
            kind="lifecycle",
            action="activity.cleared",
            severity="security",
            outcome="ok",
            summary=(
                f"{actor or 'an administrator'} cleared {removed} "
                f"entr{'y' if removed == 1 else 'ies'} older than "
                f"{PURGE_LABELS[window]}"
            ),
            actor=actor,
            detail={"window": window, "removed": removed, "before": cutoff.isoformat()},
        )
        return removed


class LibrarianAuth:
    """Issues and verifies the agent's bearer tokens."""

    PREFIX = "nvh_"

    def __init__(self, repository: LibrarianRepository, audit: AuditTrail) -> None:
        self._repository = repository
        self._audit = audit

    def issue(
        self,
        name: str,
        scopes: tuple[str, ...],
        library_ids: tuple[str, ...],
        *,
        actor: str | None = None,
    ) -> tuple[LibrarianToken, str]:
        cleaned = _clean_name(name)
        normalized = _clean_scopes(scopes)
        libraries = tuple(dict.fromkeys(library_ids))
        secret = f"{self.PREFIX}{secrets.token_urlsafe(32)}"
        token = self._repository.create_librarian_token(
            cleaned, self._hash(secret), normalized, libraries
        )
        self._audit.record(
            kind="lifecycle",
            action="token.issued",
            severity="security",
            outcome="ok",
            summary=f"{actor or 'an administrator'} issued token “{cleaned}”",
            token=token,
            actor=actor,
            detail={"scopes": list(normalized), "libraryIds": list(libraries)},
        )
        return token, secret

    def verify(self, secret: str | None) -> LibrarianToken | None:
        if not secret or not secret.startswith(self.PREFIX):
            return None
        token = self._repository.librarian_token_by_hash(self._hash(secret))
        if token:
            self._repository.touch_librarian_token(token.id)
        return token

    def tokens(self, *, include_revoked: bool = False) -> list[LibrarianToken]:
        return self._repository.librarian_tokens(include_revoked=include_revoked)

    def token(self, token_id: str) -> LibrarianToken | None:
        return self._repository.librarian_token(token_id)

    def update(
        self,
        token_id: str,
        *,
        name: str | None = None,
        scopes: tuple[str, ...] | None = None,
        library_ids: tuple[str, ...] | None = None,
        actor: str | None = None,
    ) -> LibrarianToken | None:
        """Re-scope a live token without rotating its secret.

        The agent keeps working across a permission change, which is the whole
        point: if changing privileges meant re-issuing a credential and
        reconfiguring the device, the temptation is to over-grant on day one.
        """
        before = self._repository.librarian_token(token_id)
        if before is None or not before.active:
            return None
        cleaned_name = _clean_name(name) if name is not None else None
        cleaned_scopes = _clean_scopes(scopes) if scopes is not None else None
        libraries = (
            tuple(dict.fromkeys(library_ids)) if library_ids is not None else None
        )
        after = self._repository.update_librarian_token(
            token_id,
            name=cleaned_name,
            scopes=cleaned_scopes,
            library_ids=libraries,
        )
        if after is None:
            return None
        for action, field, old, new in (
            ("token.renamed", "name", before.name, after.name),
            ("token.scopes_changed", "scopes", before.scopes, after.scopes),
            (
                "token.libraries_changed",
                "libraryIds",
                before.library_ids,
                after.library_ids,
            ),
        ):
            if old == new:
                continue
            self._audit.record(
                kind="lifecycle",
                action=action,
                severity="security",
                outcome="ok",
                summary=_change_summary(actor, after.name, field, old, new),
                token=after,
                actor=actor,
                detail={"before": _plain(old), "after": _plain(new)},
            )
        return after

    def revoke(
        self, token_id: str, *, actor: str | None = None
    ) -> LibrarianToken | None:
        token = self._repository.revoke_librarian_token(token_id)
        if token is None:
            return None
        self._audit.record(
            kind="lifecycle",
            action="token.revoked",
            severity="security",
            outcome="ok",
            summary=f"{actor or 'an administrator'} revoked token “{token.name}”",
            token=token,
            actor=actor,
        )
        return token

    @staticmethod
    def _hash(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class LibrarianService:
    """Answers the agent's questions from what Nineveh already indexed."""

    CONFIDENT_SCORE = 0.95
    CONFIDENT_MARGIN = 0.08

    def __init__(self, repository: LibrarianCatalog) -> None:
        self._repository = repository

    def libraries(self, token: LibrarianToken) -> list[ManagedLibrary]:
        return [
            library
            for library in self._repository.managed_libraries()
            if library.enabled and token.reaches(library.id)
        ]

    def resolve(
        self,
        token: LibrarianToken,
        query: str,
        *,
        library: str | None = None,
        limit: int = 5,
    ) -> Resolution:
        allowed = self.libraries(token)
        if library is not None:
            allowed = [
                item
                for item in allowed
                if item.id == library or item.name.casefold() == library.casefold()
            ]
            if not allowed:
                raise LibrarianNotFound(f"Unknown library: {library}")
        stored = self._repository.all_series_metadata()
        matches: list[SeriesMatch] = []
        for managed in allowed:
            for series in self._repository.catalog_series(library_id=managed.id):
                score, source, value = best_match(
                    query, _known_titles(series, stored.get(series.id))
                )
                if score > 0 and value is not None:
                    matches.append(SeriesMatch(series, score, source or "", value))
        matches.sort(key=lambda item: (-item.score, item.series.name.casefold()))
        ranked = tuple(matches[:limit])
        return Resolution(ranked, *self._confidence(query, matches))

    def _confidence(
        self, query: str, matches: list[SeriesMatch]
    ) -> tuple[str | None, bool]:
        if not matches:
            return None, True
        if len(normalize_title(query)) < 4 or matches[0].score < self.CONFIDENT_SCORE:
            return None, True
        runner_up = matches[1].score if len(matches) > 1 else 0.0
        if matches[0].score - runner_up < self.CONFIDENT_MARGIN:
            return None, True
        return matches[0].series.id, False

    def inventory(self, token: LibrarianToken, series_id: str) -> SeriesInventory:
        series = self._repository.catalog_series_by_id(series_id)
        if series is None or not token.reaches(series.library_id):
            raise LibrarianNotFound("Series not found")
        library = self._repository.managed_library(series.library_id)
        if library is None or not library.enabled:
            raise LibrarianNotFound("Series not found")
        publications = tuple(
            sorted(
                self._repository.publications_in_series(series_id),
                key=publication_order_key,
            )
        )
        return SeriesInventory(
            series, publications, self._repository.series_metadata(series_id)
        )

    def search_metadata(
        self,
        token: LibrarianToken,
        *,
        author: str | None = None,
        artist: str | None = None,
        publisher: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        title: str | None = None,
        limit: int = 24,
    ) -> list[tuple[CatalogSeries, SeriesMetadata]]:
        filters = (author, artist, publisher, status, tag, title)
        if not any(filters):
            raise LibrarianError("Search needs at least one filter")
        reachable = {library.id for library in self.libraries(token)}
        stored = self._repository.all_series_metadata()
        found = []
        for series in self._repository.catalog_series():
            if series.library_id not in reachable:
                continue
            metadata = stored.get(series.id)
            if metadata and metadata_matches(
                metadata.effective,
                author=author,
                artist=artist,
                publisher=publisher,
                status=status,
                tag=tag,
                title=title,
            ):
                found.append((series, metadata))
        found.sort(
            key=lambda item: (item[0].library.casefold(), item[0].name.casefold())
        )
        return found[:limit]


class IngestService:
    """Stages a proposed volume under `/state`, then places it on `/data`.

    Placement is the only write an agent can cause, and it cannot overwrite:
    the final step is a hard link, which fails outright when the name is taken.
    """

    STAGING_TTL_SECONDS = 24 * 3600
    _CHUNK = 64 * 1024

    def __init__(
        self,
        data_dir: Path,
        staging_dir: Path,
        repository: IngestCatalog,
        inspector: ArchiveValidator,
        max_upload_bytes: int,
    ) -> None:
        self._data_dir = data_dir
        self._staging_dir = staging_dir
        self._repository = repository
        self._inspector = inspector
        self._max_upload_bytes = max_upload_bytes

    def stage(
        self, token: LibrarianToken, series_id: str, filename: str, source: BinaryIO
    ) -> StagedUpload:
        series, library = self._target(token, series_id)
        cleaned = validate_filename(filename)
        self._sweep_expired()
        ingest_id = secrets.token_hex(16)
        archive_path = self._staging_dir / f"{ingest_id}.cbz"
        size, digest = self._store(source, archive_path)
        siblings = self._repository.publications_in_series(series_id)
        suggested, pattern = suggest_filename(cleaned, [s.filename for s in siblings])
        try:
            scanned = self._inspector.inspect(
                archive_path,
                self._relative(library, series, suggested),
                f"staged-{ingest_id}",
            )
        except (UnsafeArchive, zipfile.BadZipFile, OSError) as error:
            archive_path.unlink(missing_ok=True)
            raise LibrarianError(f"Archive rejected: {error}") from error
        staged = StagedUpload(
            id=ingest_id,
            series_id=series_id,
            filename=cleaned,
            suggested_filename=suggested,
            sibling_pattern=pattern,
            target_path=self._relative(library, series, suggested),
            size=size,
            page_count=scanned.publication.page_count,
            sha256=digest,
            duplicate_of=self._duplicate(siblings, size, digest),
            created_at=datetime.now(UTC),
        )
        self._write_sidecar(staged)
        return staged

    def staged(self, token: LibrarianToken, ingest_id: str) -> StagedUpload | None:
        record = self._load(ingest_id)
        if record is None:
            return None
        series = self._repository.catalog_series_by_id(record.series_id)
        if series is None or not token.reaches(series.library_id):
            return None
        return record

    def pending(self, token: LibrarianToken) -> list[StagedUpload]:
        found = []
        for sidecar in sorted(self._sidecars()):
            record = self._load(sidecar.stem)
            if record is None:
                continue
            series = self._repository.catalog_series_by_id(record.series_id)
            if series is not None and token.reaches(series.library_id):
                found.append(record)
        return found

    def commit(
        self, token: LibrarianToken, ingest_id: str, filename: str | None = None
    ) -> PlacedUpload:
        record = self._load(ingest_id)
        if record is None:
            raise LibrarianNotFound("Staged upload not found or expired")
        series, library = self._target(token, record.series_id)
        cleaned = validate_filename(filename or record.suggested_filename)
        relative = self._relative(library, series, cleaned)
        target = self._data_dir.joinpath(
            library.relative_path, series.category, series.name, cleaned
        )
        staged_file = self._staging_dir / f"{ingest_id}.cbz"
        if not staged_file.is_file():
            raise LibrarianNotFound("Staged upload not found or expired")
        # A cheap pre-check so a colliding name fails before the bytes are
        # copied; `os.link` below is what actually enforces it.
        if target.exists() or target.is_symlink():
            raise LibrarianConflict(f"Already exists: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._place(staged_file, target, relative)
        self._sidecar_path(ingest_id).unlink(missing_ok=True)
        return PlacedUpload(
            ingest_id=ingest_id,
            series_id=series.id,
            library_id=library.id,
            filename=cleaned,
            relative_path=relative,
            size=record.size,
            page_count=record.page_count,
        )

    def discard(self, token: LibrarianToken, ingest_id: str) -> bool:
        if self.staged(token, ingest_id) is None:
            return False
        (self._staging_dir / f"{ingest_id}.cbz").unlink(missing_ok=True)
        self._sidecar_path(ingest_id).unlink(missing_ok=True)
        return True

    def _place(self, source: Path, target: Path, relative: str) -> None:
        """Publish `source` at `target` so the name never names a partial file.

        `/state` and `/data` are separate mounts, so renaming across them
        raises EXDEV and `shutil.move` silently degrades to copy-then-delete --
        a crash mid-copy would leave a truncated archive under the real name
        for the next scan to index. Copy into the destination directory first,
        then link the finished file into place. `os.link` refuses an existing
        target, so no-overwrite is enforced by the placing call rather than by
        a check that can go stale between looking and moving.
        """
        handle, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=".nineveh-ingest-", suffix=".part"
        )
        temporary = Path(temporary_name)
        try:
            with open(handle, "wb") as output, source.open("rb") as archive:
                shutil.copyfileobj(archive, output, self._CHUNK)
                output.flush()
                os.fsync(output.fileno())
                # mkstemp creates 0600; the library is read by other accounts.
                os.fchmod(output.fileno(), 0o644)
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise LibrarianConflict(f"Already exists: {relative}") from None
            _sync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
        source.unlink(missing_ok=True)

    def _target(
        self, token: LibrarianToken, series_id: str
    ) -> tuple[CatalogSeries, ManagedLibrary]:
        series = self._repository.catalog_series_by_id(series_id)
        if series is None or not token.reaches(series.library_id):
            raise LibrarianNotFound("Series not found")
        library = self._repository.managed_library(series.library_id)
        if library is None or not library.enabled:
            raise LibrarianNotFound("Series not found")
        return series, library

    @staticmethod
    def _relative(library: ManagedLibrary, series: CatalogSeries, filename: str) -> str:
        return f"{library.relative_path}/{series.category}/{series.name}/{filename}"

    def _store(self, source: BinaryIO, destination: Path) -> tuple[int, str]:
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with destination.open("wb") as output:
            while chunk := source.read(self._CHUNK):
                size += len(chunk)
                if size > self._max_upload_bytes:
                    output.close()
                    destination.unlink(missing_ok=True)
                    raise LibrarianTooLarge("Upload exceeds the configured size limit")
                digest.update(chunk)
                output.write(chunk)
        return size, digest.hexdigest()

    def _duplicate(
        self, siblings: list[Publication], size: int, digest: str
    ) -> tuple[str, str] | None:
        """Find an identical archive already in the series, by content.

        Only same-size siblings are hashed, so the usual answer costs nothing.
        Catching this at stage time rather than at commit means the duplicate
        shows up in the proposal a human reads, not as a late rejection.

        Compares against the *indexed* catalog, so a volume placed seconds ago
        is invisible until the scan that follows it finishes. Widening this to
        walk the directory would trade a rare miss for a cost on every upload.
        """
        for sibling in siblings:
            if sibling.size != size:
                continue
            path = self._data_dir / sibling.relative_path
            try:
                if _file_digest(path) == digest:
                    return sibling.id, sibling.filename
            except OSError:  # pragma: no cover - unreadable sibling is not a match
                continue
        return None

    def _sidecars(self) -> list[Path]:
        if not self._staging_dir.is_dir():
            return []
        return list(self._staging_dir.glob("*.json"))

    def _sidecar_path(self, ingest_id: str) -> Path:
        return self._staging_dir / f"{ingest_id}.json"

    def _write_sidecar(self, staged: StagedUpload) -> None:
        payload = {
            "id": staged.id,
            "series_id": staged.series_id,
            "filename": staged.filename,
            "suggested_filename": staged.suggested_filename,
            "sibling_pattern": staged.sibling_pattern,
            "target_path": staged.target_path,
            "size": staged.size,
            "page_count": staged.page_count,
            "sha256": staged.sha256,
            "duplicate_of": list(staged.duplicate_of) if staged.duplicate_of else None,
            "created_at": staged.created_at.isoformat(),
        }
        self._sidecar_path(staged.id).write_text(json.dumps(payload), encoding="utf-8")

    def _load(self, ingest_id: str) -> StagedUpload | None:
        if not _valid_ingest_id(ingest_id):
            return None
        try:
            record = json.loads(
                self._sidecar_path(ingest_id).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        try:
            duplicate = record.get("duplicate_of")
            return StagedUpload(
                id=str(record["id"]),
                series_id=str(record["series_id"]),
                filename=str(record["filename"]),
                suggested_filename=str(record["suggested_filename"]),
                sibling_pattern=record.get("sibling_pattern"),
                target_path=str(record["target_path"]),
                size=int(record["size"]),
                page_count=int(record["page_count"]),
                sha256=str(record["sha256"]),
                duplicate_of=(duplicate[0], duplicate[1]) if duplicate else None,
                created_at=datetime.fromisoformat(str(record["created_at"])),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    def _sweep_expired(self) -> None:
        """Drop staged pairs older than the TTL, and any archive left orphaned.

        Sweeping only `*.json` would strand a `.cbz` whose sidecar was never
        written -- a crash between storing the bytes and recording them.
        """
        cutoff = time.time() - self.STAGING_TTL_SECONDS
        for sidecar in self._sidecars():
            try:
                if sidecar.stat().st_mtime > cutoff:
                    continue
                (self._staging_dir / f"{sidecar.stem}.cbz").unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - a vanished sidecar needs no sweep
                continue
        if not self._staging_dir.is_dir():
            return
        for archive in self._staging_dir.glob("*.cbz"):
            try:
                if archive.stat().st_mtime > cutoff:
                    continue
                if not self._sidecar_path(archive.stem).exists():
                    archive.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - same
                continue


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(text.split())


def best_match(
    query: str, titles: list[tuple[str, str]]
) -> tuple[float, str | None, str | None]:
    """Score a query against every known title for one series.

    Exact matches win outright. Substrings score in a deliberately modest band
    so a close fuzzy resemblance can still beat a long coincidental container.
    Queries of one character match only exactly -- anything looser ranks every
    series containing that letter, which hands a small model a list of
    near-certain wrong answers instead of an empty one.
    """
    best: tuple[float, str | None, str | None] = (0.0, None, None)
    normalized = normalize_title(query)
    if not normalized:
        return best
    for source, title in titles:
        candidate = normalize_title(title)
        if not candidate:
            continue
        if normalized == candidate:
            score = 1.0
        elif len(normalized) > 1 and (
            normalized in candidate or candidate in normalized
        ):
            overlap = min(len(normalized), len(candidate)) / max(
                len(normalized), len(candidate)
            )
            score = 0.6 + 0.2 * overlap
        else:
            score = SequenceMatcher(None, normalized, candidate).ratio()
            if score < 0.6:
                continue
        if score > best[0]:
            best = (score, source, title)
    return best


def metadata_matches(
    values: dict[str, object],
    *,
    author: str | None = None,
    artist: str | None = None,
    publisher: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    title: str | None = None,
) -> bool:
    for needle, field in (
        (author, "authors"),
        (artist, "artists"),
        (publisher, "publishers"),
        (tag, "tags"),
    ):
        if needle and not _contains_any(values.get(field), needle):
            return False
    if status:
        actual = values.get("status")
        if not isinstance(actual, str) or actual.casefold() != status.casefold():
            return False
    return not title or _title_matches(values, title)


def validate_filename(filename: str) -> str:
    """Accept one plain `.cbz` file name; reject anything path-shaped."""
    cleaned = filename.strip()
    if (
        not cleaned
        or len(cleaned) > 255
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or cleaned in {".", ".."}
        or Path(cleaned).name != cleaned
    ):
        raise LibrarianError("Filename must be a single file name without directories")
    if not cleaned.casefold().endswith(".cbz") or len(cleaned) <= len(".cbz"):
        raise LibrarianError("Filename must end in .cbz")
    return cleaned


_SIBLING = re.compile(
    r"^(?P<prefix>.*?)(?P<number>\d+)(?P<suffix>[^\d]*)\.cbz$", re.IGNORECASE
)
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def suggest_filename(filename: str, siblings: list[str]) -> tuple[str, str | None]:
    """Propose a name consistent with what the series already uses.

    Returned rather than applied. A silent rewrite hides the decision, and a
    hard rejection makes the agent guess again; showing both lets whoever
    approves the upload see what will happen before it does.
    """
    sanitized = _sanitize(filename)
    pattern = _sibling_pattern(siblings)
    if pattern is None:
        return sanitized, None
    prefix, width, suffix = pattern
    number = _NUMBER.search(Path(sanitized).stem)
    if number is None:
        return sanitized, f"{prefix}{'N' * width}{suffix}.cbz"
    value = number.group(1)
    rendered = value.zfill(width) if "." not in value else value
    return f"{prefix}{rendered}{suffix}.cbz", f"{prefix}{'N' * width}{suffix}.cbz"


def _sanitize(filename: str) -> str:
    cleaned = unicodedata.normalize("NFKC", filename)
    cleaned = _UNSAFE.sub("_", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned.casefold().endswith(".cbz"):
        raise LibrarianError("Filename must end in .cbz")
    stem = cleaned[: -len(".cbz")].rstrip(" .")
    if not stem:
        raise LibrarianError("Filename must end in .cbz")
    # Trim by bytes, not characters: the limit is the filesystem's.
    encoded = stem.encode("utf-8")[:240]
    stem = encoded.decode("utf-8", "ignore").rstrip(" .") or "volume"
    return f"{stem}.cbz"


def _sibling_pattern(siblings: list[str]) -> tuple[str, int, str] | None:
    """The dominant `prefix NNN suffix` shape, if the series has one."""
    shapes: dict[tuple[str, int, str], int] = {}
    for name in siblings:
        found = _SIBLING.match(name)
        if not found:
            continue
        key = (
            found.group("prefix"),
            len(found.group("number")),
            found.group("suffix"),
        )
        shapes[key] = shapes.get(key, 0) + 1
    if not shapes:
        return None
    best, count = max(shapes.items(), key=lambda item: item[1])
    return best if count >= 2 else None


def _known_titles(
    series: CatalogSeries, metadata: SeriesMetadata | None
) -> list[tuple[str, str]]:
    titles = [("localName", series.name)]
    if metadata is None:
        return titles
    effective = metadata.effective
    title = effective.get("title")
    if isinstance(title, str) and title.strip():
        titles.append(("title", title))
    alternatives = effective.get("alternative_titles")
    if isinstance(alternatives, list):
        titles.extend(
            ("alternativeTitle", item)
            for item in alternatives
            if isinstance(item, str) and item.strip()
        )
    return titles


def _title_matches(values: dict[str, object], needle: str) -> bool:
    title = values.get("title")
    if isinstance(title, str) and needle.casefold() in title.casefold():
        return True
    return _contains_any(values.get("alternative_titles"), needle)


def _contains_any(values: object, needle: str) -> bool:
    if not isinstance(values, list):
        return False
    wanted = needle.casefold()
    return any(isinstance(item, str) and wanted in item.casefold() for item in values)


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not 1 <= len(cleaned) <= 64:
        raise LibrarianError("Token name must be 1-64 characters")
    return cleaned


def _clean_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(sorted(set(scopes)))
    unsupported = set(normalized) - SCOPES
    if unsupported:
        raise LibrarianError(f"Unsupported scope: {min(unsupported)}")
    if not normalized:
        raise LibrarianError("Select at least one capability")
    return normalized


def _change_summary(
    actor: str | None, name: str, field: str, old: object, new: object
) -> str:
    who = actor or "an administrator"
    if field == "name":
        return f"{who} renamed token “{old}” to “{new}”"
    if field == "scopes":
        added = sorted(set(_plain(new)) - set(_plain(old)))
        removed = sorted(set(_plain(old)) - set(_plain(new)))
        parts = [f"+{item}" for item in added] + [f"-{item}" for item in removed]
        return f"{who} changed “{name}” permissions: {', '.join(parts)}"
    return f"{who} changed which libraries “{name}” can reach"


def _plain(value: object) -> list[str]:
    return list(value) if isinstance(value, (tuple, list)) else [str(value)]


def _valid_ingest_id(ingest_id: str) -> bool:
    return len(ingest_id) == 32 and all(
        character in "0123456789abcdef" for character in ingest_id
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(directory: Path) -> None:
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
