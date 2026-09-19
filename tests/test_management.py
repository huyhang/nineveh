from __future__ import annotations

import sqlite3
import time
import zipfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import (
    READER_PASSWORD,
    authorization,
    image_bytes,
    wait_for_scan,
    write_cbz,
)
from fastapi.testclient import TestClient

from nineveh import database
from nineveh.app import create_app
from nineveh.authorization import AccessService, GrantPolicy
from nineveh.catalog import InvalidLibrary, LibraryService
from nineveh.config import Settings, SettingsService
from nineveh.database import SQLiteRepository
from nineveh.domain import AccessGrant
from nineveh.restart import DisabledRestartController, ProcessRestartController


def _create_reader(client: TestClient, username: str = "reader") -> tuple[str, dict]:
    response = client.post(
        "/api/v1/admin/users",
        headers=authorization(),
        json={"username": username, "password": READER_PASSWORD},
    )
    return response.json()["id"], authorization(username, READER_PASSWORD)


def _wait_for_publications(client: TestClient, count: int) -> None:
    for _ in range(200):
        body = client.get("/opds/v2/publications.json", headers=authorization()).json()
        task = client.app.state.scan_task
        if len(body["publications"]) == count and (task is None or task.done()):
            return
        time.sleep(0.01)
    raise AssertionError(f"catalog did not reach {count} publications")


def test_readers_are_default_deny_and_library_grants_filter_every_surface(
    client: TestClient, publication_id: str
):
    user_id, reader = _create_reader(client)

    assert (
        client.get("/opds/v2/catalog.json", headers=reader).json()["navigation"] == []
    )
    assert (
        client.get("/opds/v2/publications.json", headers=reader).json()["publications"]
        == []
    )
    for suffix in (
        "",
        "/file",
        "/cover",
        "/pages",
        "/pages/1",
        "/range?start=1&end=1",
    ):
        assert (
            client.get(
                f"/api/v1/publications/{publication_id}{suffix}", headers=reader
            ).status_code
            == 404
        )

    series_id = client.app.state.container.repository.catalog_series()[0].id
    assert client.get(f"/api/v1/series/{series_id}", headers=reader).status_code == 404
    assert (
        client.get(f"/api/v1/series/{series_id}/cover", headers=reader).status_code
        == 404
    )

    library = client.get("/api/v1/admin/libraries", headers=authorization()).json()[
        "libraries"
    ][0]
    response = client.put(
        f"/api/v1/admin/users/{user_id}/access",
        headers=authorization(),
        json={"grants": [{"library_id": library["id"]}]},
    )
    assert response.status_code == 200
    assert response.json()["grants"] == [
        {"libraryId": library["id"], "category": None, "seriesId": None}
    ]
    assert (
        len(
            client.get("/opds/v2/publications.json", headers=reader).json()[
                "publications"
            ]
        )
        == 1
    )
    assert (
        client.get(f"/api/v1/publications/{publication_id}", headers=reader).status_code
        == 200
    )

    cleared = client.put(
        f"/api/v1/admin/users/{user_id}/access",
        headers=authorization(),
        json={"grants": []},
    )
    assert cleared.json()["grants"] == []


def test_content_type_and_series_grants_are_scoped_to_their_library(library):
    settings, _ = library
    manga = settings.data_dir / "Main Library" / "manga" / "Manga Series" / "One.cbz"
    manga.parent.mkdir(parents=True)
    write_cbz(manga)
    other = settings.data_dir / "Other Library" / "comics" / "Other Series" / "One.cbz"
    other.parent.mkdir(parents=True)
    write_cbz(other)

    with TestClient(create_app(settings)) as client:
        wait_for_scan(client)
        user_id, reader = _create_reader(client, "limited")
        libraries = client.get(
            "/api/v1/admin/libraries", headers=authorization()
        ).json()["libraries"]
        main = next(item for item in libraries if item["name"] == "Main Library")
        manga_group = next(
            item for item in main["categories"] if item["name"] == "manga"
        )
        comic_group = next(
            item for item in main["categories"] if item["name"] == "comics"
        )

        client.put(
            f"/api/v1/admin/users/{user_id}/access",
            headers=authorization(),
            json={
                "grants": [
                    {"library_id": main["id"], "category": "manga"},
                    {
                        "library_id": main["id"],
                        "category": "comics",
                        "series_id": comic_group["series"][0]["id"],
                    },
                ]
            },
        )
        feed = client.get("/opds/v2/publications.json", headers=reader).json()
        assert len(feed["publications"]) == 2
        root = client.get("/opds/v2/catalog.json", headers=reader).json()
        assert [item["title"] for item in root["navigation"]] == ["Main Library"]
        navigation = client.get(
            "/opds/v2/navigation.json?library=Main%20Library", headers=reader
        ).json()
        assert {item["title"] for item in navigation["navigation"]} == {
            "comics",
            "manga",
        }
        assert manga_group["series"][0]["size"] == manga.stat().st_size


def test_library_lifecycle_scans_new_content_and_never_deletes_media(
    client: TestClient, library
):
    settings, _ = library
    archive = settings.data_dir / "Added Library" / "comics" / "Series" / "New.cbz"
    archive.parent.mkdir(parents=True)
    write_cbz(archive)

    before = client.get("/api/v1/admin/libraries", headers=authorization()).json()
    assert "Added Library" in before["available"]
    added = client.post(
        "/api/v1/admin/libraries",
        headers=authorization(),
        json={"relative_path": "Added Library"},
    )
    assert added.status_code == 201
    assert added.json()["scanStarted"] is True
    _wait_for_publications(client, 2)

    removed = client.delete(
        f"/api/v1/admin/libraries/{added.json()['id']}", headers=authorization()
    )
    assert removed.status_code == 200
    assert removed.json()["enabled"] is False
    assert archive.exists()
    _wait_for_publications(client, 1)
    assert (
        "Added Library"
        in client.get("/api/v1/admin/libraries", headers=authorization()).json()[
            "available"
        ]
    )
    readded = client.post(
        "/api/v1/admin/libraries",
        headers=authorization(),
        json={"relative_path": "Added Library"},
    )
    assert readded.status_code == 201
    assert readded.json()["id"] == added.json()["id"]


def test_same_name_replacement_refreshes_revision_and_capacity(
    client: TestClient, library
):
    _, archive = library
    repository = client.app.state.container.repository
    original = repository.publications(limit=1)[0][0]
    original_size = original.size

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
        handle.writestr("replacement.png", image_bytes((8, 16, 24), (240, 360)))
    managed = repository.managed_libraries()[0]
    report = client.app.state.container.scanner.scan(managed.id)
    updated = repository.publications(limit=1)[0][0]

    assert report.indexed == 1
    assert updated.id == original.id
    assert updated.revision != original.revision
    assert updated.size != original_size
    assert repository.library_usage()[0].size == archive.stat().st_size


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("../outside", "top-level directory"),
        ("Main Library/child", "top-level directory"),
        ("/etc", "top-level directory"),
        ("", "top-level directory"),
        ("missing", "No directory named missing"),
    ],
)
def test_library_service_rejects_unsafe_or_unavailable_directories(
    tmp_path: Path, name: str, reason: str
):
    """The shape check and the existence check are separate lines of defence."""
    data = tmp_path / "data"
    data.mkdir()
    repository = SQLiteRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    service = LibraryService(data, repository)
    with pytest.raises(InvalidLibrary, match=reason):
        service.add(name)


def test_empty_initial_library_discovery_only_runs_once(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    repository.initialize_libraries([])
    repository.initialize_libraries(["Added Later"])
    assert repository.managed_libraries() == []


def test_settings_are_validated_persisted_and_loaded_after_restart(library):
    settings, _ = library
    with TestClient(create_app(settings)) as client:
        wait_for_scan(client)
        current = client.get("/api/v1/admin/settings", headers=authorization()).json()
        assert current["pendingRestart"] is False
        assert current["restartEnabled"] is False

        bad = client.put(
            "/api/v1/admin/settings",
            headers=authorization(),
            json={"values": {"feed_page_size": "0"}},
        )
        assert bad.status_code == 422
        saved = client.put(
            "/api/v1/admin/settings",
            headers=authorization(),
            json={"values": {"feed_page_size": "7", "service_title": "Archive"}},
        )
        assert saved.status_code == 200
        assert saved.json()["pendingRestart"] is True
        assert (
            client.post("/api/v1/admin/restart", headers=authorization()).status_code
            == 409
        )

    with TestClient(create_app(settings)) as restarted:
        wait_for_scan(restarted)
        assert restarted.app.state.container.settings.feed_page_size == 7
        assert restarted.app.title == "Archive"


def test_settings_service_reports_pending_changes(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    service = SettingsService(Settings(feed_page_size=24), repository)
    service.activate()
    service.update({"feed_page_size": "12"})
    assert service.saved().feed_page_size == 12
    assert service.pending_restart()


def test_mangabaka_rate_limit_applies_without_a_restart(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    service = SettingsService(Settings(), repository)
    service.activate()

    service.update({"mangabaka_requests_per_minute": "7"})

    assert service.mangabaka_request_limit() == 7
    assert not service.pending_restart()


def test_only_settings_that_differ_from_the_deployment_are_persisted(tmp_path: Path):
    """A UI save must not freeze the settings it did not change.

    Storing every editable key would make the database shadow `docker/.env`
    forever, so a later deployment edit would silently do nothing.
    """
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    first = Settings(feed_page_size=24, page_range_limit=100)
    service = SettingsService(first, repository)
    service.activate()

    service.update({"service_title": "Archive"})
    assert repository.settings() == {"service_title": "Archive"}

    # The operator now raises a limit the administrator never touched.
    second = SettingsService(
        Settings(feed_page_size=24, page_range_limit=55), repository
    )
    effective = second.activate()
    assert effective.page_range_limit == 55
    assert effective.service_title == "Archive"


def test_an_override_for_a_retired_setting_does_not_block_startup(tmp_path: Path):
    """`public_base_url` moved back to the environment; databases outlive that.

    Rejecting the stale row would leave an upgraded install unbootable, with no
    way in to remove it.
    """
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    repository.replace_settings(
        {"public_base_url": "https://old.example", "feed_page_size": "7"}
    )

    service = SettingsService(Settings(feed_page_size=24), repository)
    effective = service.activate()

    assert effective.feed_page_size == 7
    assert repository.settings() == {"feed_page_size": "7"}
    assert effective.public_base_url is None


def test_activation_drops_overrides_the_deployment_has_caught_up_with(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    repository.replace_settings({"feed_page_size": "7", "service_title": "Archive"})

    service = SettingsService(Settings(feed_page_size=7), repository)
    effective = service.activate()

    assert repository.settings() == {"service_title": "Archive"}
    assert effective.feed_page_size == 7
    assert not service.pending_restart()


def test_invalid_access_updates_are_rejected(client: TestClient):
    user_id, _ = _create_reader(client)
    assert (
        client.put(
            f"/api/v1/admin/users/{user_id}/access",
            headers=authorization(),
            json={"grants": [{"library_id": "missing"}]},
        ).status_code
        == 422
    )
    admin_id = client.get("/api/v1/auth/me", headers=authorization()).json()["id"]
    assert (
        client.put(
            f"/api/v1/admin/users/{admin_id}/access",
            headers=authorization(),
            json={"grants": []},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/admin/users/missing/access", headers=authorization()
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/v1/admin/libraries", None),
        ("post", "/api/v1/admin/libraries", {"relative_path": "anything"}),
        ("get", "/api/v1/admin/settings", None),
        ("put", "/api/v1/admin/settings", {"values": {}}),
        ("post", "/api/v1/admin/restart", None),
    ],
)
def test_readers_cannot_use_management_endpoints(
    client: TestClient, reader: dict, method: str, path: str, payload: dict | None
):
    response = client.request(method, path, headers=reader, json=payload)
    assert response.status_code == 403


def test_library_admin_endpoints_report_missing_resources(client: TestClient):
    assert (
        client.post(
            "/api/v1/admin/libraries",
            headers=authorization(),
            json={"relative_path": "missing"},
        ).status_code
        == 422
    )
    assert (
        client.delete(
            "/api/v1/admin/libraries/missing", headers=authorization()
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/admin/libraries/missing/scan", headers=authorization()
        ).status_code
        == 404
    )


def test_removing_a_library_revokes_the_grants_that_pointed_at_it(client: TestClient):
    """Otherwise re-adding the directory later silently restores old access."""
    user_id, reader = _create_reader(client, "scoped")
    library = client.get("/api/v1/admin/libraries", headers=authorization()).json()[
        "libraries"
    ][0]
    client.put(
        f"/api/v1/admin/users/{user_id}/access",
        headers=authorization(),
        json={"grants": [{"library_id": library["id"]}]},
    )
    granted = client.get(
        f"/api/v1/admin/users/{user_id}/access", headers=authorization()
    )
    assert granted.json()["grants"]

    client.delete(f"/api/v1/admin/libraries/{library['id']}", headers=authorization())
    assert (
        client.get(
            f"/api/v1/admin/users/{user_id}/access", headers=authorization()
        ).json()["grants"]
        == []
    )

    readded = client.post(
        "/api/v1/admin/libraries",
        headers=authorization(),
        json={"relative_path": library["relativePath"]},
    )
    assert readded.status_code == 201
    _wait_for_publications(client, 1)
    assert (
        client.get(
            f"/api/v1/admin/users/{user_id}/access", headers=authorization()
        ).json()["grants"]
        == []
    )
    assert (
        client.get("/opds/v2/publications.json", headers=reader).json()["publications"]
        == []
    )


def test_all_access_grants_groups_every_user_in_one_query(client: TestClient):
    first, _ = _create_reader(client, "reader.one")
    second, _ = _create_reader(client, "reader.two")
    library = client.get("/api/v1/admin/libraries", headers=authorization()).json()[
        "libraries"
    ][0]
    for user_id in (first, second):
        client.put(
            f"/api/v1/admin/users/{user_id}/access",
            headers=authorization(),
            json={"grants": [{"library_id": library["id"]}]},
        )
    grouped = client.app.state.container.repository.all_access_grants()
    assert set(grouped) == {first, second}
    assert [grant.library_id for grant in grouped[first]] == [library["id"]]


def test_restart_api_uses_the_injected_controller(client: TestClient):
    class Restart:
        enabled = True

        def __init__(self):
            self.requested = False

        def request_restart(self):
            self.requested = True

    controller = Restart()
    client.app.state.container = replace(
        client.app.state.container, restarter=controller
    )
    response = client.post("/api/v1/admin/restart", headers=authorization())
    assert response.status_code == 202
    assert response.json() == {"status": "restarting"}
    assert controller.requested


def test_grant_policy_matches_each_hierarchy_level(client: TestClient):
    user_id, _ = _create_reader(client)
    repository = client.app.state.container.repository
    publication = repository.publications(limit=1)[0][0]
    service = AccessService(repository)
    policy = GrantPolicy(repository)
    user = repository.user_by_id(user_id)
    assert user is not None
    assert not policy.can_read(user, publication)
    for grant in (
        AccessGrant(user_id, publication.library_id),
        AccessGrant(user_id, publication.library_id, publication.category),
        AccessGrant(
            user_id,
            publication.library_id,
            publication.category,
            publication.series_id,
        ),
    ):
        service.replace(user_id, [grant])
        assert policy.can_read(user, publication)
    wrong = replace(publication, library_id="somewhere-else")
    assert not policy.can_read(user, wrong)


def _v1_database(path: Path) -> Path:
    """A database as the first Nineveh release left it: schema v1, no grants."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL,
                enabled INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            CREATE TABLE publications (
                id TEXT PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE,
                library TEXT NOT NULL, category TEXT NOT NULL, series TEXT NOT NULL,
                filename TEXT NOT NULL, title TEXT NOT NULL, number TEXT,
                description TEXT, authors_json TEXT NOT NULL, modified_ns INTEGER NOT NULL,
                size INTEGER NOT NULL, revision TEXT NOT NULL, page_count INTEGER NOT NULL,
                cover_page INTEGER NOT NULL
            );
            CREATE TABLE pages (
                publication_id TEXT NOT NULL, number INTEGER NOT NULL,
                member_name TEXT NOT NULL, media_type TEXT NOT NULL,
                compressed_size INTEGER NOT NULL, uncompressed_size INTEGER NOT NULL,
                crc INTEGER NOT NULL, width INTEGER, height INTEGER,
                PRIMARY KEY(publication_id, number)
            );
            INSERT INTO users VALUES
                ('admin', 'admin', 'hash', 1, 1, '2026-01-01T00:00:00+00:00'),
                ('reader', 'reader', 'hash', 0, 1, '2026-01-01T00:00:00+00:00');
            INSERT INTO publications VALUES (
                'pub', 'Main/comics/Series/One.cbz', 'Main', 'comics', 'Series',
                'One.cbz', 'One', NULL, NULL, '[]', 1, 4096, 'revision', 1, 1
            );
            PRAGMA user_version = 1;
            """
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_v1_migration_preserves_existing_reader_access(tmp_path: Path):
    path = _v1_database(tmp_path / "old.sqlite3")

    repository = SQLiteRepository(path)
    repository.initialize()
    library = repository.managed_libraries()[0]
    grant = repository.access_grants("reader")[0]
    publication = repository.publication_by_id("pub")
    assert grant.library_id == library.id
    assert publication is not None and publication.series_id
    assert repository.publication_by_id(
        "pub", GrantPolicy(repository).read_scope(repository.user_by_id("reader"))
    )


def test_an_interrupted_upgrade_resumes_instead_of_wedging(tmp_path: Path, monkeypatch):
    """`executescript` commits as it runs, so a half-applied upgrade is on disk.

    Killing the container mid-upgrade — a NAS reboot, an OOM kill — must not
    leave a database that refuses to open on every subsequent start.
    """
    path = _v1_database(tmp_path / "old.sqlite3")
    real_uuid4 = database.uuid.uuid4

    def explode() -> str:
        raise KeyboardInterrupt("container killed mid-upgrade")

    monkeypatch.setattr(database.uuid, "uuid4", explode)
    with pytest.raises(KeyboardInterrupt):
        SQLiteRepository(path).initialize()
    monkeypatch.setattr(database.uuid, "uuid4", real_uuid4)

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1

    repository = SQLiteRepository(path)
    repository.initialize()
    assert [item.name for item in repository.managed_libraries()] == ["Main"]
    assert repository.access_grants("reader")
    publication = repository.publication_by_id("pub")
    assert publication is not None and publication.series_id


def test_upgrading_twice_neither_duplicates_libraries_nor_regrants(tmp_path: Path):
    path = _v1_database(tmp_path / "old.sqlite3")
    SQLiteRepository(path).initialize()
    repository = SQLiteRepository(path)
    repository.replace_access_grants("reader", [])

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    repository.initialize()

    assert len(repository.managed_libraries()) == 1
    assert repository.access_grants("reader") == []


def test_restart_controllers(monkeypatch):
    with pytest.raises(RuntimeError):
        DisabledRestartController().request_restart()

    calls: list[tuple] = []

    class Timer:
        daemon = False

        def __init__(self, delay, callback, args):
            calls.append((delay, callback, args))

        def start(self):
            calls.append(("started",))

    monkeypatch.setattr("nineveh.restart.threading.Timer", Timer)
    controller = ProcessRestartController(0.25)
    assert controller.enabled
    controller.request_restart()
    assert calls[0][0] == 0.25
    assert calls[-1] == ("started",)
