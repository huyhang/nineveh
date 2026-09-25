from __future__ import annotations

import zipfile
from io import BytesIO

from conftest import ADMIN_PASSWORD, authorization
from fastapi.testclient import TestClient


def test_every_catalog_endpoint_requires_authentication(client: TestClient):
    for path in (
        "/opds/v2/catalog.json",
        "/opds/v2/publications.json",
        "/opds/v2/private.json",
        "/opds/v2/navigation.json?library=Main%20Library",
        "/api/v1/auth/me",
    ):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"].startswith("Basic")


def test_authentication_document_is_public(client: TestClient):
    response = client.get("/opds/v2/authentication.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/opds-authentication+json"
    )


def test_bad_credentials_are_rejected(client: TestClient):
    assert (
        client.get(
            "/opds/v2/catalog.json", headers=authorization(password="wrong")
        ).status_code
        == 401
    )


def test_opds_navigation_walks_library_then_category(client: TestClient):
    catalog = client.get("/opds/v2/catalog.json", headers=authorization()).json()
    assert catalog["navigation"][0]["title"] == "Main Library"

    library = client.get(
        catalog["navigation"][0]["href"], headers=authorization()
    ).json()
    assert [entry["title"] for entry in library["navigation"]] == ["comics"]

    category = client.get(
        library["navigation"][0]["href"], headers=authorization()
    ).json()
    assert [entry["title"] for entry in category["navigation"]] == ["Example Series"]

    series = client.get(category["navigation"][0]["href"], headers=authorization())
    assert series.status_code == 200
    assert series.json()["metadata"]["numberOfItems"] == 1


def test_private_series_use_the_separate_opds_hierarchy(client: TestClient):
    [series] = client.app.state.container.repository.catalog_series()
    before = client.get("/opds/v2/catalog.json", headers=authorization()).json()[
        "metadata"
    ]["modified"]

    updated = client.put(
        f"/api/v1/series/{series.id}/privacy",
        headers=authorization(),
        json={"private": True},
    )

    assert updated.status_code == 200
    assert updated.json()["isPrivate"] is True
    catalog = client.get("/opds/v2/catalog.json", headers=authorization()).json()
    assert catalog["metadata"]["modified"] != before
    assert [item["title"] for item in catalog["navigation"]] == ["Private Collection"]
    private_root = client.get(
        catalog["navigation"][0]["href"], headers=authorization()
    ).json()
    assert [item["title"] for item in private_root["navigation"]] == ["Main Library"]
    library = client.get(
        private_root["navigation"][0]["href"], headers=authorization()
    ).json()
    category = client.get(
        library["navigation"][0]["href"], headers=authorization()
    ).json()
    feed = client.get(category["navigation"][0]["href"], headers=authorization())
    assert feed.json()["metadata"]["numberOfItems"] == 1
    assert (
        client.get("/opds/v2/publications.json", headers=authorization()).json()[
            "publications"
        ]
        == []
    )


def test_a_browser_session_needs_a_csrf_token_to_change_privacy(client: TestClient):
    [series] = client.app.state.container.repository.catalog_series()
    url = f"/api/v1/series/{series.id}/privacy"
    client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )

    assert client.put(url, json={"private": True}).status_code == 403
    assert not client.app.state.container.repository.catalog_series_by_id(
        series.id
    ).is_private

    token = client.get("/").text.split('name="csrf_token" value="')[1].split('"')[0]
    updated = client.put(url, headers={"X-CSRF-Token": token}, json={"private": True})
    assert updated.status_code == 200
    assert updated.json()["isPrivate"] is True


def test_only_an_administrator_can_change_privacy(client: TestClient, reader: dict):
    [series] = client.app.state.container.repository.catalog_series()
    url = f"/api/v1/series/{series.id}/privacy"

    refused = client.put(url, headers=reader, json={"private": True})
    assert refused.status_code == 403
    assert not client.app.state.container.repository.catalog_series_by_id(
        series.id
    ).is_private

    client.put(url, headers=authorization(), json={"private": True}).raise_for_status()
    restored = client.put(url, headers=authorization(), json={"private": False})
    assert restored.json()["isPrivate"] is False
    missing = client.put(
        "/api/v1/series/absent/privacy", headers=authorization(), json={"private": True}
    )
    assert missing.status_code == 404


def test_search_matches_the_title(client: TestClient):
    hit = client.get("/opds/v2/publications.json?q=First", headers=authorization())
    assert hit.json()["metadata"]["numberOfItems"] == 1
    miss = client.get("/opds/v2/publications.json?q=absent", headers=authorization())
    assert miss.json()["metadata"]["numberOfItems"] == 0


def test_search_escapes_sql_wildcards(client: TestClient):
    response = client.get("/opds/v2/publications.json?q=%25", headers=authorization())
    assert response.json()["metadata"]["numberOfItems"] == 0


def test_download_is_byte_identical_and_supports_ranges(
    client: TestClient, library, publication_id: str
):
    _, archive = library
    whole = client.get(
        f"/api/v1/publications/{publication_id}/file", headers=authorization()
    )
    assert whole.status_code == 200
    assert whole.content == archive.read_bytes()

    partial = client.get(
        f"/api/v1/publications/{publication_id}/file",
        headers={**authorization(), "Range": "bytes=0-15"},
    )
    assert partial.status_code == 206
    assert partial.content == archive.read_bytes()[:16]

    cached = client.get(
        f"/api/v1/publications/{publication_id}/file",
        headers={**authorization(), "If-None-Match": whole.headers["etag"]},
    )
    assert cached.status_code == 304


def test_head_on_a_download_returns_no_body(client: TestClient, publication_id: str):
    response = client.head(
        f"/api/v1/publications/{publication_id}/file", headers=authorization()
    )
    assert response.status_code == 200
    assert response.content == b""


def test_page_manifest_reports_ordered_pages_with_dimensions(
    client: TestClient, publication_id: str
):
    manifest = client.get(
        f"/api/v1/publications/{publication_id}/pages?start=1&end=2",
        headers=authorization(),
    ).json()
    assert manifest["start"] == 1
    assert manifest["end"] == 2
    assert manifest["totalPages"] == 3
    assert [(p["width"], p["height"]) for p in manifest["pages"]] == [(40, 60)] * 2
    assert [p["spread"] for p in manifest["pages"]] == [False, False]
    assert manifest["next"].endswith("start=3&end=3")


def test_page_manifest_defaults_to_the_whole_publication(
    client: TestClient, publication_id: str
):
    manifest = client.get(
        f"/api/v1/publications/{publication_id}/pages", headers=authorization()
    ).json()
    assert (manifest["start"], manifest["end"]) == (1, 3)
    assert "next" not in manifest


def test_page_range_boundaries_are_enforced(client: TestClient, publication_id: str):
    base = f"/api/v1/publications/{publication_id}/pages"
    cases = {
        "?start=99": 416,
        "?start=1&end=99": 416,
        "?start=3&end=2": 422,
        "?start=0": 422,  # rejected by the query constraint
    }
    for query, expected in cases.items():
        assert client.get(base + query, headers=authorization()).status_code == expected


def test_pages_are_served_with_their_media_type_and_revalidate(
    client: TestClient, publication_id: str
):
    page = client.get(
        f"/api/v1/publications/{publication_id}/pages/1", headers=authorization()
    )
    assert page.status_code == 200
    assert page.headers["content-type"] == "image/png"

    revalidated = client.get(
        f"/api/v1/publications/{publication_id}/pages/1",
        headers={**authorization(), "If-None-Match": page.headers["etag"]},
    )
    assert revalidated.status_code == 304


def test_a_revision_pinned_page_is_immutable(client: TestClient, publication_id: str):
    feed = client.get("/opds/v2/publications.json", headers=authorization()).json()
    revision = feed["publications"][0]["images"][0]["href"].split("revision=")[1]
    pinned = client.get(
        f"/api/v1/publications/{publication_id}/pages/1?revision={revision}",
        headers=authorization(),
    )
    assert "immutable" in pinned.headers["cache-control"]

    stale = client.get(
        f"/api/v1/publications/{publication_id}/pages/1?revision=nope",
        headers=authorization(),
    )
    assert stale.status_code == 409


def test_streaming_path_serves_the_same_bytes(streaming_client: TestClient, library):
    feed = streaming_client.get(
        "/opds/v2/publications.json", headers=authorization()
    ).json()
    identifier = feed["publications"][0]["metadata"]["identifier"].removeprefix(
        "urn:uuid:"
    )
    page = streaming_client.get(
        f"/api/v1/publications/{identifier}/pages/1", headers=authorization()
    )
    assert page.status_code == 200
    settings, _ = library
    assert not list(settings.page_cache_dir.rglob("*.page"))


def test_missing_page_and_publication_are_404(client: TestClient, publication_id: str):
    assert (
        client.get(
            f"/api/v1/publications/{publication_id}/pages/99", headers=authorization()
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/v1/publications/nope/file", headers=authorization()
        ).status_code
        == 404
    )


def test_a_deleted_archive_stops_being_served(
    client: TestClient, library, publication_id: str
):
    _, archive = library
    archive.unlink()
    assert (
        client.get(
            f"/api/v1/publications/{publication_id}/file", headers=authorization()
        ).status_code
        == 404
    )


def test_a_changed_archive_conflicts_instead_of_disappearing(
    client: TestClient, library, publication_id: str
):
    """A rescan conflict must not be reported as a missing archive.

    `ArchiveChanged` subclasses `ArchiveUnavailable`, so measuring page
    dimensions used to downgrade it to 404 and the reader's "reload me" path
    could never run.
    """
    _, archive = library
    with archive.open("ab") as handle:
        handle.write(b"junk")

    assert (
        client.get(
            f"/api/v1/publications/{publication_id}/pages", headers=authorization()
        ).status_code
        == 409
    )


def _progress_url(publication_id: str) -> str:
    return f"/api/v1/publications/{publication_id}/progress"


def test_reading_progress_round_trips_for_an_api_client(
    client: TestClient, publication_id: str
):
    """Basic auth carries no session, so no CSRF token is involved."""
    url = _progress_url(publication_id)
    assert client.get(url, headers=authorization()).status_code == 404

    saved = client.put(
        url,
        headers=authorization(),
        json={"page": 2, "mode": "double", "completed": False},
    )
    assert saved.status_code == 200
    assert saved.json()["publicationId"] == publication_id
    assert saved.json()["page"] == 2
    assert saved.json()["mode"] == "double"

    browser_save = client.put(
        url,
        headers=authorization(),
        json={"page": 3, "completed": False},
    )
    assert browser_save.json()["page"] == 3
    assert browser_save.json()["mode"] == "double", "a save without a mode keeps it"

    fetched = client.get(url, headers=authorization())
    assert fetched.status_code == 200
    assert fetched.json() == browser_save.json()

    assert client.delete(url, headers=authorization()).status_code == 204
    assert client.get(url, headers=authorization()).status_code == 404


def test_reading_progress_validates_the_position(
    client: TestClient, publication_id: str
):
    url = _progress_url(publication_id)
    beyond = client.put(
        url, headers=authorization(), json={"page": 99, "mode": "single"}
    )
    assert beyond.status_code == 422
    assert "outside" in beyond.json()["detail"]

    early = client.put(
        url,
        headers=authorization(),
        json={"page": 1, "mode": "single", "completed": True},
    )
    assert early.status_code == 422

    for body in ({"page": 0, "mode": "single"}, {"page": 1, "mode": "sideways"}):
        assert client.put(url, headers=authorization(), json=body).status_code == 422


def test_reading_progress_is_private_and_scoped(
    client: TestClient, reader: dict, publication_id: str
):
    url = _progress_url(publication_id)
    client.put(
        url, headers=authorization(), json={"page": 2, "mode": "single"}
    ).raise_for_status()

    assert client.get(url, headers=reader).status_code == 404
    assert (
        client.put(url, headers=reader, json={"page": 1, "mode": "single"}).status_code
        == 404
    )
    assert client.delete(url, headers=reader).status_code == 404
    assert client.get(url, headers=authorization()).json()["page"] == 2


def test_reading_progress_from_a_browser_session_needs_a_csrf_token(
    client: TestClient, publication_id: str
):
    url = _progress_url(publication_id)
    client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )

    assert client.put(url, json={"page": 1, "mode": "single"}).status_code == 403
    assert client.delete(url).status_code == 403

    token = client.get("/").text.split('name="csrf_token" value="')[1].split('"')[0]
    headers = {"X-CSRF-Token": token}
    saved = client.put(url, headers=headers, json={"page": 1, "mode": "scroll"})
    assert saved.status_code == 200
    assert saved.json()["mode"] == "scroll"
    assert client.delete(url, headers=headers).status_code == 204


def test_covers_are_rendered_at_the_allowed_widths(
    client: TestClient, publication_id: str
):
    cover = client.get(
        f"/api/v1/publications/{publication_id}/cover?width=160",
        headers=authorization(),
    )
    assert cover.status_code == 200
    assert cover.headers["content-type"] == "image/webp"
    assert (
        client.get(
            f"/api/v1/publications/{publication_id}/cover?width=999",
            headers=authorization(),
        ).status_code
        == 422
    )


def test_range_download_contains_exactly_the_requested_pages(
    client: TestClient, publication_id: str
):
    response = client.get(
        f"/api/v1/publications/{publication_id}/range?start=2&end=3",
        headers=authorization(),
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.comicbook+zip"
    assert "p2-3.cbz" in response.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(response.content)) as produced:
        assert produced.namelist() == ["pages/2.png", "pages/10.png"]


def test_range_download_rejects_an_impossible_range(
    client: TestClient, publication_id: str
):
    assert (
        client.get(
            f"/api/v1/publications/{publication_id}/range?start=2&end=99",
            headers=authorization(),
        ).status_code
        == 416
    )


def test_range_download_revalidates(client: TestClient, publication_id: str):
    first = client.get(
        f"/api/v1/publications/{publication_id}/range?start=1&end=1",
        headers=authorization(),
    )
    again = client.get(
        f"/api/v1/publications/{publication_id}/range?start=1&end=1",
        headers={**authorization(), "If-None-Match": first.headers["etag"]},
    )
    assert again.status_code == 304


def test_range_downloads_leave_no_files_behind(
    client: TestClient, library, publication_id: str
):
    settings, _ = library
    client.get(
        f"/api/v1/publications/{publication_id}/range?start=1&end=3",
        headers=authorization(),
    )
    assert not list(settings.range_dir.glob("range-*.cbz"))


def test_health_endpoints_report_the_catalog(client: TestClient):
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}
    ready = client.get("/api/v1/health/ready").json()
    assert ready["status"] == "ok"
    assert ready["catalog"]["report"]["indexed"] == 1


def test_responses_carry_the_hardening_headers(client: TestClient):
    response = client.get("/api/v1/health/live")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_a_page_is_served_at_a_screen_size_until_it_is_known_to_be_small(
    client: TestClient, publication_id: str
):
    """Continuous scroll asks for a bounded width. Before the reader has
    measured a volume the server resizes on request; once dimensions are
    stored it can tell that resizing a small page would only cost bytes."""
    base = f"/api/v1/publications/{publication_id}/pages"

    plain = client.get(f"{base}/2", headers=authorization())
    sized = client.get(f"{base}/2?width=640", headers=authorization())
    assert sized.status_code == 200
    assert sized.headers["content-type"] == "image/webp"
    # Two views of the same page must not share an ETag, or a browser holding
    # the small copy would never fetch the full one.
    assert sized.headers["etag"] != plain.headers["etag"]

    # The manifest measures every page and stores the result.
    client.get(base, headers=authorization())
    settled = client.get(f"{base}/2?width=640", headers=authorization())
    assert settled.content == plain.content


def test_an_unsupported_page_width_is_refused(client: TestClient, publication_id: str):
    assert (
        client.get(
            f"/api/v1/publications/{publication_id}/pages/1?width=999",
            headers=authorization(),
        ).status_code
        == 422
    )
