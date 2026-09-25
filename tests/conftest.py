from __future__ import annotations

import base64
import io
import time
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import (
    FakeArchives,
    FakeCovers,
    FakePageStore,
    FakeRenditions,
    FakeRestartController,
    FakeScanner,
    page,
    publication,
)
from fastapi.testclient import TestClient
from PIL import Image

from nineveh.app import Container, create_app
from nineveh.auth import AuthService
from nineveh.authorization import AccessService, ReadAllPolicy
from nineveh.catalog import ArchiveInspector, LibraryService, SeriesService
from nineveh.config import Settings, SettingsService
from nineveh.database import SQLiteRepository
from nineveh.domain import ScannedPublication
from nineveh.librarian import (
    AuditTrail,
    IngestService,
    LibrarianAuth,
    LibrarianService,
)
from nineveh.opds import OpdsBuilder
from nineveh.reader import ReaderService

ADMIN_PASSWORD = "correct horse battery staple"
READER_PASSWORD = "a sufficiently long password"


def authorization(username: str = "admin", password: str = ADMIN_PASSWORD) -> dict:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def image_bytes(color: tuple[int, int, int], size: tuple[int, int] = (40, 60)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


COMIC_INFO = """<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Title>The First Issue</Title>
  <Series>Example Series</Series>
  <Number>1</Number>
  <Writer>A. Writer</Writer>
  <Summary>An example publication.</Summary>
  <Pages><Page Image="0" Type="FrontCover" /></Pages>
</ComicInfo>
"""


def write_cbz(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("pages/10.png", image_bytes((0, 0, 255)))
        archive.writestr("pages/2.png", image_bytes((0, 255, 0)))
        archive.writestr("pages/1.png", image_bytes((255, 0, 0)))
        archive.writestr("ComicInfo.xml", COMIC_INFO)


@pytest.fixture
def library(tmp_path: Path) -> tuple[Settings, Path]:
    data = tmp_path / "data"
    state = tmp_path / "state"
    series = data / "Main Library" / "comics" / "Example Series"
    series.mkdir(parents=True)
    archive = series / "Issue 1.cbz"
    write_cbz(archive)
    settings = Settings(
        data_dir=data,
        state_dir=state,
        secure_cookies=False,
        scan_interval_seconds=0,
        bootstrap_admin_username="admin",
        bootstrap_admin_password=ADMIN_PASSWORD,
    )
    return settings, archive


def wait_for_scan(client: TestClient) -> None:
    for _ in range(200):
        status = client.get("/api/v1/health/ready").json()["catalog"]
        if status["completed_at"]:
            return
        time.sleep(0.01)
    raise AssertionError("catalog scan did not complete")


def scanned_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        wait_for_scan(client)
        yield client


@pytest.fixture
def client(library) -> Iterator[TestClient]:
    settings, _ = library
    yield from scanned_client(settings)


@pytest.fixture
def streaming_client(library) -> Iterator[TestClient]:
    """A client whose page cache is disabled, so pages take the streaming path."""
    settings, _ = library
    yield from scanned_client(replace(settings, page_cache_mb=0))


@pytest.fixture
def publication_id(client: TestClient) -> str:
    feed = client.get("/opds/v2/publications.json", headers=authorization()).json()
    identifier = feed["publications"][0]["metadata"]["identifier"]
    return identifier.removeprefix("urn:uuid:")


@pytest.fixture
def reader(client: TestClient) -> dict:
    """A second, non-administrative account."""
    response = client.post(
        "/api/v1/admin/users",
        headers=authorization(),
        json={"username": "reader", "password": READER_PASSWORD},
    )
    assert response.status_code == 201
    return authorization("reader", READER_PASSWORD)


@pytest.fixture
def fake_container(tmp_path: Path) -> Container:
    """Every I/O port replaced by a fake from `fakes`, over a real database."""
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
    audit = AuditTrail(repository)
    return Container(
        settings=settings,
        repository=repository,
        auth=AuthService(repository),
        authorization=ReadAllPolicy(),
        scanner=FakeScanner(),
        archives=FakeArchives(),
        thumbnails=FakeCovers(cover),
        renditions=FakeRenditions(),
        page_cache=FakePageStore(),
        opds=OpdsBuilder("Nineveh"),
        access=AccessService(repository),
        libraries=LibraryService(settings.data_dir, repository),
        series=SeriesService(repository),
        configuration=SettingsService(settings, repository),
        restarter=FakeRestartController(enabled=False),
        reader=ReaderService(repository, repository),
        audit=audit,
        librarian_auth=LibrarianAuth(repository, audit),
        librarian=LibrarianService(repository),
        ingest=IngestService(
            settings.data_dir,
            settings.ingest_staging_dir,
            repository,
            ArchiveInspector(settings),
            settings.max_upload_bytes,
        ),
    )


@pytest.fixture
def fake_client(fake_container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(container=fake_container)) as client:
        fake_container.repository.upsert_publication(
            ScannedPublication(publication(), tuple(page(n) for n in (1, 2, 3)))
        )
        yield client
