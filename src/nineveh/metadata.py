from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageOps, UnidentifiedImageError

from .archives import DiskCacheBudget
from .domain import CatalogSeries, MetadataCandidate, MetadataLookup, SeriesMetadata
from .ports import MetadataRepository

LOGGER = logging.getLogger(__name__)
MANGABAKA_BASE_URL = "https://api.mangabaka.org"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_COVER_BYTES = 16 * 1024 * 1024
CANDIDATE_CACHE_BYTES = 32 * 1024 * 1024
EDITABLE_FIELDS = frozenset(
    {
        "title",
        "alternative_titles",
        "authors",
        "artists",
        "description",
        "published_start",
        "published_end",
        "status",
        "content_rating",
        "media_type",
        "rating",
        "publishers",
        "tags",
        "final_volume",
        "total_chapters",
    }
)
LIST_FIELDS = frozenset(
    {"alternative_titles", "authors", "artists", "publishers", "tags"}
)
NUMBER_FIELDS = frozenset({"rating", "final_volume", "total_chapters"})
NUMBER_BOUNDS = {
    # MangaBaka's meta rating is a 0-100 scale carrying full float precision
    # (91.4937142857143 for Berserk), so the field is bounded but not stepped.
    "rating": (0.0, 100.0),
    "final_volume": (0.0, 10_000.0),
    "total_chapters": (0.0, 100_000.0),
}
# MangaBaka returns every tag it knows -- Berserk alone carries 320 -- which
# buries the synopsis under a wall of chips. The untruncated payload stays in
# the stored raw record; only the reader-facing list is bounded.
MAX_LIST_VALUES = 20


class MetadataError(RuntimeError):
    """A safe, administrator-facing metadata failure."""


class JsonTransport(Protocol):
    def get_json(self, url: str) -> dict[str, Any]: ...

    def get_bytes(self, url: str, maximum: int) -> bytes: ...


class UrllibTransport:
    """Small bounded HTTP adapter; provider logic never depends on urllib."""

    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout
        self._headers = {
            "Accept": "application/json",
            "User-Agent": "Nineveh/0.1 (self-hosted metadata client)",
        }

    def get_json(self, url: str) -> dict[str, Any]:
        payload = self.get_bytes(url, MAX_RESPONSE_BYTES)
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MetadataError("MangaBaka returned an invalid response") from error
        if not isinstance(parsed, dict):
            raise MetadataError("MangaBaka returned an unexpected response")
        return parsed

    def get_bytes(self, url: str, maximum: int) -> bytes:
        request = Request(url, headers=self._headers)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = response.read(maximum + 1)
        except HTTPError as error:
            if error.code == 429:
                message = "MangaBaka is temporarily rate limiting requests"
                retry_after = _retry_after_seconds(error)
                if retry_after:
                    message += f"; try again in {retry_after} seconds"
            elif error.code == 404:
                message = "MangaBaka could not find that series"
            else:
                message = f"MangaBaka returned HTTP {error.code}"
            raise MetadataError(message) from error
        except (OSError, URLError) as error:
            raise MetadataError("MangaBaka could not be reached") from error
        if len(payload) > maximum:
            raise MetadataError("MangaBaka returned more data than Nineveh accepts")
        return payload


class PersistentRateLimiter:
    """A strict rolling limiter whose reservations survive process restarts."""

    def __init__(
        self,
        repository: MetadataRepository,
        limit: Callable[[], int],
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repository = repository
        self._limit = limit
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                limit = min(30, max(1, self._limit()))
                wait = self._repository.reserve_metadata_request(limit, self._clock())
                if wait <= 0:
                    return
                self._sleeper(wait)


@dataclass(frozen=True, slots=True)
class ProviderSeries:
    provider_id: int
    canonical_url: str
    values: dict[str, Any]
    raw: dict[str, Any]
    provider_updated_at: str | None
    cover_url: str | None
    merged_with: int | None = None


class MetadataProvider(Protocol):
    def search(self, query: str, limit: int = 5) -> list[MetadataCandidate]: ...

    def fetch(self, provider_id: int) -> ProviderSeries: ...

    def cover(self, url: str) -> bytes: ...


class MangaBakaProvider:
    name = "mangabaka"

    def __init__(
        self, transport: JsonTransport, limiter: PersistentRateLimiter
    ) -> None:
        self._transport = transport
        self._limiter = limiter

    def search(self, query: str, limit: int = 5) -> list[MetadataCandidate]:
        cleaned = query.strip()
        if not cleaned:
            raise MetadataError("Enter a series title to search")
        payload = self._json(
            "/v2/series/search",
            {"q": cleaned, "limit": min(10, max(1, limit)), "schema": "full"},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise MetadataError("MangaBaka returned an unexpected search response")
        candidates: list[MetadataCandidate] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(
                item.get("id"), (int, float)
            ):
                continue
            values = _normalized_values(item)
            candidates.append(
                MetadataCandidate(
                    provider_id=int(item["id"]),
                    title=str(values.get("title") or "Untitled series"),
                    alternative_titles=tuple(values.get("alternative_titles") or ()),
                    authors=tuple(values.get("authors") or ()),
                    artists=tuple(values.get("artists") or ()),
                    description=_optional_text(values.get("description")),
                    year=_year(values.get("published_start")),
                    media_type=_optional_text(values.get("media_type")),
                    status=_optional_text(values.get("status")),
                    rating=float(values["rating"])
                    if isinstance(values.get("rating"), (int, float))
                    else None,
                    publishers=tuple(values.get("publishers") or ()),
                    tags=tuple(values.get("tags") or ()),
                    cover_url=_cover_url(item),
                    source_url=_canonical_url(item.get("canonical_url")),
                )
            )
        return candidates

    def fetch(self, provider_id: int) -> ProviderSeries:
        payload = self._json(f"/v2/series/{provider_id}", {"schema": "full"})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MetadataError("MangaBaka returned an unexpected series response")
        # v2 returns retired series rather than 404ing, so a deleted record
        # would otherwise be stored as a hollow match.
        if data.get("state") == "deleted":
            raise MetadataError("MangaBaka has retired that series; search again")
        merged = data.get("merged_with")
        return ProviderSeries(
            provider_id=int(data.get("id", provider_id)),
            canonical_url=_canonical_url(data.get("canonical_url"))
            or "https://mangabaka.org/",
            values=_normalized_values(data),
            raw=data,
            provider_updated_at=_optional_text(data.get("last_updated_at")),
            cover_url=_cover_url(data),
            merged_with=int(merged) if isinstance(merged, (int, float)) else None,
        )

    def cover(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise MetadataError("MangaBaka supplied an unsafe cover URL")
        self._limiter.acquire()
        return self._transport.get_bytes(url, MAX_COVER_BYTES)

    def _json(self, path: str, parameters: dict[str, object]) -> dict[str, Any]:
        self._limiter.acquire()
        payload = self._transport.get_json(
            f"{MANGABAKA_BASE_URL}{path}?{urlencode(parameters)}"
        )
        status = payload.get("status")
        if status != 200:
            message = payload.get("message")
            raise MetadataError(
                str(message) if isinstance(message, str) else "MangaBaka request failed"
            )
        return payload


class MetadataCoverStore:
    """Series artwork under `/state`.

    Series covers are owned data and kept until unlinked. Candidate thumbnails
    are throwaway search results, so they live in their own subdirectory under
    a byte budget -- every suggestion list would otherwise leave up to ten
    files behind for good, in the one `/state` directory the README tells you
    to back up.
    """

    def __init__(
        self,
        directory: Path,
        max_image_pixels: int,
        candidate_cache_bytes: int = CANDIDATE_CACHE_BYTES,
    ) -> None:
        self._directory = directory
        self._max_image_pixels = max_image_pixels
        self._candidates = DiskCacheBudget(
            directory / "candidates", "*.webp", candidate_cache_bytes
        )

    def save_provider(self, series_id: str, payload: bytes) -> Path:
        return self._save(self._path(series_id, "provider"), payload)

    def save_custom(self, series_id: str, payload: bytes) -> Path:
        return self._save(self._path(series_id, "custom"), payload)

    def save_candidate(self, provider_id: int, payload: bytes) -> Path:
        destination = self._save(self._candidate_path(provider_id), payload)
        self._candidates.added(destination, destination.stat().st_size)
        return destination

    def candidate(self, provider_id: int) -> Path | None:
        candidate = self._candidate_path(provider_id)
        return candidate if candidate.is_file() else None

    def cover(self, series_id: str) -> Path | None:
        for kind in ("custom", "provider"):
            candidate = self._path(series_id, kind)
            if candidate.is_file():
                return candidate
        return None

    def remove_provider(self, series_id: str) -> None:
        self._path(series_id, "provider").unlink(missing_ok=True)

    def remove_custom(self, series_id: str) -> None:
        self._path(series_id, "custom").unlink(missing_ok=True)

    def has_custom(self, series_id: str) -> bool:
        return self._path(series_id, "custom").is_file()

    def _save(self, destination: Path, payload: bytes) -> Path:
        if len(payload) > MAX_COVER_BYTES:
            raise MetadataError("Cover images may not exceed 16 MiB")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(io.BytesIO(payload)) as opened:
                if opened.width * opened.height > self._max_image_pixels:
                    raise MetadataError("Cover dimensions exceed the configured limit")
                image = ImageOps.exif_transpose(opened)
                image.thumbnail((960, 1440), Image.Resampling.LANCZOS)
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGB")
                with tempfile.NamedTemporaryFile(
                    prefix="series-cover-",
                    suffix=".webp",
                    dir=destination.parent,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                try:
                    image.save(temporary_path, format="WEBP", quality=86, method=4)
                    os.replace(temporary_path, destination)
                finally:
                    temporary_path.unlink(missing_ok=True)
        except MetadataError:
            raise
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise MetadataError(
                "The selected file is not a safe supported image"
            ) from error
        return destination

    def _path(self, series_id: str, kind: str) -> Path:
        digest = hashlib.sha256(series_id.encode()).hexdigest()
        return self._directory / f"{digest}-{kind}.webp"

    def _candidate_path(self, provider_id: int) -> Path:
        return self._directory / "candidates" / f"{provider_id}.webp"


class MetadataService:
    def __init__(
        self,
        repository: MetadataRepository,
        provider: MetadataProvider,
        covers: MetadataCoverStore,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self.covers = covers

    def lookup(self, series: CatalogSeries, query: str | None = None) -> MetadataLookup:
        self._require_manga(series)
        try:
            candidates = self._provider.search(query or series.name)
        except MetadataError as error:
            self._repository.replace_metadata_lookup(series.id, [], str(error))
            raise
        return self._repository.replace_metadata_lookup(series.id, candidates)

    def match(self, series: CatalogSeries, provider_id: int) -> SeriesMetadata:
        self._require_manga(series)
        provider = self._provider.fetch(provider_id)
        if provider.merged_with and provider.merged_with != provider.provider_id:
            provider = self._provider.fetch(provider.merged_with)
        metadata = self._repository.save_series_metadata(
            series.id,
            provider.provider_id,
            provider.canonical_url,
            provider.values,
            provider.raw,
            provider.provider_updated_at,
        )
        if provider.cover_url:
            try:
                self.covers.save_provider(
                    series.id, self._provider.cover(provider.cover_url)
                )
            except MetadataError as error:
                LOGGER.warning("Could not cache cover for %s: %s", series.id, error)
        return metadata

    def candidate_cover(self, series_id: str, provider_id: int) -> Path:
        cached = self.covers.candidate(provider_id)
        if cached:
            return cached
        lookup = self._repository.metadata_lookup(series_id)
        candidate = next(
            (item for item in lookup.candidates if item.provider_id == provider_id),
            None,
        )
        if not candidate or not candidate.cover_url:
            raise MetadataError("Candidate cover is unavailable")
        return self.covers.save_candidate(
            provider_id, self._provider.cover(candidate.cover_url)
        )

    def refresh(self, series: CatalogSeries) -> SeriesMetadata:
        current = self._repository.series_metadata(series.id)
        if not current:
            raise MetadataError("Match this series before refreshing it")
        return self.match(series, current.provider_id)

    def update(self, series_id: str, supplied: dict[str, object]) -> SeriesMetadata:
        current = self._repository.series_metadata(series_id)
        if not current:
            raise MetadataError("Match this series before editing its metadata")
        overrides = dict(current.overrides)
        for field, value in supplied.items():
            if field not in EDITABLE_FIELDS:
                raise MetadataError(f"Unsupported metadata field: {field}")
            normalized = _normalize_override(field, value)
            if normalized == current.values.get(field):
                overrides.pop(field, None)
            else:
                overrides[field] = normalized
        return self._repository.replace_metadata_overrides(series_id, overrides)

    def unlink(self, series_id: str) -> None:
        self._repository.delete_series_metadata(series_id)
        self.covers.remove_provider(series_id)

    @staticmethod
    def _require_manga(series: CatalogSeries) -> None:
        if series.category.casefold() != "manga":
            raise MetadataError("MangaBaka metadata is available only for manga series")


def _ranked_titles(data: dict[str, Any]) -> list[str]:
    """Order every known title, best display candidate first.

    MangaBaka flags one primary title *per language*, so "the first entry with
    `is_primary`" is really "whichever language the payload happens to list
    first" -- for Berserk that is Korean. Rank by language instead: an official
    English title, then the romanised Japanese one, then the native script,
    then everything else.
    """
    raw = data.get("titles")
    entries = [
        item
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict) and _optional_text(item.get("title"))
    ]

    def language(entry: dict[str, Any]) -> str:
        return str(entry.get("language") or "")

    def official_primary_first(entry: dict[str, Any]) -> tuple[bool, bool]:
        traits = entry.get("traits")
        traits = traits if isinstance(traits, list) else []
        return ("official" not in traits, not entry.get("is_primary"))

    english = sorted(
        (item for item in entries if language(item).startswith("en")),
        key=official_primary_first,
    )
    romanised = [item for item in entries if language(item) == "ja-Latn"]
    native = [item for item in entries if language(item) == "ja"]
    rest = [item for item in entries if language(item)[:2] not in {"en", "ja"}]

    ordered: list[str] = []
    for item in (*english, *romanised, *native, *rest):
        candidate = _optional_text(item.get("title"))
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _normalized_values(data: dict[str, Any]) -> dict[str, Any]:
    ranked = _ranked_titles(data)
    title = ranked[0] if ranked else None
    alternatives = _capped(ranked[1:])
    published = data.get("published")
    published = published if isinstance(published, dict) else {}
    publishers = data.get("publishers")
    publisher_names = _capped(
        _unique_strings(
            item.get("name") for item in publishers or () if isinstance(item, dict)
        )
    )
    tags = data.get("tags")
    tag_names = _capped(_unique_strings(_tag_name(item) for item in tags or ()))
    return {
        "title": title,
        "alternative_titles": alternatives,
        "authors": _capped(_unique_strings(data.get("authors") or ())),
        "artists": _capped(_unique_strings(data.get("artists") or ())),
        "description": _optional_text(data.get("description")),
        "published_start": _optional_text(published.get("start_date")),
        "published_end": _optional_text(published.get("end_date")),
        "status": _optional_text(data.get("status")),
        "content_rating": _optional_text(data.get("content_rating")),
        "media_type": _optional_text(data.get("type")),
        "rating": data.get("rating")
        if isinstance(data.get("rating"), (int, float))
        else None,
        "publishers": publisher_names,
        "tags": tag_names,
        "final_volume": data.get("final_volume")
        if isinstance(data.get("final_volume"), (int, float))
        else None,
        "total_chapters": data.get("total_chapters")
        if isinstance(data.get("total_chapters"), (int, float))
        else None,
    }


def _cover_url(data: dict[str, Any]) -> str | None:
    cover = data.get("cover")
    if not isinstance(cover, dict):
        return None
    for key in ("x350", "raw", "x250", "x150"):
        value = cover.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _canonical_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = urljoin("https://mangabaka.org/", value.strip())
    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (
        hostname == "mangabaka.org" or hostname.endswith(".mangabaka.org")
    ):
        return None
    return candidate


def _tag_name(value: object) -> object:
    if not isinstance(value, dict):
        return value
    direct = value.get("name")
    if isinstance(direct, str):
        return direct
    tag = value.get("tag")
    return tag.get("name") if isinstance(tag, dict) else None


def _unique_strings(values) -> list[str]:
    found: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in found:
            found.append(value.strip())
    return found


def _capped(values: list[str]) -> list[str]:
    return values[:MAX_LIST_VALUES]


def _retry_after_seconds(error: HTTPError) -> int | None:
    """Turn a `Retry-After` header into advice an administrator can act on."""
    headers = getattr(error, "headers", None)
    raw = headers.get("Retry-After") if headers else None
    if not isinstance(raw, str) or not raw.strip().isdigit():
        return None
    return min(int(raw.strip()), 3600)


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _year(value: object) -> int | None:
    text = _optional_text(value)
    if not text or len(text) < 4 or not text[:4].isdigit():
        return None
    return int(text[:4])


def _normalize_override(field: str, value: object) -> object:
    if field in LIST_FIELDS:
        if isinstance(value, str):
            # One entry per line, never per comma: real titles and credits
            # contain commas ("Oh, My Sweet Alien!", "Smith, John"), so a
            # comma split silently shredded them into extra entries the moment
            # an administrator opened the form and pressed Save.
            entries = _unique_strings(value.splitlines())
        elif isinstance(value, (list, tuple)):
            entries = _unique_strings(value)
        else:
            entries = []
        if len(entries) > MAX_LIST_VALUES:
            raise MetadataError(
                f"{field.replace('_', ' ').title()} accepts at most "
                f"{MAX_LIST_VALUES} entries"
            )
        return entries
    if field in NUMBER_FIELDS:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise MetadataError(
                f"{field.replace('_', ' ').title()} must be a number"
            ) from error
        if field != "rating" and not number.is_integer():
            raise MetadataError(
                f"{field.replace('_', ' ').title()} must be a whole number"
            )
        # The form's min/max only bind a browser; the JSON API reaches the same
        # code path, so the bounds are enforced here too.
        low, high = NUMBER_BOUNDS[field]
        if not low <= number <= high:
            raise MetadataError(
                f"{field.replace('_', ' ').title()} must be between "
                f"{low:g} and {high:g}"
            )
        return int(number) if number.is_integer() else number
    return _optional_text(value)
