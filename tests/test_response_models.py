"""A response model must send every body its builder can produce unchanged.

The reading app's JSON routes validate the builders' plain dicts on the way
out. A variant the rest of the suite never produces would surface in
production as a 500, a key that turned into null, or an integer that came
back as a float -- so each optional shape is exercised here on purpose.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import authorization
from fakes import FakeArchives, page, publication
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nineveh.app import Container, create_app
from nineveh.domain import ScannedPublication
from nineveh.http_api import ReadingPosition


class UnmeasurableArchives(FakeArchives):
    """An archive whose images yield no dimensions."""

    def page_dimensions_many(self, publication, pages) -> dict[int, tuple[int, int]]:
        return {}


def test_a_publication_without_optional_metadata_leaves_those_keys_out(
    fake_client: TestClient, fake_container: Container
):
    bare = replace(
        publication("bare-id", pages=1),
        relative_path="Lib/comics/Series/Bare.cbz",
        filename="Bare.cbz",
        number=None,
        authors=(),
    )
    fake_container.repository.upsert_publication(ScannedPublication(bare, (page(1),)))

    response = fake_client.get("/api/v1/publications/bare-id", headers=authorization())

    assert response.status_code == 200
    # The stored row, not `bare`: cataloguing is what assigns the series its id.
    stored = fake_container.repository.publication_by_id("bare-id")
    assert response.json() == fake_container.opds.publication(
        "http://testserver", stored
    )
    metadata = response.json()["metadata"]
    assert {"description", "author"}.isdisjoint(metadata)
    [series] = metadata["belongsTo"]["series"]
    assert set(series) == {"name", "identifier"}


def test_a_publication_names_the_series_detail_it_belongs_to(
    fake_client: TestClient, fake_container: Container
):
    member = replace(
        publication("member-id", pages=1),
        relative_path="Lib/comics/Series/Member.cbz",
        filename="Member.cbz",
    )
    fake_container.repository.upsert_publication(ScannedPublication(member, (page(1),)))

    response = fake_client.get(
        "/api/v1/publications/member-id", headers=authorization()
    )
    [series] = response.json()["metadata"]["belongsTo"]["series"]
    series_id = series["identifier"].removeprefix("urn:uuid:")
    detail = fake_client.get(f"/api/v1/series/{series_id}", headers=authorization())

    assert detail.status_code == 200
    assert detail.json()["localName"] == series["name"]


def test_a_fractional_volume_keeps_its_fraction(
    fake_client: TestClient, fake_container: Container
):
    half = replace(
        publication("half-id", pages=1),
        relative_path="Lib/comics/Series/Half.cbz",
        filename="Half.cbz",
        number="2.5",
    )
    fake_container.repository.upsert_publication(ScannedPublication(half, (page(1),)))

    response = fake_client.get("/api/v1/publications/half-id", headers=authorization())

    assert response.status_code == 200
    [series] = response.json()["metadata"]["belongsTo"]["series"]
    assert series["position"] == 2.5


def test_a_page_that_could_not_be_measured_is_sent_with_null_dimensions(
    fake_container: Container,
):
    container = replace(fake_container, archives=UnmeasurableArchives())
    with TestClient(create_app(container=container)) as client:
        container.repository.upsert_publication(
            ScannedPublication(publication(), tuple(page(n) for n in (1, 2, 3)))
        )
        response = client.get(
            "/api/v1/publications/fake-id/pages", headers=authorization()
        )

    assert response.status_code == 200
    manifest = response.json()
    assert [(p["width"], p["height"]) for p in manifest["pages"]] == [(None, None)] * 3
    # The whole publication fit and spread detection is off: omitted, not null.
    assert {"next", "pairingAnchor"}.isdisjoint(manifest)


PROVIDER_VALUES = {
    "title": "Exemplar",
    "alternative_titles": [],
    "authors": ["A. Writer"],
    "artists": [],
    "description": None,
    "published_start": "2019-04-01",
    "published_end": None,
    "status": "releasing",
    "content_rating": None,
    "media_type": "manga",
    "rating": 91,
    "publishers": [],
    "tags": [],
    "final_volume": 12.0,
    "total_chapters": None,
}


def test_series_metadata_keeps_its_nulls_and_its_number_types(
    fake_client: TestClient, fake_container: Container
):
    repository = fake_container.repository
    [series] = repository.catalog_series()
    repository.save_series_metadata(
        series.id, 4242, "https://mangabaka.org/4242", PROVIDER_VALUES, {}, None
    )
    repository.replace_metadata_overrides(series.id, {"total_chapters": 214})

    response = fake_client.get(f"/api/v1/series/{series.id}", headers=authorization())

    assert response.status_code == 200
    metadata = response.json()["metadata"]
    assert metadata["providerUpdatedAt"] is None
    assert metadata["editedFields"] == ["total_chapters"]
    assert metadata["values"] == {**PROVIDER_VALUES, "total_chapters": 214}
    # Equality alone would pass 91.0 == 91; a client decoding an integer would not.
    assert type(metadata["values"]["rating"]) is int
    assert type(metadata["values"]["final_volume"]) is float


def _nulls(node: object, path: str = "$"):
    if node is None:
        yield path
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _nulls(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _nulls(value, f"{path}[{index}]")


def test_no_opds_document_turns_an_absent_field_into_null(
    fake_client: TestClient, fake_container: Container
):
    """The OPDS builders leave out what they do not know. A feed route
    without `response_model_exclude_unset` would send each of those as null
    instead -- `templated` on every link -- and an OPDS client reading
    `null` where the spec allows only a boolean may refuse the feed."""
    [series] = fake_container.repository.catalog_series()
    requests = [
        ("/opds/v2/authentication.json", {}),
        ("/opds/v2/catalog.json", {}),
        ("/opds/v2/navigation.json", {"library": series.library}),
        (
            "/opds/v2/navigation.json",
            {"library": series.library, "category": series.category},
        ),
        ("/opds/v2/publications.json", {"library": series.library}),
    ]
    for path, params in requests:
        response = fake_client.get(path, params=params, headers=authorization())
        assert response.status_code == 200, (path, params)
        assert list(_nulls(response.json())) == [], (path, params)


POSITION = {
    "publicationId": "fake-id",
    "page": 3,
    "mode": "single",
    "completed": False,
    "updatedAt": "2026-01-01T00:00:00+00:00",
}


def test_a_field_the_model_does_not_declare_is_refused_rather_than_dropped():
    """So a builder that grows a field fails its tests until the model, and
    with it the published contract, grows too."""
    ReadingPosition.model_validate(POSITION)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReadingPosition.model_validate({**POSITION, "chapter": 12})


def test_a_value_of_the_wrong_type_is_refused_rather_than_converted():
    """Otherwise a builder sending "3" would pass here, as 3, and stay wrong
    with nothing pointing at it."""
    with pytest.raises(ValidationError, match="Input should be a valid integer"):
        ReadingPosition.model_validate({**POSITION, "page": "3"})
