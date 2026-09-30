"""Split access, sign-in protection, fair admission and the security feed, end to end."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from conftest import ADMIN_PASSWORD, READER_PASSWORD, authorization
from fakes import FakeCovers
from fastapi.testclient import TestClient

from nineveh import http_api, limits
from nineveh.app import Container, build_container, create_app
from nineveh.domain import SecurityEvent
from nineveh.logins import LoginGuard
from nineveh.security import SignInEvents

PUBLIC = ("https://read.example", ("203.0.113.10", 50000))
TAILNET = ("https://nas.tail.ts.net", ("100.100.10.20", 50000))
LAN = ("https://192.168.7.168:5443", ("192.168.7.50", 50000))
FORGED = ("https://nas.tail.ts.net", ("203.0.113.10", 50000))
SPLIT = {
    "public_base_url": PUBLIC[0],
    "private_base_urls": (TAILNET[0], LAN[0]),
    "private_allow_ips": "100.64.0.0/10,192.168.7.0/24",
    "secure_cookies": True,
}


def wait_for_scan(client: TestClient) -> None:
    for _ in range(300):
        task = client.app.state.scan_task
        if task is not None and task.done():
            return
        time.sleep(0.01)
    raise AssertionError("catalog scan did not complete")


@contextmanager
def front_doors(settings) -> Iterator[dict[str, TestClient]]:
    """One application, reached the way each kind of client reaches it."""
    application = create_app(replace(settings, **SPLIT))
    with TestClient(application, base_url=TAILNET[0], client=TAILNET[1]) as tailnet:
        wait_for_scan(tailnet)
        created = tailnet.post(
            "/api/v1/admin/users",
            headers=authorization(),
            json={"username": "reader", "password": READER_PASSWORD},
        )
        assert created.status_code == 201
        doors = {"tailnet": tailnet}
        for name, (base, client) in {
            "public": PUBLIC,
            "lan": LAN,
            "forged": FORGED,
        }.items():
            doors[name] = TestClient(application, base_url=base, client=client)
        yield doors


@pytest.fixture
def doors(library) -> Iterator[dict[str, TestClient]]:
    settings, _ = library
    with front_doors(settings) as opened:
        yield opened


def events(client: TestClient, kind: str | None = None) -> list[SecurityEvent]:
    return client.app.state.protection.events.recent(kind=kind, limit=100)


READER = authorization("reader", READER_PASSWORD)
ADMIN = authorization()
WRONG_ADMIN = authorization("admin", "not the administrator password")


# --- Split access ------------------------------------------------------------


def test_public_readers_are_served(doors):
    assert (
        doors["public"].get("/opds/v2/catalog.json", headers=READER).status_code == 200
    )


def test_administrators_work_over_the_tailnet_and_the_lan(doors):
    for name in ("tailnet", "lan"):
        response = doors[name].get("/api/v1/admin/users", headers=ADMIN)
        assert response.status_code == 200, name


def test_a_public_admin_password_is_answered_like_a_wrong_one(doors):
    public = doors["public"]
    right = public.get("/opds/v2/catalog.json", headers=ADMIN)
    wrong = public.get("/opds/v2/catalog.json", headers=WRONG_ADMIN)
    assert right.status_code == wrong.status_code == 401
    assert right.json() == wrong.json()
    assert right.headers["www-authenticate"] == wrong.headers["www-authenticate"]
    assert [event.kind for event in events(public)][:2] == [
        "sign_in.failed",
        "sign_in.admin_public",
    ]


def test_a_cached_admin_credential_is_still_refused_publicly(doors):
    assert doors["tailnet"].get("/api/v1/auth/me", headers=ADMIN).status_code == 200
    assert doors["public"].get("/api/v1/auth/me", headers=ADMIN).status_code == 401


def test_a_public_admin_success_still_counts_as_a_failed_attempt(doors):
    protection = doors["public"].app.state.protection
    doors["public"].get("/api/v1/auth/me", headers=ADMIN)
    assert protection.logins.delay(PUBLIC[1][0], "admin") == 1


def test_a_forged_private_hostname_from_outside_is_public(doors):
    forged = doors["forged"]
    assert forged.get("/api/v1/auth/me", headers=ADMIN).status_code == 401
    assert forged.get("/api/v1/admin/users", headers=ADMIN).status_code == 403
    assert forged.get("/api/v1/auth/me", headers=READER).status_code == 200


def test_unrecognized_origins_are_refused(doors):
    response = doors["public"].get(
        "/opds/v2/catalog.json", headers={**READER, "Host": "evil.example"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/admin", "/admin/users", "/docs", "/openapi.json"])
def test_private_pages_are_refused_on_the_public_origin(doors, path: str):
    assert doors["public"].get(path, headers=READER).status_code == 403


def test_a_private_upload_is_refused_before_its_body_is_read(doors):
    def endless():
        yield b"--x\r\n"
        raise AssertionError("the body should never be read")

    response = doors["public"].post(
        "/api/v1/librarian/ingest",
        content=endless(),
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 403


def test_public_browser_sign_in_refuses_administrators_like_a_wrong_password(doors):
    public = doors["public"]
    origin = {"Origin": PUBLIC[0]}
    right = public.post(
        "/login", data={"username": "admin", "password": ADMIN_PASSWORD}, headers=origin
    )
    wrong = public.post(
        "/login",
        data={"username": "admin", "password": "not it at all"},
        headers=origin,
    )
    assert right.status_code == wrong.status_code == 401
    assert "Invalid username or password." in right.text
    reader = public.post(
        "/login",
        data={"username": "reader", "password": READER_PASSWORD},
        headers=origin,
        follow_redirects=False,
    )
    assert reader.status_code == 303


def test_split_sign_in_requires_the_origin_header(doors):
    response = doors["public"].post(
        "/login", data={"username": "reader", "password": READER_PASSWORD}
    )
    assert response.status_code == 403


def test_an_admin_session_is_not_honoured_on_the_public_origin(doors):
    tailnet = doors["tailnet"]
    tailnet.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        headers={"Origin": TAILNET[0]},
    )
    cookie = tailnet.cookies.get(http_api.SESSION_COOKIE)
    assert cookie
    response = doors["public"].get(
        "/api/v1/auth/me", headers={"Cookie": f"{http_api.SESSION_COOKIE}={cookie}"}
    )
    assert response.status_code == 401


def test_readiness_detail_is_private(doors):
    assert set(doors["public"].get("/api/v1/health/ready").json()) == {"status"}
    assert "catalog" in doors["lan"].get("/api/v1/health/ready").json()


def test_links_name_the_origin_the_request_arrived_on(doors):
    public = doors["public"].get("/opds/v2/catalog.json", headers=READER).json()
    private = doors["tailnet"].get("/opds/v2/catalog.json", headers=READER).json()
    assert public["links"][0]["href"].startswith(PUBLIC[0])
    assert private["links"][0]["href"].startswith(TAILNET[0])


def test_the_librarian_api_is_private(doors):
    assert doors["public"].get("/api/v1/librarian/libraries").status_code == 403
    assert doors["forged"].get("/api/v1/librarian/libraries").status_code == 403
    assert doors["tailnet"].get("/api/v1/librarian/libraries").status_code == 401


def test_the_hardening_headers_include_cross_origin_isolation(client: TestClient):
    response = client.get("/api/v1/health/live")
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert "object-src 'none'" in response.headers["content-security-policy"]


# --- Docs ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_are_for_administrators(client: TestClient, reader: dict, path: str):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=reader).status_code == 403
    assert client.get(path, headers=authorization()).status_code == 200


def test_guessing_through_the_docs_is_throttled_and_recorded(client: TestClient):
    protection = client.app.state.protection
    assert client.get("/openapi.json", headers=WRONG_ADMIN).status_code == 401
    assert protection.logins.delay("testclient", "admin") == 1
    assert events(client, "sign_in.failed")[0].detail == {
        "username": "admin",
        "address": "testclient",
    }


# --- Security feed --------------------------------------------------------------


def test_the_security_feed_is_for_administrators(client: TestClient, reader: dict):
    client.get("/api/v1/auth/me", headers=authorization("nobody", "wrong password"))
    listed = client.get("/api/v1/admin/security/events", headers=authorization())
    assert listed.status_code == 200
    assert listed.json()["events"][0]["kind"] == "sign_in.failed"
    assert (
        client.get("/api/v1/admin/security/events", headers=reader).status_code == 403
    )


def test_the_overview_warns_without_split_access_and_lists_signals(client: TestClient):
    client.get("/api/v1/auth/me", headers=authorization("nobody", "wrong password"))
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    page = client.get("/admin").text
    assert "Administration is reachable from any address." in page
    assert "Failed sign-in for “nobody”" in page


def test_the_overview_stops_warning_once_split_access_is_on(doors):
    tailnet = doors["tailnet"]
    tailnet.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        headers={"Origin": TAILNET[0]},
    )
    assert "reachable from any address" not in tailnet.get("/admin").text


def test_the_security_feed_is_bounded(client: TestClient):
    repository = client.app.state.container.repository
    with repository._connect() as connection:
        connection.executemany(
            "INSERT INTO security_events(id, kind, summary, created_at)"
            " VALUES (?, 'probe', ?, ?)",
            (
                (
                    f"id-{n}",
                    f"signal {n}",
                    f"2026-01-01T00:00:{n // 100:02d}.{n % 100:06d}",
                )
                for n in range(5100)
            ),
        )
        count = connection.execute("SELECT COUNT(*) FROM security_events").fetchone()[0]
    assert 4000 < count <= 5000
    assert repository.security_events(limit=1)[0].summary == "signal 5099"


def test_repeated_throttling_is_recorded_once_a_minute(client: TestClient):
    log = client.app.state.protection.events
    assert log.record("account.throttled", "one", once_per="reader") is not None
    assert log.record("account.throttled", "two", once_per="reader") is None
    assert log.record("account.throttled", "three", once_per="other") is not None


# --- Request bodies --------------------------------------------------------------


def test_an_oversized_declared_body_is_refused_before_parsing(library):
    settings, _ = library
    with TestClient(
        create_app(replace(settings, max_request_body_bytes=1024))
    ) as client:
        response = client.post(
            "/login",
            content=b"username=admin&password=" + b"x" * 2000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 413


def test_a_chunked_body_cannot_slip_past_the_limit(library):
    settings, _ = library

    def chunks():
        yield b"username=admin&password="
        for _ in range(4):
            yield b"x" * 512

    with TestClient(
        create_app(replace(settings, max_request_body_bytes=1024))
    ) as client:
        response = client.post(
            "/login",
            content=chunks(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 413


def test_uploads_get_their_own_larger_ceiling(library):
    settings, _ = library
    with TestClient(
        create_app(replace(settings, max_request_body_bytes=1024))
    ) as client:
        response = client.post(
            "/api/v1/librarian/ingest",
            files={"file": ("a.cbz", b"x" * 4096, "application/vnd.comicbook+zip")},
            data={"series_id": "s", "filename": "a.cbz"},
        )
    assert response.status_code == 401  # reached authentication, not refused


def test_a_spooled_upload_stops_at_the_free_space_reserve(library, monkeypatch):
    settings, _ = library
    usage = type("Usage", (), {"free": 1024 * 1024})
    monkeypatch.setattr(limits.shutil, "disk_usage", lambda _path: usage)
    with TestClient(
        create_app(replace(settings, state_free_reserve_bytes=2**30))
    ) as client:
        response = client.post(
            "/api/v1/librarian/ingest",
            files={"file": ("a.cbz", b"x" * 4096, "application/vnd.comicbook+zip")},
            data={"series_id": "s", "filename": "a.cbz"},
        )
    assert response.status_code == 413


# --- Downloads and ranges -----------------------------------------------------------


def test_head_and_ranged_reads_are_not_rationed(
    client: TestClient, publication_id: str
):
    """A streaming reader reads one CBZ through many small Range requests."""
    url = f"/api/v1/publications/{publication_id}/file"
    for _ in range(40):
        ranged = client.get(url, headers={**authorization(), "Range": "bytes=0-63"})
        assert ranged.status_code == 206
        assert client.head(url, headers=authorization()).status_code == 200


def test_a_range_past_the_byte_ceiling_is_refused(library):
    settings, _ = library
    with TestClient(
        create_app(replace(settings, max_range_uncompressed_bytes=10))
    ) as client:
        wait_for_scan(client)
        publication = _first_publication(client)
        response = client.get(
            f"/api/v1/publications/{publication}/range?start=1&end=2",
            headers=authorization(),
        )
    assert response.status_code == 413


def test_ranges_stop_at_the_free_space_reserve(
    client: TestClient, publication_id, monkeypatch
):
    usage = type("Usage", (), {"free": 1024})
    monkeypatch.setattr(http_api.shutil, "disk_usage", lambda _path: usage)
    response = client.get(
        f"/api/v1/publications/{publication_id}/range?start=1&end=2",
        headers=authorization(),
    )
    assert response.status_code == 507
    assert events(client, "storage.reserve")


def test_a_range_that_failed_recovers_once_the_archive_returns(
    client: TestClient, library, publication_id: str
):
    _, archive = library
    url = f"/api/v1/publications/{publication_id}/range?start=1&end=2"
    moved = archive.with_suffix(".away")
    archive.rename(moved)
    assert client.get(url, headers=authorization()).status_code == 404
    moved.rename(archive)
    assert client.get(url, headers=authorization()).status_code == 200


def test_a_runaway_account_is_throttled_and_recorded(library):
    settings, _ = library
    with TestClient(
        create_app(replace(settings, account_requests_per_minute=60))
    ) as client:
        codes = [
            client.get("/api/v1/auth/me", headers=authorization()).status_code
            for _ in range(61)
        ]
        assert codes[:60] == [200] * 60
        refused = client.get("/api/v1/auth/me", headers=authorization())
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) >= 1
        assert events(client, "account.throttled")


def _first_publication(client: TestClient) -> str:
    feed = client.get("/opds/v2/publications.json", headers=authorization()).json()
    return feed["publications"][0]["metadata"]["identifier"].removeprefix("urn:uuid:")


# --- Sign-in protection through HTTP ---------------------------------------------------


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _guarded_client(settings, sleep) -> TestClient:
    container = build_container(settings)
    protection = replace(
        container.protection,
        logins=LoginGuard(SignInEvents(container.protection.events), sleep=sleep),
    )
    return TestClient(create_app(container=replace(container, protection=protection)))


def test_failures_slow_the_next_attempt_and_success_still_waits_its_turn(library):
    settings, _ = library
    sleep = RecordingSleep()
    with _guarded_client(settings, sleep) as client:
        for _ in range(3):
            assert client.get("/api/v1/auth/me", headers=WRONG_ADMIN).status_code == 401
        assert client.get("/api/v1/auth/me", headers=authorization()).status_code == 200
        assert client.get("/api/v1/auth/me", headers=authorization()).status_code == 200
    assert sleep.delays == [1, 2, 4]


def test_a_burst_with_a_fresh_password_is_verified_once(library, monkeypatch):
    """An OPDS app opening its catalog: many requests, one Argon2 check."""
    settings, _ = library
    with TestClient(create_app(settings)) as client:
        auth = client.app.state.container.auth
        calls = []
        verify = auth.verify

        def counted(username, password):
            calls.append(username)
            time.sleep(0.05)
            return verify(username, password)

        monkeypatch.setattr(auth, "verify", counted)
        results = _parallel(
            12, lambda: client.get("/opds/v2/catalog.json", headers=authorization())
        )
    assert [response.status_code for response in results] == [200] * 12
    assert calls == ["admin"]


# --- Fair admission through HTTP -------------------------------------------------------


class SlowCovers(FakeCovers):
    """Covers that take real time to render, and are never cached."""

    def __init__(self, path: Path, seconds: float) -> None:
        super().__init__(path)
        self.seconds = seconds

    def cover(self, publication, page, width):
        time.sleep(self.seconds)
        return super().cover(publication, page, width)


def _parallel(count: int, request):
    results = [None] * count

    def run(index: int) -> None:
        results[index] = request()

    threads = [threading.Thread(target=run, args=(n,)) for n in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


@contextmanager
def _media_client(fake_container: Container, seconds: float) -> Iterator[TestClient]:
    covers = SlowCovers(
        fake_container.settings.state_dir.parent / "cover.webp", seconds
    )
    container = replace(
        fake_container,
        thumbnails=covers,
        settings=replace(fake_container.settings, extract_workers=1),
    )
    with TestClient(create_app(container=container)) as client:
        client.app.state.container.repository.upsert_publication(_fake_publication())
        container.auth.create_user("reader", READER_PASSWORD)
        yield client


def _fake_publication():
    from fakes import page, publication

    from nineveh.domain import ScannedPublication

    return ScannedPublication(publication(), tuple(page(n) for n in (1, 2, 3)))


def _timed(request) -> float:
    started = time.perf_counter()
    response = request()
    assert response.status_code == 200
    return time.perf_counter() - started


def test_queued_image_work_holds_no_thread(fake_container: Container):
    """A backlog waits on the event loop; other readers still get threads."""
    with _media_client(fake_container, 0.4) as client:
        client.portal.call(_limit_threads, 4)
        client.get("/api/v1/auth/me", headers=READER)  # cache the credential
        cover = "/api/v1/publications/fake-id/cover?width=160"
        flood = threading.Thread(
            target=_parallel,
            args=(6, lambda: client.get(cover, headers=authorization())),
        )
        flood.start()
        time.sleep(0.1)
        # A feed page queries SQLite on the shared pool, so it needs a thread.
        feed = "/opds/v2/publications.json"
        elapsed = _timed(lambda: client.get(feed, headers=READER))
        flood.join(30)
    # Had the backlog waited inside the pool, all four threads would be held
    # by renders and this request would wait for one of them to finish.
    assert elapsed < 0.2


def test_a_reader_is_served_before_another_accounts_backlog(fake_container: Container):
    with _media_client(fake_container, 0.2) as client:
        client.get("/api/v1/auth/me", headers=READER)
        cover = "/api/v1/publications/fake-id/cover?width=160"
        flood = threading.Thread(
            target=_parallel,
            args=(8, lambda: client.get(cover, headers=authorization())),
        )
        flood.start()
        time.sleep(0.1)
        elapsed = _timed(lambda: client.get(cover, headers=READER))
        flood.join(30)
    # The backlog is 1.6 s of work; the reader waits for one job, then its own.
    assert elapsed < 0.8


def _limit_threads(tokens: int) -> None:
    anyio.to_thread.current_default_thread_limiter().total_tokens = tokens


# --- Settings the operator owns ------------------------------------------------------


def test_an_administrators_ingest_choice_survives_a_restart(library):
    settings, _ = library
    first = build_container(settings)
    mount = first.mounts.statuses()[0].mount
    first.mounts.update(
        mount.id,
        name=mount.name,
        path=mount.path,
        allow_ingest=False,
        scan_enabled=True,
    )
    restarted = build_container(settings)
    assert restarted.mounts.statuses()[0].mount.allow_ingest is False
