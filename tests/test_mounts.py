"""Multiple data mounts: registration, health, scanning and serving.

The invariant most of these protect is the same one: a mount that is
disconnected, missing or read-only changes what Nineveh *shows*, never what
it has indexed and never what is on disk.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ADMIN_PASSWORD, authorization, scanned_client, storage, write_cbz
from fastapi.testclient import TestClient

from nineveh.catalog import InvalidLibrary
from nineveh.config import Settings
from nineveh.database import SCHEMA_VERSION, SQLiteRepository
from nineveh.domain import DEFAULT_MOUNT_ID, CatalogVisibility, MountHealth, ReadScope
from nineveh.search import CatalogSearchService
from nineveh.storage import MountInUse, MountNotFound, StorageError


def _settings(tmp_path: Path, data: Path) -> Settings:
    return Settings(
        data_dir=data,
        state_dir=tmp_path / "state",
        secure_cookies=False,
        scan_interval_seconds=0,
        bootstrap_admin_username="admin",
        bootstrap_admin_password=ADMIN_PASSWORD,
    )


def _series(root: Path, library: str, category: str, name: str) -> Path:
    archive = root / library / category / name / "Issue 1.cbz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    write_cbz(archive)
    return archive


@pytest.fixture
def two_roots(tmp_path: Path):
    """A configured primary root and a second directory ready to register."""
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    _series(primary, "Main Library", "comics", "Example Series")
    _series(secondary, "Archive", "manga", "Other Series")
    settings = _settings(tmp_path, primary)
    return settings, primary, secondary


def _login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _csrf(client: TestClient, path: str = "/admin/libraries") -> str:
    return client.get(path).text.split('name="csrf_token" value="')[1].split('"')[0]


def _wait_for_scan(client: TestClient) -> None:
    for _ in range(300):
        task = client.app.state.scan_task
        if task is None or task.done():
            return
        time.sleep(0.01)
    raise AssertionError("scan did not finish")


# --- The default mount ------------------------------------------------------


def test_the_configured_data_directory_becomes_the_default_mount(two_roots):
    settings, primary, _ = two_roots

    [status] = storage(settings).mounts.statuses()

    assert status.mount.is_default
    assert Path(status.mount.path) == primary.resolve()
    assert status.health is MountHealth.HEALTHY


def test_the_default_mount_follows_a_moved_data_directory(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    storage(_settings(tmp_path, first))

    [status] = storage(_settings(tmp_path, second)).mounts.statuses()

    assert Path(status.mount.path) == second.resolve()


def test_the_default_mount_cannot_be_forgotten(two_roots):
    settings, _, _ = two_roots
    with pytest.raises(StorageError, match="cannot be forgotten"):
        storage(settings).mounts.forget("default")


# --- Registering a mount ----------------------------------------------------


def test_a_registered_mount_is_resolved_and_healthy(two_roots):
    settings, _, secondary = two_roots
    service = storage(settings).mounts

    mount = service.add("Archive drive", str(secondary))

    assert Path(mount.path) == secondary.resolve()
    assert mount.allow_ingest is False, "mounts are read-only until asked otherwise"
    assert service.status(mount).health is MountHealth.HEALTHY


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("relative/path", "absolute path"),
        ("/nineveh-does-not-exist", "does not exist"),
        ("/", "filesystem root"),
        ("/etc", "protected system directory"),
        ("/proc", "protected system directory"),
        ("/run/secrets", "protected system directory"),
        ("/usr", "system directory"),
    ],
)
def test_unusable_paths_are_refused_with_a_reason(two_roots, path, reason):
    settings, _, _ = two_roots
    with pytest.raises(StorageError, match=reason):
        storage(settings).mounts.add("Bad", path)


def test_a_file_cannot_be_registered_as_a_mount(two_roots, tmp_path: Path):
    settings, _, _ = two_roots
    plain = tmp_path / "not-a-directory"
    plain.write_text("x")
    with pytest.raises(StorageError, match="must be a directory"):
        storage(settings).mounts.add("File", str(plain))


def test_a_symlinked_path_is_refused(two_roots, tmp_path: Path):
    settings, _, secondary = two_roots
    link = tmp_path / "link"
    link.symlink_to(secondary)
    with pytest.raises(StorageError, match="symbolic link"):
        storage(settings).mounts.add("Link", str(link))


def test_a_mount_inside_another_mount_is_refused(two_roots):
    settings, primary, _ = two_roots
    nested = primary / "Main Library"
    with pytest.raises(StorageError, match="overlaps with"):
        storage(settings).mounts.add("Nested", str(nested))


def test_the_state_directory_is_refused(two_roots):
    settings, _, _ = two_roots
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(StorageError, match="state directory"):
        storage(settings).mounts.add("State", str(settings.state_dir))


def test_a_duplicate_label_or_path_is_refused(two_roots, tmp_path: Path):
    settings, _, secondary = two_roots
    third = tmp_path / "third"
    third.mkdir()
    service = storage(settings).mounts
    service.add("Archive", str(secondary))

    with pytest.raises(ValueError, match="already called"):
        service.add("Archive", str(third))
    with pytest.raises(StorageError, match="overlaps with Archive"):
        service.add("Another", str(secondary))


def test_ingest_requires_a_writable_path(two_roots, tmp_path: Path):
    settings, _, _ = two_roots
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(StorageError, match="writable"):
            storage(settings).mounts.add("Locked", str(locked), allow_ingest=True)
    finally:
        locked.chmod(0o700)


def test_an_unknown_mount_is_reported_as_missing_not_invalid(two_roots):
    settings, _, _ = two_roots
    service = storage(settings).mounts
    for call in (
        lambda: service.disconnect("nope"),
        lambda: service.reconnect("nope"),
        lambda: service.forget("nope"),
    ):
        with pytest.raises(MountNotFound):
            call()


# --- Libraries across mounts ------------------------------------------------


def test_a_new_mount_offers_its_directories_rather_than_adopting_them(two_roots):
    """The original root is adopted on a fresh install; later mounts are not,
    so leaving a directory out of the catalog stays left out."""
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))

    graph.libraries.initialize()

    assert [item.name for item in graph.repository.managed_libraries()] == [
        "Main Library"
    ]
    assert graph.libraries.available(mount.id) == ["Archive"]
    graph.libraries.add("Archive", mount.id)
    assert graph.libraries.available(mount.id) == []


def test_the_same_folder_name_on_two_mounts_needs_an_alias(two_roots, tmp_path: Path):
    """Display names are what readers and grants see, so they stay unique."""
    settings, _, secondary = two_roots
    _series(secondary, "Main Library", "comics", "Second Series")
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))

    with pytest.raises(InvalidLibrary, match="already called"):
        graph.libraries.add("Main Library", mount.id)

    aliased = graph.libraries.add("Main Library", mount.id, "Main Library (archive)")
    assert aliased.relative_path == "Main Library"
    assert aliased.name == "Main Library (archive)"


def test_two_mounts_can_hold_the_same_relative_path(two_roots):
    settings, _, secondary = two_roots
    second_archive = _series(secondary, "Main Library", "comics", "Example Series")
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    aliased = graph.libraries.add("Main Library", mount.id, "Archived")

    report = graph.scanner(settings).scan()
    publications, total = graph.repository.publications(limit=10)

    assert (report.indexed, total) == (2, 2)
    assert {item.library for item in publications} == {"Main Library", "Archived"}
    archived = next(item for item in publications if item.library_id == aliased.id)
    assert graph.archives(settings).archive_path(archived) == second_archive


def test_a_library_cannot_be_added_to_a_disconnected_mount(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    graph.mounts.disconnect(mount.id)

    with pytest.raises(InvalidLibrary, match="connected data mount"):
        graph.libraries.add("Archive", mount.id)


def test_only_connected_mounts_offer_directories(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    assert mount.id in graph.libraries.available_by_mount()

    graph.mounts.disconnect(mount.id)
    assert mount.id not in graph.libraries.available_by_mount()


# --- Scanning ---------------------------------------------------------------


def test_one_mount_can_be_scanned_without_touching_the_others(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    graph.libraries.add("Archive", mount.id)
    scanner = graph.scanner(settings)
    scanner.scan()

    report = scanner.scan_mount(mount.id)

    assert (report.discovered, report.unchanged) == (1, 1)
    assert graph.repository.publications(limit=10)[1] == 2
    with pytest.raises(InvalidLibrary, match="Data mount not found"):
        scanner.scan_mount("nope")


def test_a_disconnected_mount_is_skipped_and_keeps_its_index(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    graph.libraries.add("Archive", mount.id)
    scanner = graph.scanner(settings)
    scanner.scan()
    assert graph.repository.publications(limit=10)[1] == 2

    graph.mounts.disconnect(mount.id)
    report = scanner.scan()

    assert report.removed == 0
    # Hidden from readers, still on the books.
    assert graph.repository.publications(limit=10)[1] == 1
    assert (
        graph.repository.publication_by_path("Archive/manga/Other Series/Issue 1.cbz")
        is not None
    )
    graph.mounts.reconnect(mount.id)
    assert graph.repository.publications(limit=10)[1] == 2


def test_a_mount_excluded_from_scheduled_scans_is_left_alone(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    graph.libraries.add("Archive", mount.id)
    graph.scanner(settings).scan()
    graph.mounts.update(
        mount.id,
        name="Archive drive",
        path=str(secondary),
        allow_ingest=False,
        scan_enabled=False,
    )

    report = graph.scanner(settings).scan()

    assert report.discovered == 1, "only the primary mount was walked"
    assert graph.repository.publications(limit=10)[1] == 2


def test_storage_that_disappears_is_reported_not_pruned(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    graph.libraries.add("Archive", mount.id)
    scanner = graph.scanner(settings)
    scanner.scan()
    secondary.rename(secondary.with_name("unplugged"))

    report = scanner.scan()

    assert (report.failed, report.removed) == (1, 0)
    assert [(item.library, item.path) for item in report.failures] == [
        ("Archive", None)
    ]
    assert graph.repository.publications(limit=10)[1] == 2
    assert graph.mounts.status(mount).health is MountHealth.MISSING

    secondary.with_name("unplugged").rename(secondary)
    assert scanner.scan_mount(mount.id).unchanged == 1


# --- Serving and ingest -----------------------------------------------------


def test_a_disconnected_mount_stops_resolving_its_publications(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    graph.libraries.add("Archive", mount.id)
    graph.scanner(settings).scan()
    stored = graph.repository.publication_by_path(
        "Archive/manga/Other Series/Issue 1.cbz"
    )
    graph.mounts.disconnect(mount.id)

    with pytest.raises(StorageError, match="disconnected"):
        graph.paths.publication_path(stored)


def test_ingest_is_refused_on_a_read_only_mount(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    library = graph.libraries.add("Archive", mount.id)

    with pytest.raises(StorageError, match="read-only"):
        graph.paths.ingest_path(library, "manga", "Other Series", "Issue 2.cbz")

    graph.mounts.update(
        mount.id,
        name="Archive drive",
        path=str(secondary),
        allow_ingest=True,
        scan_enabled=True,
    )
    target = graph.paths.ingest_path(library, "manga", "Other Series", "Issue 2.cbz")
    assert target.is_relative_to(secondary.resolve())


@pytest.mark.parametrize("relative_path", ["../escape", "nested/child", "..", ""])
def test_a_library_path_that_is_not_a_direct_child_is_refused(two_roots, relative_path):
    """Defence in depth: the resolver re-checks what the database stored."""
    settings, _, _ = two_roots
    graph = storage(settings)
    [library] = graph.repository.managed_libraries()

    with pytest.raises(StorageError):
        graph.paths.library_root(replace(library, relative_path=relative_path))


def test_a_publication_path_cannot_escape_its_mount(two_roots):
    settings, _, _ = two_roots
    graph = storage(settings)
    graph.scanner(settings).scan()
    [stored] = graph.repository.publications(limit=10)[0]

    with pytest.raises(StorageError, match="unsafe"):
        graph.paths.publication_path(replace(stored, relative_path="../escape.cbz"))


# --- Forgetting -------------------------------------------------------------


def test_forgetting_needs_a_disconnect_first_and_spares_the_media(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    graph.libraries.add("Archive", mount.id)
    graph.scanner(settings).scan()

    with pytest.raises(MountInUse, match="Disconnect"):
        graph.mounts.forget(mount.id)

    graph.mounts.disconnect(mount.id)
    graph.mounts.forget(mount.id)

    assert graph.repository.data_mount(mount.id) is None
    assert graph.repository.publications(limit=10)[1] == 1
    assert (secondary / "Archive" / "manga" / "Other Series" / "Issue 1.cbz").is_file()


# --- Reader visibility ------------------------------------------------------


def test_a_disconnected_mount_hides_its_series_from_readers(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    graph.libraries.add("Archive", mount.id)
    graph.scanner(settings).scan()
    assert len(graph.repository.catalog_series(visibility=CatalogVisibility.ALL)) == 2

    graph.mounts.disconnect(mount.id)

    assert len(graph.repository.catalog_series(visibility=CatalogVisibility.ALL)) == 1


# --- HTTP -------------------------------------------------------------------


def test_the_admin_api_manages_a_mount_end_to_end(two_roots):
    settings, _, secondary = two_roots
    for client in scanned_client(settings):
        headers = authorization()
        created = client.post(
            "/api/v1/admin/mounts",
            headers=headers,
            json={"name": "Archive drive", "path": str(secondary)},
        )
        assert created.status_code == 201
        mount = created.json()
        assert mount["health"] == "healthy"
        assert mount["allowIngest"] is False

        listed = client.get("/api/v1/admin/libraries", headers=headers).json()
        assert [item["name"] for item in listed["mounts"]] == [
            "Primary",
            "Archive drive",
        ]
        assert listed["mounts"][1]["availableDirectories"] == ["Archive"]

        added = client.post(
            "/api/v1/admin/libraries",
            headers=headers,
            json={"relative_path": "Archive", "mount_id": mount["id"]},
        )
        assert added.status_code == 201
        assert added.json()["mountId"] == mount["id"]
        _wait_for_scan(client)

        assert (
            client.post(
                f"/api/v1/admin/mounts/{mount['id']}/scan", headers=headers
            ).status_code
            == 202
        )
        _wait_for_scan(client)
        usage = client.get("/api/v1/admin/libraries", headers=headers).json()
        assert usage["mounts"][1]["publicationCount"] == 1
        break


def test_mount_refusals_use_distinguishable_status_codes(two_roots):
    """404 means "no such mount"; 422 means "that value will not do"."""
    settings, _, secondary = two_roots
    for client in scanned_client(settings):
        headers = authorization()
        mount = client.post(
            "/api/v1/admin/mounts",
            headers=headers,
            json={"name": "Archive drive", "path": str(secondary)},
        ).json()
        body = {
            "name": "Archive drive",
            "path": "relative/path",
            "allow_ingest": False,
            "scan_enabled": True,
        }

        assert (
            client.post("/api/v1/admin/mounts", headers=headers, json=body).status_code
            == 422
        )
        assert (
            client.put(
                f"/api/v1/admin/mounts/{mount['id']}", headers=headers, json=body
            ).status_code
            == 422
        ), "a bad path is not a missing mount"
        assert (
            client.put(
                "/api/v1/admin/mounts/nope",
                headers=headers,
                json={**body, "path": str(secondary)},
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/v1/admin/mounts/{mount['id']}", headers=headers
            ).status_code
            == 409
        ), "a connected mount has to be disconnected first"
        client.post(f"/api/v1/admin/mounts/{mount['id']}/disconnect", headers=headers)
        assert (
            client.delete(
                f"/api/v1/admin/mounts/{mount['id']}", headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post("/api/v1/admin/mounts/nope/scan", headers=headers).status_code
            == 404
        )
        break


def test_the_browser_manages_mounts_without_javascript(two_roots):
    settings, _, secondary = two_roots
    for client in scanned_client(settings):
        _login(client)
        token = _csrf(client)

        added = client.post(
            "/admin/mounts",
            data={
                "name": "Archive drive",
                "path": str(secondary),
                "scan_enabled": "true",
                "csrf_token": token,
            },
            follow_redirects=True,
        )
        assert "Added Archive drive" in added.text
        assert "Archive drive" in client.get("/admin/libraries").text

        mount = next(
            item
            for item in client.app.state.container.repository.data_mounts()
            if item.name == "Archive drive"
        )
        library = client.post(
            "/admin/libraries",
            data={
                "relative_path": "Archive",
                "mount_id": mount.id,
                "name": "Archived manga",
                "csrf_token": token,
            },
            follow_redirects=True,
        )
        assert "Added Archived manga" in library.text
        _wait_for_scan(client)

        page = client.get("/admin/libraries").text
        assert "Archived manga" in page
        assert 'class="pill healthy"' in page

        disconnected = client.post(
            f"/admin/mounts/{mount.id}/disconnect",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert "catalog history was kept" in disconnected.text
        assert 'class="pill disconnected"' in client.get("/admin/libraries").text
        break


def test_the_browser_reports_a_bad_mount_path_instead_of_failing(two_roots):
    settings, _, _ = two_roots
    for client in scanned_client(settings):
        _login(client)
        response = client.post(
            "/admin/mounts",
            data={
                "name": "Bad",
                "path": "/etc",
                "csrf_token": _csrf(client),
            },
            follow_redirects=True,
        )
        assert "protected system directory" in response.text
        break


def test_a_missing_primary_data_directory_stops_the_service(tmp_path: Path):
    """A mistyped bind mount must not look like an empty library."""
    settings = _settings(tmp_path, tmp_path / "never-created")
    with pytest.raises(RuntimeError, match="Data directory does not exist"):
        for _ in scanned_client(settings):
            pass


def test_the_librarian_places_a_volume_on_the_series_own_mount(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary), allow_ingest=True)
    graph.libraries.add("Archive", mount.id)
    graph.scanner(settings).scan()
    [series] = [
        item
        for item in graph.repository.catalog_series(visibility=CatalogVisibility.ALL)
        if item.category == "manga"
    ]
    library = graph.repository.managed_library(series.library_id)

    target = graph.paths.ingest_path(library, "manga", series.name, "Issue 2.cbz")

    assert target.parent == secondary.resolve() / "Archive" / "manga" / series.name


def test_ingest_is_refused_once_the_mount_is_disconnected(two_roots):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary), allow_ingest=True)
    library = graph.libraries.add("Archive", mount.id)
    graph.mounts.disconnect(mount.id)

    with pytest.raises(StorageError, match="disconnected"):
        graph.paths.ingest_path(library, "manga", "Other Series", "Issue 2.cbz")


def test_an_upgraded_database_keeps_its_libraries_on_the_default_mount(two_roots):
    settings, _, _ = two_roots
    first = storage(settings)
    first.scanner(settings).scan()
    names = {item.name for item in first.repository.managed_libraries()}

    second = storage(settings)

    assert {item.name for item in second.repository.managed_libraries()} == names
    assert all(
        item.mount_id == "default" for item in second.repository.managed_libraries()
    )
    assert second.repository.publications(limit=10)[1] == 1


def test_a_re_added_library_keeps_its_identity(two_roots):
    settings, _, _ = two_roots
    graph = storage(settings)
    [library] = graph.repository.managed_libraries()
    graph.libraries.remove(library.id)

    restored = graph.libraries.add(library.relative_path)

    assert restored.id == library.id


# --- Remaining edges --------------------------------------------------------


def test_a_library_whose_mount_vanished_reports_rather_than_crashes(two_roots):
    """The database can outlive a mount row; the resolver says so plainly."""
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary))
    library = graph.libraries.add("Archive", mount.id)
    graph.repository.forget_data_mount(mount.id)

    with pytest.raises(StorageError, match="no longer configured"):
        graph.paths.library_root(replace(library, mount_id=mount.id))


def test_a_publication_whose_library_vanished_reports_rather_than_crashes(two_roots):
    settings, _, _ = two_roots
    graph = storage(settings)
    graph.scanner(settings).scan()
    [stored] = graph.repository.publications(limit=10)[0]

    with pytest.raises(StorageError, match="library is no longer configured"):
        graph.paths.publication_path(replace(stored, library_id="gone"))


def test_an_unreadable_directory_is_refused(two_roots, tmp_path: Path):
    settings, _, _ = two_roots
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o000)
    try:
        with pytest.raises(StorageError, match="not readable"):
            storage(settings).mounts.add("Blocked", str(blocked))
    finally:
        blocked.chmod(0o700)


@pytest.mark.parametrize("label", ["", "   ", "x" * 81])
def test_a_mount_label_has_to_be_usable(two_roots, label):
    settings, _, secondary = two_roots
    with pytest.raises(StorageError, match="1-80 characters"):
        storage(settings).mounts.add(label, str(secondary))


def test_an_ingest_enabled_mount_warns_when_it_turns_read_only(
    two_roots, tmp_path: Path
):
    settings, _, _ = two_roots
    writable = tmp_path / "writable"
    writable.mkdir()
    graph = storage(settings)
    mount = graph.mounts.add("Drop box", str(writable), allow_ingest=True)
    assert graph.mounts.status(mount).detail is None

    writable.chmod(0o500)
    try:
        assert "not writable" in graph.mounts.status(mount).detail
    finally:
        writable.chmod(0o700)


def test_ingest_refuses_to_follow_a_symlinked_series_directory(two_roots):
    """Even a link that stays inside the mount: placement follows real paths."""
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary), allow_ingest=True)
    library = graph.libraries.add("Archive", mount.id)
    category = secondary / "Archive" / "manga"
    (category / "Linked Series").symlink_to(category / "Other Series")

    with pytest.raises(StorageError, match="Symbolic links"):
        graph.paths.ingest_path(library, "manga", "Linked Series", "Issue 2.cbz")


def test_ingest_refuses_a_link_that_leaves_the_mount(two_roots, tmp_path: Path):
    settings, _, secondary = two_roots
    graph = storage(settings)
    mount = graph.mounts.add("Archive drive", str(secondary), allow_ingest=True)
    library = graph.libraries.add("Archive", mount.id)
    outside = tmp_path / "outside"
    outside.mkdir()
    (secondary / "Archive" / "manga" / "Escape").symlink_to(outside)

    with pytest.raises(StorageError, match="escapes its mount"):
        graph.paths.ingest_path(library, "manga", "Escape", "Issue 2.cbz")


# --- Upgrading a deployed database ------------------------------------------


def _v9_database(path: Path) -> Path:
    """A database as the last single-mount release left it.

    Only the parts the mount upgrade has to rewrite: `managed_libraries` with
    no `mount_id` and a bare-path UNIQUE, and `publications` with globally
    unique paths. Both constraints have to move, and SQLite can drop neither.
    """
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE managed_libraries (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                relative_path TEXT NOT NULL COLLATE NOCASE UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE catalog_series (
                id TEXT PRIMARY KEY,
                library_id TEXT NOT NULL REFERENCES managed_libraries(id)
                    ON DELETE CASCADE,
                category TEXT NOT NULL COLLATE NOCASE,
                name TEXT NOT NULL COLLATE NOCASE,
                is_private INTEGER NOT NULL DEFAULT 0,
                UNIQUE(library_id, category, name)
            );
            CREATE TABLE publications (
                id TEXT PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE,
                library TEXT NOT NULL, category TEXT NOT NULL, series TEXT NOT NULL,
                filename TEXT NOT NULL, title TEXT NOT NULL, number TEXT,
                description TEXT, authors_json TEXT NOT NULL DEFAULT '[]',
                modified_ns INTEGER NOT NULL, size INTEGER NOT NULL,
                revision TEXT NOT NULL, page_count INTEGER NOT NULL,
                cover_page INTEGER NOT NULL DEFAULT 1,
                library_id TEXT REFERENCES managed_libraries(id),
                series_id TEXT REFERENCES catalog_series(id)
            );
            CREATE TABLE access_grants (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                library_id TEXT NOT NULL REFERENCES managed_libraries(id)
                    ON DELETE CASCADE,
                category TEXT NOT NULL DEFAULT '',
                series_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(user_id, library_id, category, series_id)
            );
            INSERT INTO users VALUES
                ('reader', 'reader', 'hash', 0, 1, '2026-01-01T00:00:00+00:00');
            INSERT INTO managed_libraries VALUES
                ('lib', 'Main Library', 'Main Library', 1, '2026-01-01T00:00:00+00:00');
            INSERT INTO catalog_series VALUES ('ser', 'lib', 'comics', 'Example Series', 0);
            INSERT INTO publications VALUES (
                'pub', 'Main Library/comics/Example Series/One.cbz', 'Main Library',
                'comics', 'Example Series', 'One.cbz', 'The First Issue', '1', NULL,
                '[]', 1, 4096, 'revision', 1, 1, 'lib', 'ser'
            );
            INSERT INTO access_grants VALUES ('reader', 'lib', '', '');
            PRAGMA user_version = 9;
            """
        )
        connection.commit()
    return path


def test_upgrading_a_deployed_database_moves_it_onto_the_default_mount(tmp_path: Path):
    """The upgrade an existing Docker deployment actually performs."""
    repository = SQLiteRepository(_v9_database(tmp_path / "state.sqlite3"))

    repository.initialize()

    [library] = repository.managed_libraries()
    assert library.mount_id == DEFAULT_MOUNT_ID
    assert library.name == "Main Library"
    # Nothing the reader owned moved.
    assert [grant.library_id for grant in repository.access_grants("reader")] == ["lib"]
    assert repository.publication_by_id("pub") is not None
    assert repository.publications(limit=10)[1] == 1


def test_the_upgrade_indexes_what_is_already_there_without_a_rescan(tmp_path: Path):
    repository = SQLiteRepository(_v9_database(tmp_path / "state.sqlite3"))

    repository.initialize()

    found = CatalogSearchService(repository).search(
        "Example", scope=ReadScope(unrestricted=True), user_id="reader"
    )
    assert [hit.title for hit in found.results] == ["Example Series"]
    volumes = CatalogSearchService(repository).search(
        "First Issue", scope=ReadScope(unrestricted=True), user_id="reader"
    )
    assert volumes.total == 1


def test_the_upgrade_rescopes_both_uniqueness_constraints(tmp_path: Path):
    """The point of the rebuild: two disks may repeat a name and a path."""
    path = _v9_database(tmp_path / "state.sqlite3")
    repository = SQLiteRepository(path)
    repository.initialize()

    second = repository.add_data_mount("Archive", str(tmp_path / "archive"))
    aliased = repository.add_library(
        "Main Library", second.id, "Main Library (archive)"
    )

    assert aliased.relative_path == "Main Library"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO catalog_series(id, library_id, category, name, is_private)"
            " VALUES (?, ?, 'comics', 'Example Series', 0)",
            ("ser2", aliased.id),
        )
        connection.execute(
            "INSERT INTO publications(id, relative_path, library, category, series,"
            " filename, title, number, description, authors_json, modified_ns, size,"
            " revision, page_count, cover_page, library_id, series_id)"
            " VALUES (?, ?, ?, 'comics', 'Example Series',"
            " 'One.cbz', 'The First Issue', '1', NULL, '[]', 1, 4096, 'rev', 1, 1, ?, ?)",
            (
                "pub2",
                "Main Library/comics/Example Series/One.cbz",
                "Main Library (archive)",
                aliased.id,
                "ser2",
            ),
        )
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert repository.publications(limit=10)[1] == 2


def test_upgrading_twice_changes_nothing_the_second_time(tmp_path: Path):
    path = _v9_database(tmp_path / "state.sqlite3")
    SQLiteRepository(path).initialize()
    first = SQLiteRepository(path).managed_libraries()

    SQLiteRepository(path).initialize()

    assert SQLiteRepository(path).managed_libraries() == first
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_an_older_release_refuses_the_upgraded_database(tmp_path: Path):
    """Rolling the image back has to fail loudly, not read it half-right."""
    path = _v9_database(tmp_path / "state.sqlite3")
    SQLiteRepository(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()

    with pytest.raises(RuntimeError, match="newer than this Nineveh release"):
        SQLiteRepository(path).initialize()
