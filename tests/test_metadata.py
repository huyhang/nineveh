from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from conftest import (
    ADMIN_PASSWORD,
    authorization,
    image_bytes,
    wait_for_scan,
    write_cbz,
)
from fastapi.testclient import TestClient

from nineveh.app import build_container, create_app
from nineveh.catalog import ArchiveInspector, CatalogScanner
from nineveh.config import Settings
from nineveh.database import SQLiteRepository
from nineveh.metadata import (
    MangaBakaProvider,
    MetadataCoverStore,
    MetadataError,
    MetadataService,
    PersistentRateLimiter,
    ProviderSeries,
    UrllibTransport,
)


class RecordingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


class StubTransport:
    def __init__(self, payload: dict | None = None, cover: bytes = b"cover") -> None:
        self.payload = payload or {"status": 200, "data": []}
        self.cover = cover
        self.urls: list[str] = []

    def get_json(self, url: str) -> dict:
        self.urls.append(url)
        return self.payload

    def get_bytes(self, url: str, maximum: int) -> bytes:
        self.urls.append(url)
        return self.cover


def _provider_item(title: str = "Fullmetal Alchemist", provider_id: int = 12) -> dict:
    return {
        "id": provider_id,
        "state": "active",
        "merged_with": None,
        "canonical_url": f"https://mangabaka.org/series/{provider_id}",
        "titles": [
            {
                "language": "en",
                "traits": ["official"],
                "title": title,
                "is_primary": True,
            },
            {
                "language": "ja",
                "traits": ["native"],
                "title": "鋼の錬金術師",
                "is_primary": False,
            },
        ],
        "authors": ["Hiromu Arakawa"],
        "artists": ["Hiromu Arakawa"],
        "description": "Two brothers search for the Philosopher's Stone.",
        "published": {
            "start_date": "2001-07-12",
            "end_date": "2010-06-11",
        },
        "status": "completed",
        "content_rating": "safe",
        "type": "manga",
        "rating": 91.5,
        "publishers": [{"name": "Square Enix", "type": "Original", "note": None}],
        "tags": [{"name": "Adventure"}, {"tag": {"name": "Fantasy"}}],
        "final_volume": 27,
        "total_chapters": 116,
        "last_updated_at": "2026-08-01T00:00:00Z",
        "cover": {
            "raw": "https://images.example/cover.jpg",
            "x350": "https://images.example/cover-350.jpg",
        },
    }


def _manga_repository(settings: Settings) -> tuple[SQLiteRepository, object]:
    archive = settings.data_dir / "Main Library" / "manga" / "Alchemy" / "01.cbz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    write_cbz(archive)
    repository = SQLiteRepository(settings.database_path)
    repository.initialize()
    repository.initialize_libraries(["Main Library"])
    CatalogScanner(settings.data_dir, repository, ArchiveInspector(settings)).scan()
    return repository, repository.catalog_series(category="manga")[0]


def test_mangabaka_v2_search_and_full_mapping():
    item = _provider_item()
    transport = StubTransport({"status": 200, "data": [item]})
    limiter = RecordingLimiter()
    provider = MangaBakaProvider(transport, limiter)

    candidates = provider.search("fullmetal", limit=99)
    assert candidates[0].title == "Fullmetal Alchemist"
    assert candidates[0].alternative_titles == ("鋼の錬金術師",)
    assert candidates[0].description.startswith("Two brothers")
    assert candidates[0].artists == ("Hiromu Arakawa",)
    assert candidates[0].rating == 91.5
    assert candidates[0].publishers == ("Square Enix",)
    assert candidates[0].tags == ("Adventure", "Fantasy")
    assert candidates[0].year == 2001
    assert candidates[0].source_url == "https://mangabaka.org/series/12"
    assert "limit=10" in transport.urls[0]
    assert "schema=full" in transport.urls[0]

    transport.payload = {"status": 200, "data": item}
    record = provider.fetch(12)
    assert record.values["authors"] == ["Hiromu Arakawa"]
    assert record.values["publishers"] == ["Square Enix"]
    assert record.values["tags"] == ["Adventure", "Fantasy"]
    assert record.cover_url == "https://images.example/cover-350.jpg"
    assert limiter.calls == 2


def test_mangabaka_canonical_links_are_normalized_and_not_invented():
    item = _provider_item()
    item["canonical_url"] = "/manga/12/fullmetal-alchemist"
    provider = MangaBakaProvider(
        StubTransport({"status": 200, "data": [item]}), RecordingLimiter()
    )
    assert provider.search("fullmetal")[0].source_url == (
        "https://mangabaka.org/manga/12/fullmetal-alchemist"
    )

    item["canonical_url"] = "https://example.com/not-mangabaka"
    assert provider.search("fullmetal")[0].source_url is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"status": 503, "message": "Try later"}, "Try later"),
        ({"status": 200, "data": "wrong"}, "unexpected search"),
    ],
)
def test_mangabaka_errors_are_safe(payload: dict, message: str):
    provider = MangaBakaProvider(StubTransport(payload), RecordingLimiter())
    with pytest.raises(MetadataError, match=message):
        provider.search("title")
    with pytest.raises(MetadataError, match="Enter a series title"):
        provider.search("  ")


def test_transport_bounds_and_validates_responses(monkeypatch):
    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return BytesIO(self.payload)

        def __exit__(self, *args):
            return None

    transport = UrllibTransport(timeout=1)
    monkeypatch.setattr(
        "nineveh.metadata.urlopen", lambda request, timeout: Response(b'{"ok":true}')
    )
    assert transport.get_json("https://api.mangabaka.org/test") == {"ok": True}

    monkeypatch.setattr(
        "nineveh.metadata.urlopen", lambda request, timeout: Response(b"[]")
    )
    with pytest.raises(MetadataError, match="unexpected"):
        transport.get_json("https://api.mangabaka.org/test")
    monkeypatch.setattr(
        "nineveh.metadata.urlopen", lambda request, timeout: Response(b"{")
    )
    with pytest.raises(MetadataError, match="invalid"):
        transport.get_json("https://api.mangabaka.org/test")
    monkeypatch.setattr(
        "nineveh.metadata.urlopen", lambda request, timeout: Response(b"xx")
    )
    with pytest.raises(MetadataError, match="more data"):
        transport.get_bytes("https://api.mangabaka.org/test", 1)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (HTTPError("url", 429, "limited", {}, None), "rate limiting"),
        (HTTPError("url", 404, "missing", {}, None), "could not find"),
        (HTTPError("url", 500, "broken", {}, None), "HTTP 500"),
        (URLError("offline"), "could not be reached"),
    ],
)
def test_transport_turns_network_failures_into_safe_messages(
    monkeypatch, error: Exception, message: str
):
    def fail(request, timeout):
        raise error

    monkeypatch.setattr("nineveh.metadata.urlopen", fail)
    with pytest.raises(MetadataError, match=message):
        UrllibTransport().get_bytes("https://api.mangabaka.org/test", 10)


def test_the_rate_limiter_uses_a_persistent_rolling_window(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    now = [1000.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = PersistentRateLimiter(
        repository, lambda: 2, clock=lambda: now[0], sleeper=sleep
    )
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()

    assert sleeps == [60.0]
    assert repository.reserve_metadata_request(2, now[0]) > 0


class StubProvider:
    def __init__(self, cover: bytes) -> None:
        self.cover_bytes = cover
        self.title = "Provider title"
        self.searches: list[str] = []
        self.fetches: list[int] = []

    def search(self, query: str, limit: int = 5):
        self.searches.append(query)
        return MangaBakaProvider(
            StubTransport({"status": 200, "data": [_provider_item(self.title)]}),
            RecordingLimiter(),
        ).search(query, limit)

    def fetch(self, provider_id: int) -> ProviderSeries:
        self.fetches.append(provider_id)
        item = _provider_item(self.title, provider_id)
        return ProviderSeries(
            provider_id,
            item["canonical_url"],
            MangaBakaProvider(
                StubTransport({"status": 200, "data": item}), RecordingLimiter()
            )
            .fetch(provider_id)
            .values,
            item,
            item["last_updated_at"],
            item["cover"]["x350"],
        )

    def cover(self, url: str) -> bytes:
        return self.cover_bytes


def test_metadata_service_preserves_overrides_and_custom_covers(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", state_dir=tmp_path / "state")
    repository, series = _manga_repository(settings)
    provider = StubProvider(image_bytes((10, 20, 30)))
    covers = MetadataCoverStore(settings.metadata_cover_dir, 1_000_000)
    service = MetadataService(repository, provider, covers)

    lookup = service.lookup(series)
    assert lookup.candidates[0].title == "Provider title"
    candidate_cover = service.candidate_cover(series.id, 12)
    assert candidate_cover.is_file()
    provider.cover_bytes = b"not an image"
    assert service.candidate_cover(series.id, 12) == candidate_cover
    with pytest.raises(MetadataError, match="unavailable"):
        service.candidate_cover(series.id, 999)
    provider.cover_bytes = image_bytes((10, 20, 30))
    metadata = service.match(series, 12)
    assert metadata.effective["title"] == "Provider title"
    assert covers.cover(series.id).is_file()

    metadata = service.update(series.id, {"title": "My title", "rating": "95"})
    assert metadata.overrides == {"title": "My title", "rating": 95}
    assert repository.catalog_series(query="Hiromu")[0].id == series.id
    assert repository.catalog_series(query="My title")[0].id == series.id
    provider.title = "Changed upstream"
    assert service.refresh(series).effective["title"] == "My title"

    service.covers.save_custom(series.id, image_bytes((90, 80, 70)))
    custom = covers.cover(series.id)
    assert custom is not None and custom.name.endswith("-custom.webp")
    service.unlink(series.id)
    assert repository.series_metadata(series.id) is None
    assert covers.cover(series.id) == custom
    covers.remove_custom(series.id)
    assert covers.cover(series.id) is None


def test_metadata_validation_and_failure_states(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", state_dir=tmp_path / "state")
    repository, manga = _manga_repository(settings)
    provider = StubProvider(image_bytes((10, 20, 30)))
    service = MetadataService(
        repository,
        provider,
        MetadataCoverStore(settings.metadata_cover_dir, settings.max_image_pixels),
    )

    with pytest.raises(MetadataError, match="before refreshing"):
        service.refresh(manga)
    with pytest.raises(MetadataError, match="before editing"):
        service.update(manga.id, {"title": "Mine"})
    comic = replace(manga, category="comics")
    with pytest.raises(MetadataError, match="only for manga"):
        service.lookup(comic)

    service.match(manga, 12)
    with pytest.raises(MetadataError, match="Unsupported"):
        service.update(manga.id, {"unknown": "value"})
    with pytest.raises(MetadataError, match="must be a number"):
        service.update(manga.id, {"rating": "excellent"})
    with pytest.raises(MetadataError, match="whole number"):
        service.update(manga.id, {"final_volume": "2.5"})

    class FailingProvider(StubProvider):
        def search(self, query: str, limit: int = 5):
            raise MetadataError("Provider unavailable")

    failed = MetadataService(repository, FailingProvider(b""), service.covers)
    with pytest.raises(MetadataError, match="Provider unavailable"):
        failed.lookup(manga)
    assert repository.metadata_lookup(manga.id).error == "Provider unavailable"


def test_a_merged_mangabaka_id_is_followed(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", state_dir=tmp_path / "state")
    repository, manga = _manga_repository(settings)

    class MergedProvider(StubProvider):
        def fetch(self, provider_id: int) -> ProviderSeries:
            self.fetches.append(provider_id)
            item = _provider_item(provider_id=provider_id)
            return ProviderSeries(
                provider_id,
                item["canonical_url"],
                {"title": f"Series {provider_id}"},
                item,
                None,
                None,
                13 if provider_id == 12 else None,
            )

    provider = MergedProvider(b"")
    service = MetadataService(
        repository,
        provider,
        MetadataCoverStore(settings.metadata_cover_dir, settings.max_image_pixels),
    )
    metadata = service.match(manga, 12)
    assert provider.fetches == [12, 13]
    assert metadata.provider_id == 13


def test_cover_store_rejects_large_and_invalid_images(tmp_path: Path):
    store = MetadataCoverStore(tmp_path, 10)
    with pytest.raises(MetadataError, match="16 MiB"):
        store.save_custom("series", b"x" * (16 * 1024 * 1024 + 1))
    with pytest.raises(MetadataError, match="supported image"):
        store.save_custom("series", b"not an image")
    with pytest.raises(MetadataError, match="dimensions"):
        store.save_custom("series", image_bytes((1, 2, 3), (4, 4)))


def test_provider_rejects_unsafe_cover_urls():
    provider = MangaBakaProvider(StubTransport(), RecordingLimiter())
    with pytest.raises(MetadataError, match="unsafe"):
        provider.cover("http://localhost/cover.jpg")


def test_metadata_browser_and_api_workflow(library):
    settings, _ = library
    manga = settings.data_dir / "Main Library" / "manga" / "Alchemy" / "01.cbz"
    manga.parent.mkdir(parents=True)
    write_cbz(manga)
    container = build_container(settings)
    provider = StubProvider(image_bytes((20, 40, 60)))
    metadata = MetadataService(
        container.repository,
        provider,
        MetadataCoverStore(settings.metadata_cover_dir, settings.max_image_pixels),
    )
    container = replace(container, metadata=metadata)

    with TestClient(create_app(container=container)) as client:
        wait_for_scan(client)
        assert (
            client.post(
                "/login", data={"username": "admin", "password": ADMIN_PASSWORD}
            ).status_code
            == 200
        )
        series_body = client.get(
            "/api/v1/admin/metadata", headers=authorization()
        ).json()["series"][0]
        series_id = series_body["id"]
        library_id = series_body["libraryId"]
        page = client.get(f"/libraries/{library_id}/manga/metadata")
        assert "Alchemy" in page.text
        assert client.get("/admin/metadata").status_code == 404
        category_page = client.get(f"/libraries/{library_id}/manga")
        assert f'href="/libraries/{library_id}/manga/metadata"' in category_page.text
        assert ">Manage metadata</a>" in client.get(f"/series/{series_id}").text
        assert "/admin/metadata" not in client.get("/admin").text

        empty_batch = client.post(
            f"/libraries/{library_id}/manga/metadata/lookup",
            data={
                "csrf_token": page.text.split('name="csrf_token" value="')[1].split(
                    '"'
                )[0]
            },
            follow_redirects=True,
        )
        assert "Select at least one" in empty_batch.text
        batch = client.post(
            "/api/v1/admin/metadata/lookup",
            headers=authorization(),
            json={"series_ids": [series_id, series_id]},
        )
        assert batch.status_code == 202
        batch_task = client.app.state.metadata_task
        if batch_task:
            for _ in range(100):
                if batch_task.done():
                    break
                import time

                time.sleep(0.01)
        assert client.app.state.metadata_status["completed"] == 1

        admin_detail = client.get(f"/series/{series_id}/metadata")
        csrf = admin_detail.text.split('name="csrf_token" value="')[1].split('"')[0]
        web_lookup = client.post(
            f"/series/{series_id}/metadata/lookup",
            data={"csrf_token": csrf, "query": "Alchemy"},
            follow_redirects=True,
        )
        assert "MangaBaka suggestions updated" in web_lookup.text
        assert "Two brothers search for the Philosopher&#39;s Stone" in web_lookup.text
        assert "Square Enix" in web_lookup.text
        assert "Adventure" in web_lookup.text
        assert 'href="https://mangabaka.org/series/12"' in web_lookup.text
        candidate_cover = client.get(
            f"/api/v1/admin/series/{series_id}/metadata/candidates/12/cover"
        )
        assert candidate_cover.status_code == 200
        assert candidate_cover.headers["content-type"] == "image/webp"

        lookup = client.post(
            f"/api/v1/admin/series/{series_id}/metadata/lookup",
            headers=authorization(),
            json={"query": "Alchemy"},
        )
        assert lookup.status_code == 200
        assert lookup.json()["candidates"][0]["title"] == "Provider title"

        matched = client.post(
            f"/api/v1/admin/series/{series_id}/metadata/match",
            headers=authorization(),
            json={"provider_id": 12},
        )
        assert matched.status_code == 200
        assert matched.json()["title"] == "Provider title"
        assert client.get(f"/api/v1/series/{series_id}").status_code == 200
        detail = client.get(f"/series/{series_id}")
        assert "Provider title" in detail.text
        assert "CC BY-NC-SA 4.0" in detail.text

        edited = client.patch(
            f"/api/v1/admin/series/{series_id}/metadata",
            headers=authorization(),
            json={"values": {"title": "Admin title"}},
        )
        assert edited.json()["title"] == "Admin title"
        assert "Admin title" in client.get(f"/series/{series_id}").text
        assert client.get(f"/api/v1/series/{series_id}/cover").status_code == 200

        refreshed = client.post(
            f"/api/v1/admin/series/{series_id}/metadata/refresh",
            headers=authorization(),
        )
        assert refreshed.status_code == 200

        web_edit = client.post(
            f"/series/{series_id}/metadata/edit",
            data={"csrf_token": csrf, "title": "Web title"},
            follow_redirects=True,
        )
        assert "Series metadata saved" in web_edit.text
        web_refresh = client.post(
            f"/series/{series_id}/metadata/refresh",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )
        assert "local edits were preserved" in web_refresh.text
        uploaded = client.post(
            f"/series/{series_id}/metadata/cover",
            data={"csrf_token": csrf},
            files={"cover": ("cover.png", image_bytes((1, 2, 3)), "image/png")},
            follow_redirects=True,
        )
        assert "Custom series cover uploaded" in uploaded.text
        assert "Retrieved MangaBaka record" in uploaded.text
        removed = client.post(
            f"/series/{series_id}/metadata/cover/remove",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )
        assert "Custom cover removed" in removed.text

        web_unlinked = client.post(
            f"/series/{series_id}/metadata/unlink",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )
        assert "custom cover preserved" in web_unlinked.text

        web_matched = client.post(
            f"/series/{series_id}/metadata/match",
            data={"csrf_token": csrf, "provider_id": "12"},
            follow_redirects=True,
        )
        assert "MangaBaka metadata linked" in web_matched.text

        unlinked = client.delete(
            f"/api/v1/admin/series/{series_id}/metadata",
            headers=authorization(),
        )
        assert unlinked.status_code == 200
        assert client.get(f"/api/v1/series/{series_id}").json()["metadata"] is None


def test_metadata_raw_snapshot_is_kept_separate_from_edits(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", state_dir=tmp_path / "state")
    repository, series = _manga_repository(settings)
    item = _provider_item()
    repository.save_series_metadata(
        series.id,
        12,
        item["canonical_url"],
        {"title": "Provider title"},
        item,
        item["last_updated_at"],
    )
    repository.replace_metadata_overrides(series.id, {"title": "Admin title"})

    stored = repository.series_metadata(series.id)
    assert stored is not None
    assert stored.raw == item
    assert stored.values["title"] == "Provider title"
    assert stored.effective["title"] == "Admin title"
    assert json.loads(json.dumps(stored.raw))["id"] == 12


def test_mangabaka_limit_cannot_exceed_thirty(monkeypatch):
    with pytest.raises(ValueError, match="between 1 and 30"):
        Settings(mangabaka_requests_per_minute=31)
    monkeypatch.setenv("NINEVEH_MANGABAKA_REQUESTS_PER_MINUTE", "31")
    with pytest.raises(ValueError, match="between 1 and 30"):
        Settings.from_env()


def test_the_display_title_ignores_per_language_primary_flags():
    """MangaBaka flags one primary title *per language*, so taking the first
    flagged entry showed Berserk as the Korean 베르세르크."""
    item = _provider_item()
    item["titles"] = [
        {
            "language": "ko",
            "traits": ["official"],
            "title": "베르세르크",
            "is_primary": True,
        },
        {
            "language": "en",
            "traits": ["official"],
            "title": "BERSERK",
            "is_primary": True,
        },
        {
            "language": "ja-Latn",
            "traits": ["native"],
            "title": "Beruseruku",
            "is_primary": True,
        },
        {
            "language": "ja",
            "traits": ["native"],
            "title": "ベルセルク",
            "is_primary": True,
        },
    ]
    provider = MangaBakaProvider(
        StubTransport({"status": 200, "data": [item]}), RecordingLimiter()
    )

    candidate = provider.search("berserk")[0]
    assert candidate.title == "BERSERK"
    assert candidate.alternative_titles[0] == "Beruseruku"
    assert "베르세르크" in candidate.alternative_titles


def test_an_unofficial_english_title_still_beats_another_language():
    item = _provider_item()
    item["titles"] = [
        {
            "language": "ko",
            "traits": ["official"],
            "title": "한국어",
            "is_primary": True,
        },
        {
            "language": "en",
            "traits": ["alternative"],
            "title": "English Title",
            "is_primary": False,
        },
    ]
    provider = MangaBakaProvider(
        StubTransport({"status": 200, "data": [item]}), RecordingLimiter()
    )
    assert provider.search("x")[0].title == "English Title"


def test_a_series_with_no_usable_titles_falls_back_to_the_local_name():
    item = _provider_item()
    item["titles"] = [{"language": "en", "traits": [], "title": "   "}]
    provider = MangaBakaProvider(
        StubTransport({"status": 200, "data": [item]}), RecordingLimiter()
    )
    assert provider.search("x")[0].title == "Untitled series"


def test_provider_lists_are_capped_so_one_series_cannot_flood_the_page():
    item = _provider_item()
    item["tags"] = [{"name": f"Tag {number}"} for number in range(320)]
    item["authors"] = [f"Author {number}" for number in range(50)]
    provider = MangaBakaProvider(
        StubTransport({"status": 200, "data": item}), RecordingLimiter()
    )

    values = provider.fetch(12).values
    assert len(values["tags"]) == 20
    assert len(values["authors"]) == 20
    # The untruncated payload is still retained for the admin record.
    assert len(provider.fetch(12).raw["tags"]) == 320


def test_a_retired_series_is_refused_rather_than_stored_hollow():
    item = _provider_item()
    item["state"] = "deleted"
    provider = MangaBakaProvider(
        StubTransport({"status": 200, "data": item}), RecordingLimiter()
    )
    with pytest.raises(MetadataError, match="retired"):
        provider.fetch(12)


def test_a_rate_limit_reply_reports_when_to_retry(monkeypatch):
    error = HTTPError("https://api.mangabaka.org/", 429, "Too Many", {}, None)
    error.headers = {"Retry-After": "45"}

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("nineveh.metadata.urlopen", fail)
    with pytest.raises(MetadataError, match="try again in 45 seconds"):
        UrllibTransport().get_json("https://api.mangabaka.org/v2/series/1")


def test_saving_the_edit_form_unchanged_keeps_commas_out_of_the_split(library):
    """Re-posting the rendered form used to shred "Smith, John" into two
    names and persist the wreckage as a local override."""
    settings, _ = library
    repository, series = _manga_repository(settings)
    values = {
        "title": "Alchemy",
        "alternative_titles": ["Oh, My Sweet Alien!", "Plain Title"],
        "authors": ["Smith, John"],
    }
    repository.save_series_metadata(
        series.id, 12, "https://mangabaka.org/series/12", values, {}, None
    )
    service = MetadataService(
        repository,
        StubProvider(image_bytes((1, 2, 3))),
        MetadataCoverStore(settings.metadata_cover_dir, settings.max_image_pixels),
    )
    current = repository.series_metadata(series.id)

    # Exactly what the textareas render and a browser posts back untouched.
    saved = service.update(
        series.id,
        {
            field: "\n".join(current.effective[field])
            for field in ("alternative_titles", "authors")
        },
    )

    assert saved.effective["alternative_titles"] == [
        "Oh, My Sweet Alien!",
        "Plain Title",
    ]
    assert saved.effective["authors"] == ["Smith, John"]
    assert saved.overrides == {}


def test_an_over_long_list_override_is_refused_rather_than_truncated(library):
    settings, _ = library
    repository, series = _manga_repository(settings)
    repository.save_series_metadata(
        series.id, 12, "https://mangabaka.org/series/12", {"tags": []}, {}, None
    )
    service = MetadataService(
        repository,
        StubProvider(image_bytes((1, 2, 3))),
        MetadataCoverStore(settings.metadata_cover_dir, settings.max_image_pixels),
    )

    with pytest.raises(MetadataError, match="at most 20 entries"):
        service.update(series.id, {"tags": "\n".join(f"t{n}" for n in range(21))})


def test_candidate_cover_thumbnails_stay_under_a_budget(tmp_path: Path):
    """Suggestion thumbnails are throwaway; they used to accumulate forever
    in the one /state directory the README says to back up."""
    store = MetadataCoverStore(tmp_path / "covers", 10_000_000, candidate_cache_bytes=1)
    payload = image_bytes((10, 20, 30), (64, 96))

    first = store.save_candidate(1, payload)
    store.save_candidate(2, payload)

    assert not first.exists()
    assert store.candidate(1) is None
    assert store.candidate(2) is not None
    assert (tmp_path / "covers" / "candidates").is_dir()


def test_series_covers_are_not_evicted_with_the_candidate_cache(tmp_path: Path):
    store = MetadataCoverStore(tmp_path / "covers", 10_000_000, candidate_cache_bytes=1)
    payload = image_bytes((10, 20, 30), (64, 96))

    store.save_provider("series", payload)
    store.save_candidate(1, payload)
    store.save_candidate(2, payload)

    assert store.cover("series") is not None


def _metadata_client(settings, provider):
    container = build_container(settings)
    return replace(
        container,
        metadata=MetadataService(
            container.repository,
            provider,
            MetadataCoverStore(settings.metadata_cover_dir, settings.max_image_pixels),
        ),
    )


def test_the_browser_batch_lookup_matches_only_the_selected_series(library):
    """The batch route shipped untested; it is the one destructive-ish admin
    action that fans out over the provider."""
    settings, _ = library
    for name in ("Alchemy", "Beacon"):
        archive = settings.data_dir / "Main Library" / "manga" / name / "01.cbz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        write_cbz(archive)
    provider = StubProvider(image_bytes((20, 40, 60)))
    container = _metadata_client(settings, provider)

    with TestClient(create_app(container=container)) as client:
        wait_for_scan(client)
        client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
        series = client.get("/api/v1/admin/metadata", headers=authorization()).json()[
            "series"
        ]
        library_id = series[0]["libraryId"]
        chosen = series[0]["id"]
        page = client.get(f"/libraries/{library_id}/manga/metadata")
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]

        started = client.post(
            f"/libraries/{library_id}/manga/metadata/lookup",
            data={"csrf_token": csrf, "series_id": chosen},
            follow_redirects=False,
        )
        assert started.status_code == 303
        assert "?" not in started.headers["location"]

        task = client.app.state.metadata_task
        for _ in range(200):
            if task is None or task.done():
                break
            time.sleep(0.01)
        assert client.app.state.metadata_status == {
            "running": False,
            "completed": 1,
            "total": 1,
            "failed": 0,
        }
        assert provider.searches == ["Alchemy"]
        assert (
            "Started looking up 1 series."
            in client.get(f"/libraries/{library_id}/manga/metadata").text
        )


def test_the_browser_batch_lookup_rejects_series_outside_the_library(library):
    settings, _ = library
    archive = settings.data_dir / "Main Library" / "manga" / "Alchemy" / "01.cbz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    write_cbz(archive)
    provider = StubProvider(image_bytes((20, 40, 60)))
    container = _metadata_client(settings, provider)

    with TestClient(create_app(container=container)) as client:
        wait_for_scan(client)
        client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
        body = client.get("/api/v1/admin/metadata", headers=authorization()).json()
        library_id = body["series"][0]["libraryId"]
        page = client.get(f"/libraries/{library_id}/manga/metadata")
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]

        refused = client.post(
            f"/libraries/{library_id}/manga/metadata/lookup",
            data={"csrf_token": csrf, "series_id": "not-in-this-library"},
            follow_redirects=True,
        )

        assert "were not found" in refused.text
        assert provider.searches == []


def test_legacy_candidate_thumbnails_are_swept_at_startup(library):
    """The old flat layout is unreachable *and* outside the new budget, so
    without a sweep those files would sit in /state forever."""
    settings, _ = library
    settings.metadata_cover_dir.mkdir(parents=True, exist_ok=True)
    stale = settings.metadata_cover_dir / "candidate-1692.webp"
    stale.write_bytes(b"old")
    kept = settings.metadata_cover_dir / "abc-provider.webp"
    kept.write_bytes(b"cover")

    with TestClient(create_app(settings)):
        pass

    assert not stale.exists()
    assert kept.exists()


def test_a_provider_rating_survives_a_no_op_save(library):
    """`step="0.1"` rejected MangaBaka's own 91.4937142857143, which blocked
    the whole form -- including edits to unrelated fields."""
    settings, _ = library
    repository, series = _manga_repository(settings)
    repository.save_series_metadata(
        series.id,
        12,
        "https://mangabaka.org/series/12",
        {"rating": 91.4937142857143, "title": "Berserk"},
        {},
        None,
    )
    service = MetadataService(
        repository,
        StubProvider(image_bytes((1, 2, 3))),
        MetadataCoverStore(settings.metadata_cover_dir, settings.max_image_pixels),
    )

    saved = service.update(series.id, {"rating": "91.4937142857143"})

    assert saved.effective["rating"] == 91.4937142857143
    assert saved.overrides == {}


def test_the_rating_input_does_not_reject_provider_precision(library):
    settings, _ = library
    repository, series = _manga_repository(settings)
    repository.save_series_metadata(
        series.id,
        12,
        "https://mangabaka.org/series/12",
        {"rating": 91.4937142857143},
        {},
        None,
    )
    container = _metadata_client(settings, StubProvider(image_bytes((1, 2, 3))))

    with TestClient(create_app(container=container)) as client:
        client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
        page = client.get(f"/series/{series.id}/metadata").text

    rating = re.search(r'<input name="rating"[^>]*>', page).group(0)
    assert 'step="any"' in rating
    assert 'value="91.4937142857143"' in rating


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rating", "101", "between 0 and 100"),
        ("rating", "-1", "between 0 and 100"),
        ("final_volume", "-3", "between 0 and 10000"),
        ("total_chapters", "999999", "between 0 and 100000"),
    ],
)
def test_out_of_range_numbers_are_refused_by_the_service_not_just_the_browser(
    library, field: str, value: str, message: str
):
    settings, _ = library
    repository, series = _manga_repository(settings)
    repository.save_series_metadata(
        series.id, 12, "https://mangabaka.org/series/12", {field: None}, {}, None
    )
    service = MetadataService(
        repository,
        StubProvider(image_bytes((1, 2, 3))),
        MetadataCoverStore(settings.metadata_cover_dir, settings.max_image_pixels),
    )

    with pytest.raises(MetadataError, match=message):
        service.update(series.id, {field: value})
