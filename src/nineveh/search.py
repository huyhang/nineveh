"""Series-first catalog search.

The repository narrows the catalog to a bounded candidate set with SQLite's
full-text index — authorization and every active filter are applied there, in
SQL. Everything in this module is then a pure function of those candidates, so
ranking, match explanations and typo correction unit-test without a database.

Queries use AND semantics: every term has to match somewhere. A term that
matches nothing exactly is retried against the index's own vocabulary, so
"ninevh" still finds Nineveh without scanning the catalog.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

from .domain import (
    READ_STATES,
    SEARCH_SORTS,
    ReadScope,
    SearchDocument,
    SearchFacets,
    SearchFilters,
    SearchHit,
    SearchPage,
    SearchSuggestion,
    SearchVolume,
    SearchVolumeMatch,
)

__all__ = [
    "CatalogSearchService",
    "SearchIndexRepository",
    "correct_terms",
    "filters_from_params",
    "normalize_text",
    "rank_documents",
    "sort_hits",
    "tokenize",
]

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

# A term shorter than this is too easy to "correct" into something unrelated.
FUZZY_MIN_LENGTH = 4
FUZZY_RATIO = 0.82
MAX_TERMS = 12
MAX_REASONS = 3
MAX_VOLUME_MATCHES = 4
# Ranking is exact within this many candidates. A query matching more series
# than this is not one anybody reads to the end, and the window is ordered by
# the index's own relevance, so the best matches are always inside it.
CANDIDATE_WINDOW = 600

_LOCAL_TITLE = "Local title"
_TITLE = "Title"
_VOLUME = "Volume"


class SearchIndexRepository(Protocol):
    def search_candidates(
        self,
        terms: tuple[str, ...],
        *,
        filters: SearchFilters,
        scope: ReadScope,
        user_id: str,
        limit: int,
    ) -> tuple[list[SearchDocument], int]: ...

    def search_facets(
        self,
        terms: tuple[str, ...],
        *,
        filters: SearchFilters,
        scope: ReadScope,
        user_id: str,
    ) -> SearchFacets: ...

    def search_vocabulary(self) -> list[str]: ...

    def search_suggestions(
        self, terms: tuple[str, ...], *, scope: ReadScope, limit: int = 8
    ) -> list[SearchSuggestion]: ...


class CatalogSearchService:
    """Find, rank, explain and page the series one reader may see."""

    def __init__(self, repository: SearchIndexRepository, page_size: int = 24) -> None:
        self._repository = repository
        self._page_size = max(1, page_size)

    def search(
        self,
        query: str,
        *,
        scope: ReadScope,
        user_id: str,
        filters: SearchFilters | None = None,
        page: int = 1,
    ) -> SearchPage:
        filters = filters or SearchFilters()
        terms = tokenize(query)[:MAX_TERMS]
        if not terms and not filters.active:
            return SearchPage((), 0, 1, 1)
        hits, total, terms = self._find(terms, filters, scope, user_id)
        ordered = sort_hits(hits, filters.sort)
        facets = self._repository.search_facets(
            terms, filters=filters, scope=scope, user_id=user_id
        )
        return _paginate(ordered, total, page, self._page_size, facets)

    def suggestions(
        self, query: str, *, scope: ReadScope, limit: int = 8
    ) -> list[SearchSuggestion]:
        terms = tokenize(query)[:MAX_TERMS]
        if not terms or len(query.strip()) < 2:
            return []
        found = self._repository.search_suggestions(terms, scope=scope, limit=limit)
        if found:
            return found
        corrected = correct_terms(terms, self._repository.search_vocabulary())
        if corrected == terms:
            return []
        return self._repository.search_suggestions(corrected, scope=scope, limit=limit)

    def _find(
        self,
        terms: tuple[str, ...],
        filters: SearchFilters,
        scope: ReadScope,
        user_id: str,
    ) -> tuple[list[SearchHit], int, tuple[str, ...]]:
        """Rank the candidates, retrying once against the spelling dictionary."""
        hits, total = self._rank(terms, filters, scope, user_id)
        if hits or not terms:
            return hits, total, terms
        corrected = correct_terms(terms, self._repository.search_vocabulary())
        if corrected == terms:
            return hits, total, terms
        hits, total = self._rank(corrected, filters, scope, user_id)
        return hits, total, corrected

    def _rank(
        self,
        terms: tuple[str, ...],
        filters: SearchFilters,
        scope: ReadScope,
        user_id: str,
    ) -> tuple[list[SearchHit], int]:
        documents, matched = self._repository.search_candidates(
            terms,
            filters=filters,
            scope=scope,
            user_id=user_id,
            limit=CANDIDATE_WINDOW,
        )
        hits = rank_documents(documents, terms)
        # Inside the window the ranked count is the truth, because every
        # candidate was scored. Only a truncated window has to fall back to
        # what the index counted.
        total = len(hits) if len(documents) < CANDIDATE_WINDOW else matched
        return hits, total


def filters_from_params(params: Mapping[str, list[str]]) -> SearchFilters:
    """Coerce repeated query parameters, dropping anything unrecognised."""

    def values(name: str) -> tuple[str, ...]:
        seen = (item.strip() for item in params.get(name, []))
        return tuple(dict.fromkeys(item for item in seen if item))

    reading = next(iter(values("reading")), None)
    sort = next(iter(values("sort")), "relevance")
    return SearchFilters(
        library_ids=values("library"),
        categories=tuple(
            item for item in values("category") if item in {"comics", "manga"}
        ),
        collections=tuple(
            item for item in values("collection") if item in {"public", "private"}
        ),
        reading_state=reading if reading in READ_STATES else None,
        creators=values("creator"),
        publishers=values("publisher"),
        tags=values("tag"),
        statuses=values("status"),
        years=values("year"),
        sort=sort if sort in SEARCH_SORTS else "relevance",
    )


def normalize_text(value: str) -> str:
    """Case- and accent-insensitive form, so "cafe" matches "Café"."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(
        character for character in decomposed if not unicodedata.combining(character)
    ).casefold()


def tokenize(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(normalize_text(value)))


def correct_terms(terms: tuple[str, ...], vocabulary: list[str]) -> tuple[str, ...]:
    """Replace each unmatched term with the closest word the index knows.

    The dictionary is the full-text index's own term list, so a correction can
    only ever name something Nineveh has indexed. The corrected query is then
    run under the reader's own grants, which is what decides what they see.
    """
    if not vocabulary:
        return terms
    known = set(vocabulary)
    return tuple(
        term if term in known else _closest(term, vocabulary) for term in terms
    )


def _closest(term: str, vocabulary: list[str]) -> str:
    if len(term) < FUZZY_MIN_LENGTH:
        return term
    best, best_ratio = term, FUZZY_RATIO
    for candidate in vocabulary:
        if abs(len(candidate) - len(term)) > 3:
            continue
        ratio = SequenceMatcher(None, term, candidate).ratio()
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    return best


def rank_documents(
    documents: list[SearchDocument], terms: tuple[str, ...]
) -> list[SearchHit]:
    """Score each candidate, best first, and say why it matched.

    With no terms every candidate is kept unscored in title order, which is
    what a filter-only query should return.
    """
    if not terms:
        return [
            SearchHit(document=document, score=0.0, reasons=())
            for document in sorted(documents, key=_title_key)
        ]
    hits = []
    for document in documents:
        hit = _score(_IndexedDocument.build(document), terms)
        if hit is not None:
            hits.append(hit)
    return sorted(hits, key=lambda hit: (-hit.score, _title_key(hit.document)))


def sort_hits(hits: list[SearchHit], sort: str) -> list[SearchHit]:
    if sort == "title":
        return sorted(hits, key=_hit_title_key)
    if sort == "recent":
        return sorted(
            hits,
            key=lambda hit: (-hit.document.newest_modified_ns, _hit_title_key(hit)),
        )
    return sorted(hits, key=lambda hit: (-hit.score, _hit_title_key(hit)))


def _paginate(
    hits: list[SearchHit],
    total: int,
    page: int,
    page_size: int,
    facets: SearchFacets,
) -> SearchPage:
    page_count = max(1, -(-len(hits) // page_size))
    current = min(max(1, page), page_count)
    offset = (current - 1) * page_size
    return SearchPage(
        results=tuple(hits[offset : offset + page_size]),
        total=total,
        page=current,
        page_count=page_count,
        facets=facets,
    )


def _title_key(document: SearchDocument) -> str:
    return document.title.casefold()


def _hit_title_key(hit: SearchHit) -> str:
    return _title_key(hit.document)


@dataclass(frozen=True, slots=True)
class _IndexedValue:
    label: str
    weight: float
    raw: str
    folded: str
    tokens: frozenset[str]
    prefixes: tuple[str, ...]
    volume: SearchVolume | None = None


@dataclass(frozen=True, slots=True)
class _Match:
    weight: float
    reason: str
    volume: SearchVolume | None


@dataclass(frozen=True, slots=True)
class _IndexedDocument:
    document: SearchDocument
    values: tuple[_IndexedValue, ...]

    @classmethod
    def build(cls, document: SearchDocument) -> _IndexedDocument:
        values = (
            [_index(_LOCAL_TITLE, 100.0, document.titles[0])] if document.titles else []
        )
        values.extend(_index(_TITLE, 80.0, title) for title in document.titles[1:])
        values.extend(_index("Creator", 50.0, item) for item in document.creators)
        values.extend(
            _index(_VOLUME, 40.0, volume.title, volume=volume)
            for volume in document.volumes
        )
        values.extend(
            _index("Filename", 35.0, volume.filename, volume=volume)
            for volume in document.volumes
            if volume.filename and volume.filename != volume.title
        )
        values.extend(_index("Tag", 30.0, item) for item in document.tags)
        values.extend(_index("Publisher", 20.0, item) for item in document.publishers)
        if document.description:
            values.append(_index("Summary", 10.0, document.description))
        return cls(document=document, values=tuple(values))


def _index(
    label: str, weight: float, value: str, *, volume: SearchVolume | None = None
) -> _IndexedValue:
    tokens = tokenize(value)
    return _IndexedValue(
        label=label,
        weight=weight,
        raw=value,
        folded=normalize_text(value),
        tokens=frozenset(tokens),
        prefixes=tokens,
        volume=volume,
    )


def _score(indexed: _IndexedDocument, terms: tuple[str, ...]) -> SearchHit | None:
    total = 0.0
    scored: list[_Match] = []
    for term in terms:
        match = _match_term(indexed, term)
        if match is None:
            return None
        total += match.weight
        scored.append(match)
    total += _phrase_bonus(indexed, normalize_text(" ".join(terms)))
    return SearchHit(
        document=indexed.document,
        score=total,
        reasons=_top_reasons(scored),
        volumes=_matching_volumes(indexed, terms),
    )


def _match_term(indexed: _IndexedDocument, term: str) -> _Match | None:
    """The best-weighted field this term hits, if any."""
    best: _Match | None = None
    for value in indexed.values:
        weight = _term_weight(value, term)
        if weight is None:
            continue
        if best is None or weight > best.weight:
            best = _Match(weight, _reason(value), value.volume)
    return best


def _term_weight(value: _IndexedValue, term: str) -> float | None:
    if term in value.tokens:
        return value.weight
    if any(token.startswith(term) for token in value.prefixes):
        return value.weight * 0.6
    if term in value.folded:
        return value.weight * 0.4
    return None


def _phrase_bonus(indexed: _IndexedDocument, phrase: str) -> float:
    """A title containing the whole query outranks one matching each word."""
    if " " not in phrase:
        return 0.0
    for value in indexed.values:
        if value.label in (_LOCAL_TITLE, _TITLE) and phrase in value.folded:
            return 25.0
    return 0.0


def _reason(value: _IndexedValue) -> str:
    trimmed = value.raw if len(value.raw) <= 60 else value.raw[:57] + "…"
    return f"{value.label} “{trimmed}”"


def _top_reasons(scored: list[_Match]) -> tuple[str, ...]:
    ordered = sorted(scored, key=lambda match: -match.weight)
    return tuple(dict.fromkeys(match.reason for match in ordered))[:MAX_REASONS]


def _matching_volumes(
    indexed: _IndexedDocument, terms: tuple[str, ...]
) -> tuple[SearchVolumeMatch, ...]:
    """Every volume a term touched, so a reader can open one directly.

    Independent of which field won the score: a series whose title already
    matched still lists the volumes that matched too, because opening the
    right volume is faster than opening the series and looking.
    """
    best: dict[str, tuple[float, SearchVolumeMatch]] = {}
    for value in indexed.values:
        if value.volume is None:
            continue
        weight = max(
            (found for found in (_term_weight(value, term) for term in terms) if found),
            default=None,
        )
        if weight is None:
            continue
        current = best.get(value.volume.id)
        if current is None or weight > current[0]:
            best[value.volume.id] = (
                weight,
                SearchVolumeMatch(value.volume, _reason(value)),
            )
    ranked = sorted(best.values(), key=lambda item: -item[0])
    return tuple(match for _, match in ranked[:MAX_VOLUME_MATCHES])
