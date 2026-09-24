from __future__ import annotations

from dataclasses import replace

import pytest
from fakes import publication

from nineveh.opds import (
    ACQUISITION_REL,
    PAGE_MANIFEST_REL,
    PAGE_RANGE_REL,
    OpdsBuilder,
    url,
)

BASE = "https://nineveh.example.com"


@pytest.fixture
def builder() -> OpdsBuilder:
    return OpdsBuilder("Nineveh")


def _rels(feed: dict) -> list[str]:
    return [link["rel"] for link in feed["links"]]


def _href(feed: dict, rel: str) -> str:
    return next(link["href"] for link in feed["links"] if link["rel"] == rel)


def test_url_omits_the_query_string_when_there_are_no_parameters():
    assert url(BASE, "/opds/v2/catalog.json", {}) == f"{BASE}/opds/v2/catalog.json"


def test_url_percent_encodes_parameters():
    encoded = url(BASE, "/opds/v2/navigation.json", {"library": "My Library & Co"})
    assert encoded.endswith("?library=My+Library+%26+Co")


def test_authentication_document_advertises_basic(builder: OpdsBuilder):
    document = builder.authentication_document(BASE)
    assert document["authentication"][0]["type"] == "http://opds-spec.org/auth/basic"
    assert document["id"].endswith("/opds/v2/authentication.json")


def test_root_feed_links_search_and_the_authentication_document(builder: OpdsBuilder):
    feed = builder.root_feed(BASE, [("Main", 4)], "2026-01-01T00:00:00+00:00")
    assert "http://opds-spec.org/auth/document" in _rels(feed)
    assert "{searchTerms}" in _href(feed, "search")
    assert feed["navigation"][0]["properties"]["numberOfItems"] == 4
    assert feed["metadata"]["numberOfItems"] == 1


def test_first_page_has_no_previous_link(builder: OpdsBuilder):
    feed = builder.publication_feed(
        BASE,
        [],
        total=50,
        page=1,
        page_size=24,
        library=None,
        category=None,
        series=None,
        query=None,
        modified="2026-01-01T00:00:00+00:00",
    )
    assert "previous" not in _rels(feed)
    assert "next" in _rels(feed)


def test_last_page_has_no_next_link(builder: OpdsBuilder):
    feed = builder.publication_feed(
        BASE,
        [],
        total=50,
        page=3,
        page_size=24,
        library=None,
        category=None,
        series=None,
        query=None,
        modified="2026-01-01T00:00:00+00:00",
    )
    assert "next" not in _rels(feed)
    assert "previous" in _rels(feed)


def test_exactly_full_last_page_has_no_next_link(builder: OpdsBuilder):
    feed = builder.publication_feed(
        BASE,
        [],
        total=48,
        page=2,
        page_size=24,
        library=None,
        category=None,
        series=None,
        query=None,
        modified="2026-01-01T00:00:00+00:00",
    )
    assert "next" not in _rels(feed)


def test_pagination_links_carry_the_active_filters(builder: OpdsBuilder):
    feed = builder.publication_feed(
        BASE,
        [],
        total=100,
        page=2,
        page_size=24,
        library="Main",
        category="manga",
        series=None,
        query="lion",
        modified="2026-01-01T00:00:00+00:00",
    )
    next_url = _href(feed, "next")
    assert "library=Main" in next_url
    assert "category=manga" in next_url
    assert "q=lion" in next_url
    assert "page=3" in next_url
    assert "series" not in next_url
    assert feed["metadata"]["title"] == "Nineveh — Main — manga"


def test_publication_exposes_acquisition_manifest_and_range_links(
    builder: OpdsBuilder,
):
    entry = builder.publication(BASE, publication())
    rels = {link["rel"]: link for link in entry["links"]}
    assert rels[ACQUISITION_REL]["href"].endswith("/file")
    assert rels[ACQUISITION_REL]["properties"]["length"] == 4096
    assert rels[PAGE_MANIFEST_REL]["href"].endswith("/pages")
    assert rels[PAGE_RANGE_REL]["templated"] is True
    assert "{?start,end}" in rels[PAGE_RANGE_REL]["href"]


def test_publication_metadata_omits_absent_optional_fields(builder: OpdsBuilder):
    entry = builder.publication(BASE, publication())
    metadata = entry["metadata"]
    assert metadata["author"] == [{"name": "A. Writer"}]
    assert "description" not in metadata
    assert metadata["belongsTo"]["series"] == [{"name": "Series", "position": 1}]


def test_a_catalogued_series_is_identified_by_the_id_its_detail_route_takes(
    builder: OpdsBuilder,
):
    item = replace(publication(), series_id="0b6c1e9e-5a4f-4f0e-9a51-3c1d2b7e8f90")
    [series] = builder.publication(BASE, item)["metadata"]["belongsTo"]["series"]
    assert series == {
        "name": "Series",
        "identifier": "urn:uuid:0b6c1e9e-5a4f-4f0e-9a51-3c1d2b7e8f90",
        "position": 1,
    }


@pytest.mark.parametrize(
    ("number", "position"),
    [
        ("007", 7),
        ("1.5", 1.5),
        ("2.50", 2.5),
        ("3.0", 3),
        (" 4 ", 4),
        ("0", 0),
        ("-1", -1),
    ],
)
def test_a_volume_number_becomes_a_numeric_series_position(
    builder: OpdsBuilder, number: str, position: float
):
    """OPDS 2.0 places a volume's position under `belongsTo.series` as a
    number; a string, or a top-level `position`, is ignored by OPDS readers."""
    item = replace(publication(), number=number)
    metadata = builder.publication(BASE, item)["metadata"]
    [series] = metadata["belongsTo"]["series"]
    assert series["position"] == position
    assert type(series["position"]) is type(position)
    assert "position" not in metadata


@pytest.mark.parametrize("number", ["12a", "Special", "1-2", "", None])
def test_a_volume_number_nothing_can_order_leaves_the_position_out(
    builder: OpdsBuilder, number: str | None
):
    item = replace(publication(), number=number)
    metadata = builder.publication(BASE, item)["metadata"]
    assert metadata["belongsTo"]["series"] == [{"name": "Series"}]


def test_navigation_feed_titles_include_the_service_name(builder: OpdsBuilder):
    feed = builder.navigation_feed(
        BASE,
        title="Main",
        parameters={"library": "Main"},
        entries=[("comics", 2, f"{BASE}/x")],
        modified="2026-01-01T00:00:00+00:00",
    )
    assert feed["metadata"]["title"] == "Nineveh — Main"
    assert _rels(feed) == ["self", "start"]
