"""Choosing a series' cover: its artwork, or its first volume's cover."""

from __future__ import annotations

import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import ADMIN_PASSWORD, READER_PASSWORD, authorization, image_bytes
from fastapi.testclient import TestClient

from nineveh.app import create_app
from nineveh.config import Settings
from nineveh.database import SQLiteRepository
from nineveh.domain import Publication, SeriesCover
from nineveh.ordering import publication_order_key, publication_sort_key

# Volume numbers only in the file names, so reading order comes from them:
# volume 10 sorts before volume 2 as text, after it naturally.
VOLUMES = {"Alchemy v2.cbz": (200, 0, 0), "Alchemy v3.cbz": (0, 200, 0)}
VOLUMES["Alchemy v10.cbz"] = (0, 0, 200)


def _write_volume(path: Path, color: tuple[int, int, int]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", image_bytes(color, (60, 90)))
        archive.writestr("002.png", image_bytes((9, 9, 9), (60, 90)))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    manga = tmp_path / "data" / "Shelf" / "manga" / "Alchemy"
    manga.mkdir(parents=True)
    for name, color in VOLUMES.items():
        _write_volume(manga / name, color)
    comics = tmp_path / "data" / "Shelf" / "comics" / "Strips"
    comics.mkdir(parents=True)
    _write_volume(comics / "Strips 1.cbz", (50, 50, 50))
    return Settings(
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        secure_cookies=False,
        scan_interval_seconds=0,
        bootstrap_admin_password=ADMIN_PASSWORD,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        for _ in range(300):
            task = client.app.state.scan_task
            if task is not None and task.done():
                break
            time.sleep(0.01)
        yield client


def _series(client: TestClient, category: str = "manga"):
    repository = client.app.state.container.repository
    return repository.catalog_series(category=category, visibility="all")[0]


def _cover(client: TestClient, series_id: str) -> bytes:
    response = client.get(f"/api/v1/series/{series_id}/cover", headers=authorization())
    assert response.status_code == 200
    return response.content


def _volume_cover(client: TestClient, publication_id: str) -> bytes:
    response = client.get(
        f"/api/v1/publications/{publication_id}/cover?width=640",
        headers=authorization(),
    )
    return response.content


def _publication(client: TestClient, filename: str) -> str:
    repository = client.app.state.container.repository
    series = _series(client)
    return next(
        item.id
        for item in repository.publications_in_series(series.id)
        if item.filename == filename
    )


def _upload_artwork(client: TestClient, series_id: str) -> bytes:
    covers = client.app.state.container.metadata.covers
    covers.save_custom(series_id, image_bytes((255, 255, 0), (300, 450)))
    return covers.cover(series_id).read_bytes()


def _csrf(client: TestClient, path: str) -> str:
    return client.get(path).text.split('name="csrf_token" value="')[1].split('"')[0]


def _sign_in(client: TestClient) -> None:
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})


# --- Which volume is first -----------------------------------------------------


def test_the_series_cover_comes_from_the_first_volume_in_reading_order(client):
    """Not from volume 10, which merely sorts first as text."""
    series = _series(client)
    assert series.first_publication_id == _publication(client, "Alchemy v2.cbz")
    assert _cover(client, series.id) == _volume_cover(
        client, series.first_publication_id
    )


NAMES = [
    ("1", "Vol 1", "a.cbz"),
    ("10", "Vol 10", "b.cbz"),
    ("2", "Vol 2", "c.cbz"),
    (None, "Extra", "Extra.cbz"),
    (None, "Omake 2", "Omake 2.cbz"),
    (None, "Omake 10", "Omake 10.cbz"),
    ("007", "Seven", "7.cbz"),
    ("1.5", "Half", "h.cbz"),
    (None, "Vol 1", "v1 a.cbz"),
    (None, "Vol 1", "v1.cbz"),
    (None, "vol 1 ", "v1-.cbz"),
    (None, "Ω special", "o.cbz"),
    ("2", "Vol 2", "c2.cbz"),
    (None, "", "untitled 3.cbz"),
]


def test_the_stored_key_sorts_exactly_like_the_series_page():
    publications = [
        Publication(
            id=f"id-{index:02}",
            relative_path=filename,
            library="Shelf",
            category="manga",
            series="Alchemy",
            filename=filename,
            title=title,
            number=number,
            description=None,
            authors=(),
            modified_ns=0,
            size=0,
            revision="r",
            page_count=1,
            cover_page=1,
        )
        for index, (number, title, filename) in enumerate(NAMES)
    ]
    by_page = sorted(publications, key=publication_order_key)
    by_key = sorted(
        publications,
        key=lambda item: publication_sort_key(
            item.number, item.title, item.filename, item.id
        ),
    )
    assert [item.id for item in by_key] == [item.id for item in by_page]


def test_an_upgraded_database_learns_reading_order(settings: Settings, client):
    """Rows indexed before the key existed get one on the next start."""
    repository = client.app.state.container.repository
    with repository._connect() as connection:
        connection.execute("UPDATE publications SET sort_key = NULL")
    SQLiteRepository(settings.database_path).initialize()
    assert _series(client).first_publication_id == _publication(
        client, "Alchemy v2.cbz"
    )


# --- The choice ------------------------------------------------------------------


def test_artwork_is_shown_by_default(client):
    series = _series(client)
    artwork = _upload_artwork(client, series.id)
    assert series.effective_cover is SeriesCover.ARTWORK
    assert _cover(client, series.id) == artwork


def test_a_library_can_switch_every_series_to_its_first_volume(client):
    series = _series(client)
    _upload_artwork(client, series.id)
    client.app.state.container.series.set_library_cover(
        series.library_id, SeriesCover.FIRST_VOLUME
    )
    assert _cover(client, series.id) == _volume_cover(
        client, series.first_publication_id
    )


def test_one_series_can_keep_its_artwork_in_a_first_volume_library(client):
    series = _series(client)
    artwork = _upload_artwork(client, series.id)
    service = client.app.state.container.series
    service.set_library_cover(series.library_id, SeriesCover.FIRST_VOLUME)
    service.set_cover(series.id, SeriesCover.ARTWORK)
    assert _cover(client, series.id) == artwork
    service.set_cover(series.id, None)  # back to the library's choice
    assert _cover(client, series.id) == _volume_cover(
        client, series.first_publication_id
    )


def test_one_series_can_use_its_first_volume_in_an_artwork_library(client):
    series = _series(client)
    _upload_artwork(client, series.id)
    client.app.state.container.series.set_cover(series.id, SeriesCover.FIRST_VOLUME)
    assert _cover(client, series.id) == _volume_cover(
        client, series.first_publication_id
    )


def test_a_refreshed_mangabaka_cover_does_not_undo_the_choice(client):
    series = _series(client)
    client.app.state.container.series.set_cover(series.id, SeriesCover.FIRST_VOLUME)
    covers = client.app.state.container.metadata.covers
    covers.save_provider(series.id, image_bytes((1, 2, 3), (300, 450)))
    assert _cover(client, series.id) == _volume_cover(
        client, series.first_publication_id
    )


def test_a_rescan_keeps_the_choices(client):
    series = _series(client)
    service = client.app.state.container.series
    service.set_library_cover(series.library_id, SeriesCover.FIRST_VOLUME)
    service.set_cover(series.id, SeriesCover.ARTWORK)
    client.app.state.container.scanner.scan()
    rescanned = _series(client)
    assert (rescanned.cover, rescanned.library_cover) == (
        SeriesCover.ARTWORK,
        SeriesCover.FIRST_VOLUME,
    )


def test_the_cover_revalidates_when_the_choice_changes(client):
    series = _series(client)
    _upload_artwork(client, series.id)
    url = f"/api/v1/series/{series.id}/cover"
    etag = client.get(url, headers=authorization()).headers["etag"]
    client.app.state.container.series.set_cover(series.id, SeriesCover.FIRST_VOLUME)
    changed = client.get(url, headers={**authorization(), "If-None-Match": etag})
    assert changed.status_code == 200


# --- Browser controls -------------------------------------------------------------


def test_an_administrator_chooses_a_series_cover_from_its_page(client):
    series = _series(client)
    _sign_in(client)
    page = client.get(f"/series/{series.id}").text
    assert 'action="/series/' + series.id + '/cover-source"' in page
    response = client.post(
        f"/series/{series.id}/cover-source",
        data={
            "source": "first_volume",
            "csrf_token": _csrf(client, f"/series/{series.id}"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/series/{series.id}"
    assert _series(client).cover is SeriesCover.FIRST_VOLUME
    assert (
        "now uses its first volume&#39;s cover"
        in client.get(f"/series/{series.id}").text
    )


def test_the_metadata_page_offers_the_choice_and_returns_to_itself(client):
    series = _series(client)
    _sign_in(client)
    path = f"/series/{series.id}/metadata"
    assert "/cover-source" in client.get(path).text
    response = client.post(
        f"/series/{series.id}/cover-source",
        data={
            "source": "library",
            "back": "metadata",
            "csrf_token": _csrf(client, path),
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == path
    assert _series(client).cover is None


def test_comics_and_readers_are_not_offered_the_choice(client):
    comics = _series(client, "comics")
    manga = _series(client)
    _sign_in(client)
    assert "/cover-source" not in client.get(f"/series/{comics.id}").text
    client.post(
        "/api/v1/admin/users",
        headers=authorization(),
        json={"username": "reader", "password": READER_PASSWORD},
    )
    reader = TestClient(client.app)
    reader.post("/login", data={"username": "reader", "password": READER_PASSWORD})
    assert "/cover-source" not in reader.get(f"/series/{manga.id}").text


def test_an_administrator_sets_a_library_default_from_admin_libraries(client):
    series = _series(client)
    _sign_in(client)
    page = client.get("/admin/libraries").text
    assert f"/admin/libraries/{series.library_id}/series-cover" in page
    response = client.post(
        f"/admin/libraries/{series.library_id}/series-cover",
        data={
            "source": "first_volume",
            "csrf_token": _csrf(client, "/admin/libraries"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _series(client).library_cover is SeriesCover.FIRST_VOLUME
    assert (
        "now use their first volume&#39;s cover" in client.get("/admin/libraries").text
    )


def test_an_unknown_choice_is_refused(client):
    series = _series(client)
    _sign_in(client)
    response = client.post(
        f"/series/{series.id}/cover-source",
        data={"source": "random", "csrf_token": _csrf(client, f"/series/{series.id}")},
    )
    assert response.status_code == 422


def test_an_uploaded_cover_survives_switching_the_library_later(client):
    series = _series(client)
    _sign_in(client)
    path = f"/series/{series.id}/metadata"
    client.post(
        f"/series/{series.id}/metadata/cover",
        data={"csrf_token": _csrf(client, path)},
        files={"cover": ("c.png", image_bytes((255, 0, 255), (300, 450)), "image/png")},
    )
    artwork = client.app.state.container.metadata.covers.cover(series.id).read_bytes()
    client.app.state.container.series.set_library_cover(
        series.library_id, SeriesCover.FIRST_VOLUME
    )
    assert _cover(client, series.id) == artwork


def test_removing_the_upload_returns_the_series_to_its_library(client):
    series = _series(client)
    _upload_artwork(client, series.id)
    service = client.app.state.container.series
    service.set_cover(series.id, SeriesCover.ARTWORK)
    service.set_library_cover(series.library_id, SeriesCover.FIRST_VOLUME)
    _sign_in(client)
    path = f"/series/{series.id}/metadata"
    client.post(
        f"/series/{series.id}/metadata/cover/remove",
        data={"csrf_token": _csrf(client, path)},
    )
    assert _series(client).cover is None
    assert _cover(client, series.id) == _volume_cover(
        client, series.first_publication_id
    )


def test_uploading_artwork_shows_it_even_in_a_first_volume_library(client):
    series = _series(client)
    client.app.state.container.series.set_library_cover(
        series.library_id, SeriesCover.FIRST_VOLUME
    )
    _sign_in(client)
    path = f"/series/{series.id}/metadata"
    response = client.post(
        f"/series/{series.id}/metadata/cover",
        data={"csrf_token": _csrf(client, path)},
        files={"cover": ("c.png", image_bytes((255, 0, 255), (300, 450)), "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _series(client).cover is SeriesCover.ARTWORK
    assert "now shows its artwork" in client.get(path).text


# --- API --------------------------------------------------------------------------


def test_the_api_sets_and_clears_a_series_choice(client):
    series = _series(client)
    url = f"/api/v1/admin/series/{series.id}/cover-source"
    chosen = client.put(url, headers=authorization(), json={"source": "first_volume"})
    assert chosen.json() == {
        "seriesId": series.id,
        "source": "first_volume",
        "libraryDefault": "artwork",
        "effective": "first_volume",
    }
    cleared = client.put(url, headers=authorization(), json={"source": "library"})
    assert cleared.json()["source"] == "library"
    assert cleared.json()["effective"] == "artwork"


def test_the_api_sets_a_library_default(client):
    series = _series(client)
    response = client.put(
        f"/api/v1/admin/libraries/{series.library_id}/series-cover",
        headers=authorization(),
        json={"source": "first_volume"},
    )
    assert response.status_code == 200
    assert response.json()["seriesCover"] == "first_volume"
    listed = client.get("/api/v1/admin/libraries", headers=authorization()).json()
    assert listed["libraries"][0]["seriesCover"] == "first_volume"


def test_the_api_refuses_readers_and_unknown_targets(client):
    series = _series(client)
    client.post(
        "/api/v1/admin/users",
        headers=authorization(),
        json={"username": "reader", "password": READER_PASSWORD},
    )
    reader = authorization("reader", READER_PASSWORD)
    body = {"source": "first_volume"}
    url = f"/api/v1/admin/series/{series.id}/cover-source"
    assert client.put(url, headers=reader, json=body).status_code == 403
    assert (
        client.put(
            "/api/v1/admin/series/missing/cover-source",
            headers=authorization(),
            json=body,
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/api/v1/admin/libraries/missing/series-cover",
            headers=authorization(),
            json=body,
        ).status_code
        == 404
    )
