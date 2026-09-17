"""The composition seam: every I/O port can be replaced without touching routes."""

from __future__ import annotations

import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from conftest import ADMIN_PASSWORD, authorization
from fakes import (
    PAGE_BYTES,
    FakeArchives,
    FakeCovers,
    FakePageStore,
    FakeScanner,
    page,
    publication,
)
from fastapi.testclient import TestClient

from nineveh.app import Container, create_app
from nineveh.auth import AuthService
from nineveh.authorization import ReadAllPolicy
from nineveh.config import Settings
from nineveh.database import SQLiteRepository
from nineveh.domain import ScannedPublication
from nineveh.opds import OpdsBuilder


@pytest.fixture
def fake_container(tmp_path: Path) -> Container:
    settings = Settings(
        data_dir=tmp_path,
        state_dir=tmp_path / "state",
        secure_cookies=False,
        scan_interval_seconds=0,
        bootstrap_admin_password=ADMIN_PASSWORD,
    )
    repository = SQLiteRepository(settings.database_path)
    cover = tmp_path / "cover.webp"
    cover.write_bytes(b"fake-cover")
    return Container(
        settings=settings,
        repository=repository,
        auth=AuthService(repository),
        authorization=ReadAllPolicy(),
        scanner=FakeScanner(),
        archives=FakeArchives(),
        thumbnails=FakeCovers(cover),
        page_cache=FakePageStore(),
        opds=OpdsBuilder("Nineveh"),
    )


@pytest.fixture
def fake_client(fake_container: Container) -> TestClient:
    with TestClient(create_app(container=fake_container)) as client:
        fake_container.repository.upsert_publication(
            ScannedPublication(publication(), tuple(page(n) for n in (1, 2, 3)))
        )
        yield client


def test_the_container_propagates_the_worker_ceilings(tmp_path: Path):
    from dataclasses import replace

    from nineveh.app import build_container

    settings = Settings(
        data_dir=tmp_path,
        state_dir=tmp_path / "state",
        hash_workers=3,
        extract_workers=7,
    )
    container = build_container(replace(settings))
    assert all(container.auth._hash_slots.acquire(blocking=False) for _ in range(3))
    assert container.auth._hash_slots.acquire(blocking=False) is False
    assert all(container.page_cache._slots.acquire(blocking=False) for _ in range(7))
    assert container.page_cache._slots.acquire(blocking=False) is False


def test_create_app_derives_settings_from_the_container(fake_container: Container):
    application = create_app(container=fake_container)
    assert application.state.container is fake_container
    assert application.title == fake_container.settings.service_title


def test_routes_serve_entirely_from_fakes(fake_client: TestClient):
    feed = fake_client.get("/opds/v2/publications.json", headers=authorization())
    assert feed.status_code == 200
    assert feed.json()["publications"][0]["metadata"]["title"] == "Issue 1"

    body = fake_client.get(
        "/api/v1/publications/fake-id/pages/2", headers=authorization()
    )
    assert body.status_code == 200
    assert body.content == PAGE_BYTES

    cover = fake_client.get(
        "/api/v1/publications/fake-id/cover", headers=authorization()
    )
    assert cover.status_code == 200
    assert cover.content == b"fake-cover"


def test_page_manifest_uses_the_injected_dimension_source(fake_client: TestClient):
    manifest = fake_client.get(
        "/api/v1/publications/fake-id/pages", headers=authorization()
    ).json()
    assert [(p["width"], p["height"]) for p in manifest["pages"]] == [(120, 180)] * 3


def test_range_download_uses_the_injected_archive(fake_client: TestClient):
    response = fake_client.get(
        "/api/v1/publications/fake-id/range?start=2&end=3", headers=authorization()
    )
    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert archive.namelist() == ["pages/2.png", "pages/3.png"]


def test_scan_endpoint_drives_the_injected_scanner(
    fake_client: TestClient, fake_container: Container
):
    before = fake_container.scanner.runs
    assert (
        fake_client.post(
            "/api/v1/admin/catalog/scan", headers=authorization()
        ).status_code
        == 202
    )
    for _ in range(200):
        if fake_container.scanner.runs > before:
            return
        time.sleep(0.01)
    raise AssertionError("injected scanner was never invoked")


def test_shutdown_closes_the_injected_archive_source(fake_container: Container):
    with TestClient(create_app(container=fake_container)):
        pass
    assert fake_container.archives.closed
