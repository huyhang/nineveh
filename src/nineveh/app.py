from __future__ import annotations

import asyncio
import logging
import tempfile
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .archives import (
    ArchiveService,
    PageCacheService,
    PillowThumbnailRenderer,
    ThumbnailService,
)
from .auth import AuthService
from .authorization import AccessService, AuthorizationPolicy, GrantPolicy
from .catalog import ArchiveInspector, CatalogScanner, LibraryService
from .config import Settings, SettingsService
from .database import SQLiteRepository
from .deployment import discarded_forwarded_proto, proxy_trust_advice
from .http_api import router as api_router
from .http_web import router as web_router
from .metadata import (
    MangaBakaProvider,
    MetadataCoverStore,
    MetadataService,
    PersistentRateLimiter,
    UrllibTransport,
)
from .opds import OpdsBuilder
from .ports import (
    ArchiveSource,
    CatalogScan,
    CoverSource,
    PageStore,
    Repository,
    RestartController,
)
from .restart import DisabledRestartController, ProcessRestartController

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
    access: AccessService
    libraries: LibraryService
    configuration: SettingsService
    restarter: RestartController
    metadata: MetadataService | None = None


def build_container(settings: Settings) -> Container:
    repository = SQLiteRepository(settings.database_path)
    # Persisted settings decide how the rest of the graph is built, so the
    # schema has to exist before anything else is constructed. `initialize` is
    # idempotent; the lifespan calls it again for containers built by hand.
    repository.initialize()
    configuration = SettingsService(settings, repository)
    effective = configuration.activate()
    archives = ArchiveService(effective)
    limiter = PersistentRateLimiter(repository, configuration.mangabaka_request_limit)
    return Container(
        settings=effective,
        repository=repository,
        auth=AuthService(repository, effective.session_hours, effective.hash_workers),
        authorization=GrantPolicy(repository),
        scanner=CatalogScanner(
            effective.data_dir, repository, ArchiveInspector(effective)
        ),
        archives=archives,
        thumbnails=ThumbnailService(
            effective,
            archives,
            PillowThumbnailRenderer(effective.max_image_pixels),
        ),
        page_cache=PageCacheService(effective, archives),
        opds=OpdsBuilder(effective.service_title),
        access=AccessService(repository),
        libraries=LibraryService(effective.data_dir, repository),
        configuration=configuration,
        restarter=ProcessRestartController()
        if effective.restart_enabled
        else DisabledRestartController(),
        metadata=MetadataService(
            repository,
            MangaBakaProvider(UrllibTransport(), limiter),
            MetadataCoverStore(
                effective.metadata_cover_dir, effective.max_image_pixels
            ),
        ),
    )


def create_app(
    settings: Settings | None = None, container: Container | None = None
) -> FastAPI:
    """Compose the application. Pass a `container` to substitute any I/O seam."""
    defaults = settings or (container.settings if container else Settings.from_env())
    container = container or build_container(defaults)
    configured = container.settings

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        for directory in (
            configured.state_dir,
            configured.thumbnail_dir,
            configured.page_cache_dir,
            configured.range_dir,
            configured.metadata_cover_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        _discard_stale_ranges(configured.range_dir)
        _discard_flat_candidate_covers(configured.metadata_cover_dir)
        if not configured.data_dir.is_dir():
            raise RuntimeError(f"Data directory does not exist: {configured.data_dir}")
        advice = await asyncio.to_thread(
            proxy_trust_advice, configured.forwarded_allow_ips
        )
        if advice:
            LOGGER.warning("%s", advice)
        await asyncio.to_thread(container.repository.initialize)
        await asyncio.to_thread(container.libraries.initialize)
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
            await _drain_scan(application.state.metadata_task)
            container.archives.close()

    application = FastAPI(
        title=configured.service_title,
        version=__version__,
        description="An authenticated OPDS 2.0 service for CBZ libraries.",
        lifespan=lifespan,
    )
    application.state.container = container
    application.state.scan_task = None
    application.state.metadata_task = None
    application.state.metadata_status = {
        "running": False,
        "completed": 0,
        "total": 0,
        "failed": 0,
    }
    # Set by the middleware the first time a proxy's X-Forwarded-Proto is
    # discarded, so the admin console can name the address to trust.
    application.state.untrusted_proxy = None

    def start_scan(library_id: str | None = None) -> bool:
        current = application.state.scan_task
        if current is not None and not current.done():
            return False
        application.state.scan_task = asyncio.create_task(
            _run_scan(container.scanner, library_id)
        )
        return True

    application.state.start_scan = start_scan

    def start_metadata_lookup(series_ids: list[str]) -> bool:
        current = application.state.metadata_task
        if container.metadata is None or (current is not None and not current.done()):
            return False
        application.state.metadata_status = {
            "running": True,
            "completed": 0,
            "total": len(series_ids),
            "failed": 0,
        }
        application.state.metadata_task = asyncio.create_task(
            _run_metadata_lookup(application, series_ids)
        )
        return True

    application.state.start_metadata_lookup = start_metadata_lookup

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        if discarded_forwarded_proto(
            request.headers.get("x-forwarded-proto"), request.url.scheme
        ):
            _note_untrusted_proxy(application, request)
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


def _note_untrusted_proxy(application: FastAPI, request: Request) -> None:
    """Record, once per peer, that a forwarded header was thrown away."""
    peer = request.client.host if request.client else "an unknown address"
    if application.state.untrusted_proxy == peer:
        return
    application.state.untrusted_proxy = peer
    LOGGER.warning(
        "Ignoring X-Forwarded-Proto from %s: add it to NINEVEH_FORWARDED_ALLOW_IPS "
        "(currently %r) and recreate the container to honour it",
        peer,
        application.state.container.settings.forwarded_allow_ips,
    )


def openapi_document() -> dict[str, object]:
    """The published API contract, built without touching a real deployment.

    Composes the app against a throwaway state directory so the document
    depends only on the route table, never on the machine generating it.
    """
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        settings = Settings(data_dir=root, state_dir=root / "state")
        return create_app(settings).openapi()


def _discard_stale_ranges(directory: Path) -> None:
    """Generated range archives never outlive the response that produced them."""
    for leftover in directory.glob("range-*.cbz"):
        leftover.unlink(missing_ok=True)


def _discard_flat_candidate_covers(directory: Path) -> None:
    """Drop suggestion thumbnails written before they moved under a budget.

    They sit beside the series covers an operator is told to back up, and the
    new cache only scans `candidates/`, so nothing would ever reclaim them.
    """
    for leftover in directory.glob("candidate-*.webp"):
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


async def _run_scan(scanner: CatalogScan, library_id: str | None = None) -> None:
    try:
        report = await asyncio.to_thread(scanner.scan, library_id)
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


async def _run_metadata_lookup(application: FastAPI, series_ids: list[str]) -> None:
    container = application.state.container
    try:
        for series_id in series_ids:
            series = await asyncio.to_thread(
                container.repository.catalog_series_by_id, series_id
            )
            if not series or series.category.casefold() != "manga":
                application.state.metadata_status["failed"] += 1
                application.state.metadata_status["completed"] += 1
                continue
            try:
                await asyncio.to_thread(container.metadata.lookup, series)
            except Exception:
                LOGGER.exception("Metadata lookup failed for series %s", series_id)
                application.state.metadata_status["failed"] += 1
            application.state.metadata_status["completed"] += 1
    finally:
        application.state.metadata_status["running"] = False


async def _scan_scheduler(application: FastAPI, settings: Settings) -> None:
    if settings.scan_interval_seconds == 0:
        return
    while True:
        await asyncio.sleep(settings.scan_interval_seconds)
        application.state.start_scan()
