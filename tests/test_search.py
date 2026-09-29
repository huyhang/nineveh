"""Catalog search: ranking, correction, facets and the two surfaces.

Ranking is a pure function of the documents the repository hands over, so
most of this file needs no database at all. The tests that do use one are
about retrieval and authorization, which are the repository's job.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ADMIN_PASSWORD, authorization, scanned_client, storage, write_cbz
from fastapi.testclient import TestClient

from nineveh.config import Settings
from nineveh.domain import (
    AccessGrant,
    CatalogSeries,
    CatalogVisibility,
    ReadScope,
    SearchDocument,
    SearchFacets,
    SearchFilters,
    SearchVolume,
)
from nineveh.search import (
    CatalogSearchService,
    correct_terms,
    filters_from_params,
    normalize_text,
    rank_documents,
    sort_hits,
    tokenize,
)

# --- Pure ranking -----------------------------------------------------------


def _series(name: str, **overrides) -> CatalogSeries:
    return CatalogSeries(
        id=overrides.get("id", name.casefold().replace(" ", "-")),
        library_id=overrides.get("library_id", "library-1"),
        library=overrides.get("library", "Main Library"),
        category=overrides.get("category", "comics"),
        name=name,
        is_private=overrides.get("is_private", False),
        publication_count=overrides.get("publication_count", 1),
        first_publication_id="pub-1",
        first_publication_revision="rev-1",
    )


def _document(name: str, **overrides) -> SearchDocument:
    return SearchDocument(
        series=_series(name, **overrides),
        titles=overrides.get("titles", (name,)),
        volumes=overrides.get("volumes", ()),
        creators=overrides.get("creators", ()),
        publishers=overrides.get("publishers", ()),
        tags=overrides.get("tags", ()),
        description=overrides.get("description"),
        status=overrides.get("status"),
        year=overrides.get("year"),
        read_state=overrides.get("read_state", "unread"),
        newest_modified_ns=overrides.get("newest_modified_ns", 0),
    )


def test_normalising_ignores_case_and_accents():
    assert normalize_text("Café ÉCLAIR") == "cafe eclair"
    assert tokenize("The Lion's Road!") == ("the", "lion", "s", "road")


def test_a_title_match_outranks_a_volume_match():
    hits = rank_documents(
        [
            _document(
                "Other Series", volumes=(SearchVolume("v1", "Nineveh", "a.cbz"),)
            ),
            _document("Nineveh Adventures"),
        ],
        ("nineveh",),
    )

    assert [hit.title for hit in hits] == ["Nineveh Adventures", "Other Series"]


def test_every_term_has_to_match_somewhere():
    documents = [_document("Iron Blade"), _document("Iron Garden")]

    assert [hit.title for hit in rank_documents(documents, ("iron", "blade"))] == [
        "Iron Blade"
    ]
    assert rank_documents(documents, ("iron", "absent")) == []


def test_a_whole_phrase_in_the_title_outranks_scattered_words():
    hits = rank_documents(
        [_document("Blade of Iron"), _document("Iron Blade")], ("iron", "blade")
    )

    assert hits[0].title == "Iron Blade"


def test_a_prefix_matches_but_scores_below_a_whole_word():
    [prefix] = rank_documents([_document("Adventures")], ("adv",))
    [whole] = rank_documents([_document("Adventures")], ("adventures",))

    assert 0 < prefix.score < whole.score


def test_matching_explains_itself():
    document = _document(
        "Ragna Crimson",
        creators=("Daiki Kobayashi",),
        tags=("Adventure",),
        description="A dragon hunter.",
    )

    [hit] = rank_documents([document], ("kobayashi",))

    assert hit.reasons == ("Creator “Daiki Kobayashi”",)


def test_matching_volumes_are_listed_even_when_the_title_matched():
    """Opening the right volume beats opening the series and looking."""
    document = _document(
        "Nineveh Adventures",
        volumes=(
            SearchVolume("v1", "The Gates of Nineveh", "one.cbz"),
            SearchVolume("v2", "Elsewhere", "two.cbz"),
        ),
    )

    [hit] = rank_documents([document], ("nineveh",))

    assert hit.reasons[0].startswith("Local title")
    assert [match.volume.id for match in hit.volumes] == ["v1"]


def test_an_empty_query_keeps_everything_in_title_order():
    hits = rank_documents([_document("Beta"), _document("alpha")], ())

    assert [hit.title for hit in hits] == ["alpha", "Beta"]
    assert all(hit.score == 0 for hit in hits)


def test_sorting_by_title_and_by_recency_ignores_the_score():
    old = _document("Zeta", newest_modified_ns=1)
    new = _document("Alpha", newest_modified_ns=9)
    hits = rank_documents([old, new], ())

    assert [hit.title for hit in sort_hits(hits, "title")] == ["Alpha", "Zeta"]
    assert [hit.title for hit in sort_hits(hits, "recent")] == ["Alpha", "Zeta"]
    assert [hit.title for hit in sort_hits(list(reversed(hits)), "recent")] == [
        "Alpha",
        "Zeta",
    ]


# --- Spelling correction ----------------------------------------------------


def test_a_close_misspelling_is_corrected_against_the_index_vocabulary():
    assert correct_terms(("ninevh",), ["nineveh", "adventures"]) == ("nineveh",)
    assert correct_terms(("nineveh",), ["nineveh"]) == ("nineveh",)


def test_a_term_that_resembles_nothing_is_left_alone():
    assert correct_terms(("qqqqzz",), ["nineveh"]) == ("qqqqzz",)


def test_short_terms_are_never_corrected():
    """Three letters are too few to guess at without inventing a query."""
    assert correct_terms(("abc",), ["abd", "xyz"]) == ("abc",)


def test_correction_needs_a_vocabulary():
    assert correct_terms(("ninevh",), []) == ("ninevh",)


# --- Filter parsing ---------------------------------------------------------


def test_filters_collect_repeated_values_and_drop_unknown_ones():
    filters = filters_from_params(
        {
            "library": ["a", "b", "a"],
            "category": ["comics", "audiobooks"],
            "collection": ["private", "nonsense"],
            "reading": ["in-progress"],
            "tag": [" adventure ", ""],
            "sort": ["nonsense"],
        }
    )

    assert filters.library_ids == ("a", "b")
    assert filters.categories == ("comics",)
    assert filters.collections == ("private",)
    assert filters.reading_state == "in-progress"
    assert filters.tags == ("adventure",)
    assert filters.sort == "relevance"
    assert filters.active


def test_no_parameters_means_no_filters():
    assert not filters_from_params({}).active


# --- The service over a stub index ------------------------------------------


class _StubIndex:
    """Just enough repository for the service, with no database behind it."""

    def __init__(self, documents, vocabulary=()):
        self.documents = list(documents)
        self.vocabulary = list(vocabulary)
        self.calls: list[tuple[str, ...]] = []

    def search_candidates(self, terms, *, filters, scope, user_id, limit):
        self.calls.append(terms)
        matching = rank_documents(self.documents, terms)
        return [hit.document for hit in matching][:limit], len(matching)

    def search_facets(self, terms, *, filters, scope, user_id):
        return SearchFacets()

    def search_vocabulary(self):
        return self.vocabulary

    def search_suggestions(self, terms, *, scope, limit=8):
        return []


def _service(documents, vocabulary=(), page_size=2) -> CatalogSearchService:
    return CatalogSearchService(_StubIndex(documents, vocabulary), page_size=page_size)


def _search(service, query, **kwargs):
    return service.search(
        query, scope=ReadScope(unrestricted=True), user_id="u", **kwargs
    )


def test_a_query_with_no_terms_and_no_filters_returns_nothing():
    page = _search(_service([_document("Anything")]), "   ")

    assert (page.total, page.page_count) == (0, 1)


def test_filters_alone_still_browse():
    service = _service([_document("Anything")])

    page = _search(service, "", filters=SearchFilters(categories=("comics",)))

    assert page.total == 1


def test_results_are_paged_and_an_overshooting_page_clamps():
    service = _service([_document(name) for name in ("A", "B", "C")], page_size=2)

    first = _search(service, "", filters=SearchFilters(categories=("comics",)))
    last = _search(service, "", filters=SearchFilters(categories=("comics",)), page=99)

    assert (first.page, first.page_count, len(first.results)) == (1, 2, 2)
    assert (last.page, len(last.results)) == (2, 1)


def test_a_misspelled_query_is_retried_once_against_the_vocabulary():
    index = _StubIndex([_document("Nineveh Adventures")], ["nineveh", "adventures"])
    service = CatalogSearchService(index)

    page = service.search("ninevh", scope=ReadScope(unrestricted=True), user_id="u")

    assert page.total == 1
    assert index.calls == [("ninevh",), ("nineveh",)]


def test_a_query_that_already_matched_is_not_second_guessed():
    index = _StubIndex([_document("Nineveh Adventures")], ["nineveh"])
    service = CatalogSearchService(index)

    service.search("nineveh", scope=ReadScope(unrestricted=True), user_id="u")

    assert index.calls == [("nineveh",)]


# --- Retrieval, authorization and facets ------------------------------------


@pytest.fixture
def searchable(tmp_path: Path):
    """Two libraries on two mounts, one of them private."""
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    for root, library, category, series in (
        (primary, "Main Library", "comics", "Example Series"),
        (secondary, "Archive", "manga", "Other Series"),
    ):
        archive = root / library / category / series / "Issue 1.cbz"
        archive.parent.mkdir(parents=True)
        write_cbz(archive)
    settings = Settings(
        data_dir=primary,
        state_dir=tmp_path / "state",
        secure_cookies=False,
        scan_interval_seconds=0,
        bootstrap_admin_username="admin",
        bootstrap_admin_password=ADMIN_PASSWORD,
    )
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    graph.libraries.add("Archive", mount.id)
    graph.scanner(settings).scan()
    return settings, graph


def _admin_search(graph, query, **kwargs):
    service = CatalogSearchService(graph.repository)
    return service.search(
        query, scope=ReadScope(unrestricted=True), user_id="admin", **kwargs
    )


def test_a_document_carries_its_volumes_titles_and_metadata(searchable):
    _, graph = searchable
    [series] = [
        item
        for item in graph.repository.catalog_series(visibility=CatalogVisibility.ALL)
        if item.name == "Example Series"
    ]
    graph.repository.save_series_metadata(
        series.id,
        12,
        "https://example.test/12",
        {
            "title": "The Alchemist",
            "alternative_titles": ["Alchemy Tales"],
            "authors": ["Smith, John"],
            "artists": ["Jane Doe"],
            "publishers": ["Nineveh Press"],
            "tags": ["Adventure"],
            "status": "completed",
            "published_start": "2020-03-01",
        },
        {},
        None,
    )

    [hit] = _admin_search(graph, "Alchemy").results

    assert hit.title == "The Alchemist"
    assert hit.document.creators == ("Smith, John", "Jane Doe")
    assert hit.document.year == "2020"
    assert [volume.title for volume in hit.document.volumes] == ["The First Issue"]


def test_the_index_follows_a_metadata_edit(searchable):
    _, graph = searchable
    [series] = [
        item
        for item in graph.repository.catalog_series(visibility=CatalogVisibility.ALL)
        if item.name == "Example Series"
    ]
    graph.repository.save_series_metadata(
        series.id, 12, "https://example.test/12", {"title": "First Name"}, {}, None
    )
    assert _admin_search(graph, "First Name").total == 1

    graph.repository.replace_metadata_overrides(series.id, {"title": "Second Name"})

    assert _admin_search(graph, "Second Name").total == 1
    graph.repository.delete_series_metadata(series.id)
    assert _admin_search(graph, "Second Name").total == 0


def test_a_removed_volume_leaves_the_index(searchable):
    settings, graph = searchable
    assert _admin_search(graph, "Example Series").total == 1

    (settings.data_dir / "Main Library" / "comics" / "Example Series").rename(
        settings.data_dir / "Main Library" / "comics" / "gone"
    )
    graph.scanner(settings).scan()

    assert _admin_search(graph, "Example Series").total == 0


def test_search_never_shows_what_browsing_would_not(searchable):
    _, graph = searchable
    reader = graph.repository.create_user("reader", "hash", False)
    scope = ReadScope(user_id=reader.id)
    service = CatalogSearchService(graph.repository)

    assert service.search("Issue", scope=scope, user_id=reader.id).total == 0

    [main] = [
        item
        for item in graph.repository.managed_libraries()
        if item.name == "Main Library"
    ]
    graph.repository.replace_access_grants(reader.id, [AccessGrant(reader.id, main.id)])
    page = service.search("Issue", scope=scope, user_id=reader.id)
    assert [hit.series.library for hit in page.results] == ["Main Library"]


def test_a_disconnected_mount_disappears_from_search(searchable):
    _, graph = searchable
    assert _admin_search(graph, "Issue").total == 2
    [archive] = [
        item for item in graph.repository.data_mounts() if item.name == "Archive drive"
    ]

    graph.mounts.disconnect(archive.id)

    assert _admin_search(graph, "Issue").total == 1


def test_facet_counts_are_narrowed_by_every_other_filter(searchable):
    """A count is a promise: choosing that value returns exactly that many.

    Counting against the unfiltered query instead makes the rail advertise
    combinations that lead to an empty page.
    """
    _, graph = searchable
    [other] = [
        item
        for item in graph.repository.catalog_series(visibility=CatalogVisibility.ALL)
        if item.name == "Other Series"
    ]
    graph.repository.set_series_private(other.id, True)

    unfiltered = _admin_search(graph, "Issue")
    assert {facet.label: facet.count for facet in unfiltered.facets.libraries} == {
        "Main Library": 1,
        "Archive": 1,
    }

    private_only = _admin_search(
        graph, "Issue", filters=SearchFilters(collections=("private",))
    )

    assert private_only.total == 1
    assert {facet.label: facet.count for facet in private_only.facets.libraries} == {
        "Archive": 1
    }, "Main Library holds nothing private, so it must not be offered"


def test_every_offered_facet_leads_to_that_many_results(searchable):
    """The property the count claims, checked by following it."""
    _, graph = searchable
    page = _admin_search(graph, "Issue")

    for facet in page.facets.libraries:
        followed = _admin_search(
            graph, "Issue", filters=SearchFilters(library_ids=(facet.value,))
        )
        assert followed.total == facet.count, facet.label


def test_a_facet_family_does_not_narrow_itself(searchable):
    """Choosing one library still shows the others, or you could never switch."""
    _, graph = searchable
    [main] = [
        item
        for item in graph.repository.managed_libraries()
        if item.name == "Main Library"
    ]

    page = _admin_search(graph, "Issue", filters=SearchFilters(library_ids=(main.id,)))

    assert page.total == 1
    assert {facet.label for facet in page.facets.libraries} == {
        "Main Library",
        "Archive",
    }


def test_reading_state_filters_and_is_reported(searchable):
    _, graph = searchable
    reader = graph.repository.create_user("reader", "hash", False)
    scope = ReadScope(unrestricted=True)
    service = CatalogSearchService(graph.repository)
    [publication] = [
        item
        for item in graph.repository.publications(limit=10)[0]
        if item.library == "Main Library"
    ]

    def total(state):
        return service.search(
            "Issue",
            scope=scope,
            user_id=reader.id,
            filters=SearchFilters(reading_state=state),
        ).total

    assert total("unread") == 2
    graph.repository.save_reading_progress(reader.id, publication.id, 1, None, False)
    assert (total("unread"), total("in-progress"), total("completed")) == (1, 1, 0)

    graph.repository.save_reading_progress(
        reader.id, publication.id, publication.page_count, None, True
    )
    assert (total("in-progress"), total("completed")) == (0, 1)


def test_a_typo_still_finds_the_series_through_the_real_index(searchable):
    _, graph = searchable

    assert _admin_search(graph, "Exampel Series").total == 1


@pytest.mark.parametrize(
    "query", ['"', 'a"b', "NEAR", "foo*", "foo OR bar", "(", "^", "a AND", "-x", "co:l"]
)
def test_full_text_operators_in_a_query_are_just_text(searchable, query):
    _, graph = searchable
    assert _admin_search(graph, query).total == 0


# --- HTTP surfaces ----------------------------------------------------------


def _login(client: TestClient) -> None:
    client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )


def test_the_search_page_renders_facets_reasons_and_volume_links(searchable):
    settings, _ = searchable
    for client in scanned_client(settings):
        _login(client)

        page = client.get("/search?q=Issue").text

        assert "Search your collection" in page
        assert 'name="library"' in page
        assert "Matched" in page
        assert 'class="matching-volumes"' in page
        assert 'href="/read/' in page
        assert 'name="reading" value="unread"' in page
        break


def test_the_search_page_needs_a_session(searchable):
    settings, _ = searchable
    for client in scanned_client(settings):
        response = client.get("/search?q=Issue", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        break


def test_an_empty_search_page_explains_itself(searchable):
    settings, _ = searchable
    for client in scanned_client(settings):
        _login(client)
        assert "Search from anywhere" in client.get("/search").text
        assert "No matches" in client.get("/search?q=zzzzqqqq").text
        break


def test_the_search_api_ranks_and_explains(searchable):
    settings, _ = searchable
    for client in scanned_client(settings):
        body = client.get("/api/v1/search?q=Issue", headers=authorization()).json()

        assert body["total"] == 2
        assert body["results"][0]["matchReasons"]
        assert body["results"][0]["readState"] == "unread"
        assert {facet["label"] for facet in body["facets"]["libraries"]} == {
            "Main Library",
            "Archive",
        }
        break


def test_the_search_api_applies_filters_from_repeated_parameters(searchable):
    settings, _ = searchable
    for client in scanned_client(settings):
        headers = authorization()
        libraries = client.get("/api/v1/search?q=Issue", headers=headers).json()[
            "facets"
        ]["libraries"]
        chosen = next(item for item in libraries if item["label"] == "Archive")

        body = client.get(
            f"/api/v1/search?q=Issue&library={chosen['value']}", headers=headers
        ).json()

        assert body["total"] == chosen["count"]
        assert [hit["series"]["library"] for hit in body["results"]] == ["Archive"]
        break


def test_suggestions_are_scoped_and_need_two_characters(searchable):
    settings, _ = searchable
    for client in scanned_client(settings):
        headers = authorization()

        found = client.get("/api/v1/search/suggestions?q=Exam", headers=headers)
        assert found.json()["suggestions"][0]["title"] == "Example Series"
        assert (
            client.get("/api/v1/search/suggestions?q=E", headers=headers).status_code
            == 422
        )
        break


def test_the_shortcut_and_suggestion_script_ships_with_the_page(searchable):
    settings, _ = searchable
    for client in scanned_client(settings):
        _login(client)
        assert "search.js" in client.get("/search").text
        script = client.get("/static/search.js")
        assert script.status_code == 200
        assert "AbortController" in script.text
        break


# --- Remaining edges --------------------------------------------------------


def test_a_substring_inside_a_word_matches_at_a_discount():
    """ "logy" is in "Anthology" without starting a word there."""
    [hit] = rank_documents([_document("Anthology")], ("logy",))
    [prefix] = rank_documents([_document("Anthology")], ("anth",))

    assert 0 < hit.score < prefix.score


class _SuggestingIndex(_StubIndex):
    def __init__(self, answers, vocabulary):
        super().__init__([], vocabulary)
        self.answers = answers
        self.asked: list[tuple[str, ...]] = []

    def search_suggestions(self, terms, *, scope, limit=8):
        self.asked.append(terms)
        return self.answers.get(terms, [])


def test_suggestions_retry_a_misspelling_once():
    index = _SuggestingIndex({("nineveh",): ["a suggestion"]}, ["nineveh"])

    found = CatalogSearchService(index).suggestions(
        "ninevh", scope=ReadScope(unrestricted=True)
    )

    assert found == ["a suggestion"]
    assert index.asked == [("ninevh",), ("nineveh",)]


def test_suggestions_give_up_when_correction_changes_nothing():
    index = _SuggestingIndex({}, ["unrelated"])

    assert (
        CatalogSearchService(index).suggestions(
            "qqqqzz", scope=ReadScope(unrestricted=True)
        )
        == []
    )
    assert index.asked == [("qqqqzz",)]


def test_suggestions_need_something_to_go_on():
    index = _SuggestingIndex({}, [])
    service = CatalogSearchService(index)
    scope = ReadScope(unrestricted=True)

    assert service.suggestions("  ", scope=scope) == []
    assert service.suggestions("!", scope=scope) == []
    assert index.asked == []
