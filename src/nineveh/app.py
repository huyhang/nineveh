from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Collection
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
    PageRenditionService,
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
from .librarian import AuditTrail, IngestService, LibrarianAuth, LibrarianService
from .metadata import (
    MangaBakaProvider,
    MetadataCoverStore,
    MetadataError,
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
    RenditionSource,
    Repository,
    RestartController,
)
from .reader import ReaderService, SpreadDetectionService
from .restart import DisabledRestartController, ProcessRestartController
from .spreads import LayeredSpreadDetector, SeamSpreadDetector, WidePageDetector

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
    renditions: RenditionSource
    page_cache: PageStore
    opds: OpdsBuilder
    access: AccessService
    libraries: LibraryService
    configuration: SettingsService
    restarter: RestartController
    reader: ReaderService
    audit: AuditTrail
    librarian_auth: LibrarianAuth
    librarian: LibrarianService
    ingest: IngestService
    metadata: MetadataService | None = None
    spreads: SpreadDetectionService | None = None


def build_container(settings: Settings) -> Container:
    repository = SQLiteRepository(settings.database_path)
    # Persisted settings decide how the rest of the graph is built, so the
    # schema has to exist before anything else is constructed. `initialize` is
    # idempotent; the lifespan calls it again for containers built by hand.
    repository.initialize()
    configuration = SettingsService(settings, repository)
    effective = configuration.activate()
    archives = ArchiveService(effective)
    audit = AuditTrail(repository)
    limiter = PersistentRateLimiter(repository, configuration.mangabaka_request_limit)
    spreads = SpreadDetectionService(
        repository,
        archives,
        LayeredSpreadDetector(
            SeamSpreadDetector(archives, effective.max_image_pixels),
            WidePageDetector(),
        ),
    )
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
        renditions=PageRenditionService(
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
        reader=ReaderService(repository, repository),
        audit=audit,
        librarian_auth=LibrarianAuth(repository, audit),
        librarian=LibrarianService(repository),
        ingest=IngestService(
            effective.data_dir,
            effective.ingest_staging_dir,
            repository,
            ArchiveInspector(effective),
            effective.max_upload_bytes,
        ),
        metadata=MetadataService(
            repository,
            MangaBakaProvider(UrllibTransport(), limiter),
            MetadataCoverStore(
                effective.metadata_cover_dir, effective.max_image_pixels
            ),
        ),
        spreads=spreads,
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
            configured.rendition_dir,
            configured.range_dir,
            configured.metadata_cover_dir,
            configured.ingest_staging_dir,
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
        active_job = await asyncio.to_thread(
            container.repository.active_metadata_auto_match_job
        )
        if active_job:
            application.state.start_metadata_auto_match(active_job.id)
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
            await _drain_scan(application.state.spread_task)
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
    application.state.pending_metadata_job = None
    application.state.spread_task = None
    application.state.spread_pending = {}
    application.state.spread_active = None
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
            _run_scan(application, library_id)
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

    def start_metadata_auto_match(job_id: str) -> bool:
        current = application.state.metadata_task
        if container.metadata is None:
            return False
        if current is not None and not current.done():
            if application.state.pending_metadata_job is not None:
                return False
            application.state.pending_metadata_job = job_id
            return True
        application.state.metadata_task = asyncio.create_task(
            _run_metadata_auto_match(application, job_id)
        )
        return True

    application.state.start_metadata_auto_match = start_metadata_auto_match

    def start_spread_detection(series_ids: list[str], *, force: bool = False) -> bool:
        if container.spreads is None:
            return False
        for series_id in dict.fromkeys(series_ids):
            application.state.spread_pending[series_id] = (
                force or application.state.spread_pending.get(series_id, False)
            )
        current = application.state.spread_task
        if current is not None and not current.done():
            return True
        application.state.spread_task = asyncio.create_task(
            _run_spread_detection(application)
        )
        return True

    application.state.start_spread_detection = start_spread_detection

    def spread_detection_running(series_id: str) -> bool:
        """Whether this one series is queued or being analyzed right now.

        Asking about the shared task instead would make every series page in
        the library claim to be analyzing whenever any of them was.
        """
        return (
            series_id in application.state.spread_pending
            or application.state.spread_active == series_id
        )

    application.state.spread_detection_running = spread_detection_running

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


CONTRACT_SLICES: dict[str, frozenset[str]] = {
    # An LLM agent on another device: resolve series, stage and place volumes.
    "librarian": frozenset({"librarian"}),
    # A native reading app: browse over OPDS, then fetch pages and files and
    # sync progress through the extensions the feeds advertise. Health checks
    # stay out; the OPDS authentication document and `/auth/me` already tell
    # an app whether a server is Nineveh and whether its credentials work.
    "app": frozenset(
        {"authentication", "opds", "pages", "publications", "reader", "series"}
    ),
}


def tagged_contract(
    name: str, tags: Collection[str], document: dict[str, object] | None = None
) -> dict:
    """The slice of the contract one kind of client actually needs.

    The librarian agent calls eight of sixty operations. Handing it the
    whole document means every unrelated endpoint churns its vendored copy and
    raises a false "the contract moved" alarm, so the useful signal -- did *my*
    surface change? -- gets lost in noise.

    Schemas are pulled in by following `$ref` to a fixed point rather than by
    copying `components` wholesale: a subset that references a schema it does
    not carry is worse than no subset, because it fails at generation time
    instead of review time. Security schemes travel the same way, for the
    same reason.
    """
    source = document or openapi_document()
    selected = frozenset(tags)
    paths: dict[str, dict] = {}
    for path, item in source.get("paths", {}).items():
        operations = {
            method: operation
            for method, operation in item.items()
            if isinstance(operation, dict)
            and not selected.isdisjoint(operation.get("tags") or [])
        }
        if not operations:
            continue
        # Path-level keys (shared `parameters`, `summary`) belong to every
        # operation under them, so they travel with any that survive.
        shared = {
            key: value
            for key, value in item.items()
            if not isinstance(value, dict) or "responses" not in value
        }
        paths[path] = {**shared, **operations}

    schemas = source.get("components", {}).get("schemas", {})
    wanted: set[str] = set()
    _collect_refs(paths, wanted)
    pending = set(wanted)
    while pending:
        nested: set[str] = set()
        _collect_refs(schemas.get(pending.pop(), {}), nested)
        discovered = nested - wanted
        wanted |= discovered
        pending |= discovered

    required = {
        scheme
        for item in paths.values()
        for operation in item.values()
        if isinstance(operation, dict)
        for requirement in operation.get("security") or []
        for scheme in requirement
    }
    defined = source.get("components", {}).get("securitySchemes", {})

    components: dict[str, dict] = {}
    if wanted:
        components["schemas"] = {
            schema: schemas[schema] for schema in sorted(wanted) if schema in schemas
        }
    if required:
        components["securitySchemes"] = {
            scheme: defined[scheme] for scheme in sorted(required) if scheme in defined
        }

    contract: dict[str, object] = {
        "openapi": source["openapi"],
        "info": {
            **source["info"],
            "title": f"{source['info']['title']} ({name})",
        },
        "paths": paths,
    }
    if components:
        contract["components"] = components
    return contract


def _collect_refs(node: object, found: set[str]) -> None:
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith(_SCHEMA_PREFIX):
            found.add(reference[len(_SCHEMA_PREFIX) :])
        for value in node.values():
            _collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)


_SCHEMA_PREFIX = "#/components/schemas/"


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


async def _run_scan(application: FastAPI, library_id: str | None = None) -> None:
    container = application.state.container
    try:
        report = await asyncio.to_thread(container.scanner.scan, library_id)
        LOGGER.info(
            "Catalog scan completed: discovered=%d indexed=%d unchanged=%d removed=%d failed=%d",
            report.discovered,
            report.indexed,
            report.unchanged,
            report.removed,
            report.failed,
        )
        series_ids = await asyncio.to_thread(
            container.repository.spread_detection_series_ids
        )
        if series_ids:
            application.state.start_spread_detection(series_ids)
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
        pending_job = application.state.pending_metadata_job
        application.state.pending_metadata_job = None
        if pending_job:
            await _run_metadata_auto_match(application, pending_job)


async def _run_metadata_auto_match(application: FastAPI, job_id: str) -> None:
    container = application.state.container
    completed_normally = False
    try:
        series_ids = await asyncio.to_thread(
            container.repository.pending_metadata_auto_match_series, job_id
        )
        for series_id in series_ids:
            series = await asyncio.to_thread(
                container.repository.catalog_series_by_id, series_id
            )
            if not series or series.category.casefold() != "manga":
                await asyncio.to_thread(
                    container.repository.finish_metadata_auto_match_item,
                    job_id,
                    series_id,
                    "failed",
                    "Manga series no longer exists",
                )
                continue
            try:
                result = await asyncio.to_thread(container.metadata.auto_match, series)
            except Exception as error:
                LOGGER.exception("Metadata auto-match failed for series %s", series_id)
                detail = (
                    str(error)
                    if isinstance(error, MetadataError)
                    else "Unexpected metadata error"
                )
                lookup = await asyncio.to_thread(
                    container.repository.metadata_lookup, series_id
                )
                if lookup.error != detail:
                    await asyncio.to_thread(
                        container.repository.replace_metadata_lookup,
                        series_id,
                        list(lookup.candidates),
                        detail,
                    )
                await asyncio.to_thread(
                    container.repository.finish_metadata_auto_match_item,
                    job_id,
                    series_id,
                    "failed",
                    detail,
                )
                continue
            await asyncio.to_thread(
                container.repository.finish_metadata_auto_match_item,
                job_id,
                series_id,
                result.status,
                result.detail,
            )
        completed_normally = True
    finally:
        if completed_normally:
            await asyncio.to_thread(
                container.repository.complete_metadata_auto_match_job, job_id
            )


async def _run_spread_detection(application: FastAPI) -> None:
    service = application.state.container.spreads
    if service is None:
        return
    while application.state.spread_pending:
        series_id = next(iter(application.state.spread_pending))
        force = application.state.spread_pending.pop(series_id)
        application.state.spread_active = series_id
        try:
            await asyncio.to_thread(service.analyze_series, series_id, force=force)
        except Exception:
            LOGGER.exception("Spread detection failed for series %s", series_id)
        finally:
            application.state.spread_active = None


async def _scan_scheduler(application: FastAPI, settings: Settings) -> None:
    if settings.scan_interval_seconds == 0:
        return
    while True:
        await asyncio.sleep(settings.scan_interval_seconds)
        application.state.start_scan()
