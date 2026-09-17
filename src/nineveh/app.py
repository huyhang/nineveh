from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .archives import (
    ArchiveService,
    PageCacheService,
    PillowThumbnailRenderer,
    ThumbnailService,
)
from .auth import AuthService
from .authorization import AuthorizationPolicy, ReadAllPolicy
from .catalog import ArchiveInspector, CatalogScanner
from .config import Settings
from .database import SQLiteRepository
from .http_api import router as api_router
from .http_web import router as web_router
from .opds import OpdsBuilder
from .ports import ArchiveSource, CatalogScan, CoverSource, PageStore, Repository

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Container:
    """Every I/O seam the HTTP layer depends on, typed against its port."""

    settings: Settings
    repository: Repository
    auth: AuthService
    authorization: AuthorizationPolicy
    scanner: CatalogScan
    archives: ArchiveSource
    thumbnails: CoverSource
    page_cache: PageStore
    opds: OpdsBuilder


def build_container(settings: Settings) -> Container:
    repository = SQLiteRepository(settings.database_path)
    archives = ArchiveService(settings)
    return Container(
        settings=settings,
        repository=repository,
        auth=AuthService(repository, settings.session_hours, settings.hash_workers),
        authorization=ReadAllPolicy(),
        scanner=CatalogScanner(
            settings.data_dir, repository, ArchiveInspector(settings)
        ),
        archives=archives,
        thumbnails=ThumbnailService(
            settings, archives, PillowThumbnailRenderer(settings.max_image_pixels)
        ),
        page_cache=PageCacheService(settings, archives),
        opds=OpdsBuilder(settings.service_title),
    )


def create_app(
    settings: Settings | None = None, container: Container | None = None
) -> FastAPI:
    """Compose the application. Pass a `container` to substitute any I/O seam."""
    configured = settings or (container.settings if container else Settings.from_env())
    container = container or build_container(configured)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        for directory in (
            configured.state_dir,
            configured.thumbnail_dir,
            configured.page_cache_dir,
            configured.range_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        _discard_stale_ranges(configured.range_dir)
        if not configured.data_dir.is_dir():
            raise RuntimeError(f"Data directory does not exist: {configured.data_dir}")
        await asyncio.to_thread(container.repository.initialize)
        if await asyncio.to_thread(container.repository.user_count) == 0:
            await asyncio.to_thread(
                container.auth.bootstrap_admin,
                configured.bootstrap_admin_username,
                configured.admin_password(),
            )
        application.state.start_scan()
        scheduler = asyncio.create_task(_scan_scheduler(application, configured))
        try:
            yield
        finally:
            scheduler.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler
            await _drain_scan(application.state.scan_task)
            container.archives.close()

    application = FastAPI(
        title=configured.service_title,
        version="0.1.0",
        description="An authenticated OPDS 2.0 service for CBZ libraries.",
        lifespan=lifespan,
    )
    application.state.container = container
    application.state.scan_task = None

    def start_scan() -> bool:
        current = application.state.scan_task
        if current is not None and not current.done():
            return False
        application.state.scan_task = asyncio.create_task(_run_scan(container.scanner))
        return True

    application.state.start_scan = start_scan

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if request.url.path in {"/docs", "/redoc"}:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
                "style-src 'self' https://cdn.jsdelivr.net; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "frame-ancestors 'none'",
            )
        else:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html"):
            response.headers.setdefault("Cache-Control", "private, no-store")
        elif request.url.path.startswith("/opds/"):
            response.headers.setdefault("Cache-Control", "private, no-cache")
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    static_path = Path(__file__).parent / "static"
    application.mount("/static", StaticFiles(directory=static_path), name="static")
    application.include_router(api_router)
    application.include_router(web_router)
    return application


def _discard_stale_ranges(directory: Path) -> None:
    """Generated range archives never outlive the response that produced them."""
    for leftover in directory.glob("range-*.cbz"):
        leftover.unlink(missing_ok=True)


async def _drain_scan(scan_task: asyncio.Task | None) -> None:
    """Give an in-flight scan a bounded chance to finish, reporting why it did not."""
    if scan_task is None or scan_task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(scan_task), timeout=10)
    except TimeoutError:
        LOGGER.warning("Catalog scan did not finish within the shutdown grace period")
    except asyncio.CancelledError:
        LOGGER.info("Catalog scan was cancelled during shutdown")
    except Exception:
        LOGGER.exception("Catalog scan failed during shutdown")


async def _run_scan(scanner: CatalogScan) -> None:
    try:
        report = await asyncio.to_thread(scanner.scan)
        LOGGER.info(
            "Catalog scan completed: discovered=%d indexed=%d unchanged=%d removed=%d failed=%d",
            report.discovered,
            report.indexed,
            report.unchanged,
            report.removed,
            report.failed,
        )
    except Exception:
        LOGGER.exception("Background catalog scan failed")


async def _scan_scheduler(application: FastAPI, settings: Settings) -> None:
    if settings.scan_interval_seconds == 0:
        return
    while True:
        await asyncio.sleep(settings.scan_interval_seconds)
        application.state.start_scan()
