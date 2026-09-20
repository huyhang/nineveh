from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .archives import ArchiveChanged, ArchiveUnavailable
from .auth import (
    AuthenticationError,
    AuthService,
    InvalidUserInput,
    LastAdministratorError,
)
from .catalog import InvalidLibrary
from .deployment import memory_limit_text
from .domain import (
    AccessGrant,
    CatalogSeries,
    LibraryUsage,
    ManagedLibrary,
    Page,
    Publication,
    Session,
    User,
)
from .metadata import MetadataError
from .opds import CBZ_MEDIA_TYPE, NAVIGATION_PATH, PUBLICATIONS_PATH
from .opds import url as opds_url

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from .app import Container

SESSION_COOKIE = "nineveh_session"
NavigationEntries = tuple[str, dict[str, str], list[tuple[str, int, str]]]
basic_auth = HTTPBasic(auto_error=False)


@dataclass(frozen=True, slots=True)
class Identity:
    user: User
    session: Session | None = None


class UserCreate(BaseModel):
    username: str
    password: str = Field(min_length=12, max_length=1024)
    is_admin: bool = False


class UserUpdate(BaseModel):
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=1024)


class GrantInput(BaseModel):
    library_id: str
    category: Literal["comics", "manga"] | None = None
    series_id: str | None = None


class AccessUpdate(BaseModel):
    grants: list[GrantInput]


class LibraryCreate(BaseModel):
    relative_path: str = Field(min_length=1, max_length=255)


class SettingsUpdate(BaseModel):
    values: dict[str, str]


class MetadataLookupInput(BaseModel):
    query: str | None = Field(default=None, max_length=200)


class MetadataMatchInput(BaseModel):
    provider_id: int = Field(gt=0)


class MetadataEditInput(BaseModel):
    values: dict[str, str | int | float | list[str] | None]


class MetadataBatchInput(BaseModel):
    series_ids: list[str] = Field(min_length=1, max_length=500)


def _container(request: Request) -> Container:
    return request.app.state.container


async def authenticated(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_auth)],
) -> Identity:
    container = _container(request)
    if credentials:
        try:
            user = await run_in_threadpool(
                container.auth.authenticate, credentials.username, credentials.password
            )
            return Identity(user)
        except AuthenticationError:
            pass
    elif browser_session := await run_in_threadpool(
        container.auth.session, request.cookies.get(SESSION_COOKIE)
    ):
        return Identity(browser_session.user, browser_session)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="Nineveh", charset="UTF-8"'},
    )


async def administrator(
    identity: Annotated[Identity, Depends(authenticated)],
    request: Request,
) -> Identity:
    if not _container(request).authorization.can_administer(identity.user):
        raise HTTPException(status_code=403, detail="Administrator access required")
    return identity


def require_api_csrf(
    request: Request, identity: Identity, supplied: str | None
) -> None:
    if identity.session and not _container(request).auth.valid_csrf(
        identity.session, supplied
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def base_url(request: Request) -> str:
    configured = _container(request).settings.public_base_url
    return (configured or str(request.base_url)).rstrip("/")


router = APIRouter()


class OpdsResponse(JSONResponse):
    media_type = "application/opds+json"


class OpdsAuthenticationResponse(JSONResponse):
    media_type = "application/opds-authentication+json"


@router.get("/api/v1/health/live", tags=["health"])
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/v1/health/ready", tags=["health"])
async def ready(request: Request) -> dict[str, object]:
    container = _container(request)
    database_ready = await run_in_threadpool(container.repository.ping)
    scan = container.scanner.status
    return {
        "status": "ok" if database_ready else "unavailable",
        "catalog": asdict(scan),
    }


@router.get(
    "/opds/v2/authentication.json",
    response_class=OpdsAuthenticationResponse,
    tags=["opds"],
)
async def opds_authentication(request: Request) -> dict[str, object]:
    return _container(request).opds.authentication_document(base_url(request))


@router.get("/opds/v2/catalog.json", response_class=OpdsResponse, tags=["opds"])
async def opds_catalog(
    request: Request, identity: Annotated[Identity, Depends(authenticated)]
) -> dict[str, object]:
    container = _container(request)
    scope = container.authorization.read_scope(identity.user)
    libraries = await run_in_threadpool(container.repository.libraries, scope)
    return container.opds.root_feed(
        base_url(request), libraries, _catalog_modified(container)
    )


@router.get("/opds/v2/navigation.json", response_class=OpdsResponse, tags=["opds"])
async def opds_navigation(
    request: Request,
    identity: Annotated[Identity, Depends(authenticated)],
    library: str,
    category: str | None = None,
) -> dict[str, object]:
    container = _container(request)
    root = base_url(request)
    scope = container.authorization.read_scope(identity.user)
    title, parameters, entries = await (
        _category_entries(container, root, library, scope)
        if category is None
        else _series_entries(container, root, library, category, scope)
    )
    return container.opds.navigation_feed(
        root,
        title=title,
        parameters=parameters,
        entries=entries,
        modified=_catalog_modified(container),
    )


async def _category_entries(
    container: Container, root: str, library: str, scope
) -> NavigationEntries:
    groups = await run_in_threadpool(container.repository.categories, library, scope)
    entries = [
        (
            name,
            count,
            opds_url(root, NAVIGATION_PATH, {"library": library, "category": name}),
        )
        for name, count in groups
    ]
    return library, {"library": library}, entries


async def _series_entries(
    container: Container, root: str, library: str, category: str, scope
) -> NavigationEntries:
    groups = await run_in_threadpool(
        container.repository.series, library, category, scope
    )
    entries = [
        (
            name,
            count,
            opds_url(
                root,
                PUBLICATIONS_PATH,
                {"library": library, "category": category, "series": name},
            ),
        )
        for name, count in groups
    ]
    parameters = {"library": library, "category": category}
    return f"{library} — {category}", parameters, entries


@router.get("/opds/v2/publications.json", response_class=OpdsResponse, tags=["opds"])
async def opds_publications(
    request: Request,
    identity: Annotated[Identity, Depends(authenticated)],
    library: str | None = None,
    category: str | None = None,
    series: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
) -> dict[str, object]:
    container = _container(request)
    scope = container.authorization.read_scope(identity.user)
    page_size = container.settings.feed_page_size
    items, total = await run_in_threadpool(
        container.repository.publications,
        library=library,
        category=category,
        series=series,
        query=q,
        limit=page_size,
        offset=(page - 1) * page_size,
        scope=scope,
    )
    return container.opds.publication_feed(
        base_url(request),
        items,
        total=total,
        page=page,
        page_size=page_size,
        library=library,
        category=category,
        series=series,
        query=q,
        modified=_catalog_modified(container),
    )


@router.get("/api/v1/publications/{publication_id}", tags=["publications"])
async def publication_detail(
    request: Request,
    publication_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
) -> dict[str, object]:
    publication = await _publication_or_404(request, publication_id, identity)
    return _container(request).opds.publication(base_url(request), publication)


@router.get("/api/v1/series/{series_id}", tags=["series"])
async def series_detail(
    request: Request,
    series_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
) -> dict[str, object]:
    item = await _series_or_404(request, series_id, identity)
    metadata = await run_in_threadpool(
        _container(request).repository.series_metadata, series_id
    )
    return _public_series(item, metadata)


@router.get(
    "/api/v1/series/{series_id}/cover",
    tags=["series"],
    operation_id="readSeriesCover",
)
@router.head(
    "/api/v1/series/{series_id}/cover",
    tags=["series"],
    operation_id="headSeriesCover",
)
async def series_cover(
    request: Request,
    series_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
    revision: str | None = None,
):
    del revision  # The URL changes with the local fallback; ETag covers overrides.
    container = _container(request)
    series = await _series_or_404(request, series_id, identity)
    custom = container.metadata.covers.cover(series_id) if container.metadata else None
    if custom:
        stat = custom.stat()
        return FileResponse(
            custom,
            media_type="image/webp",
            headers={
                "ETag": _etag(f"{stat.st_mtime_ns}-{stat.st_size}"),
                "Cache-Control": "private, no-cache",
            },
        )
    publication = await run_in_threadpool(
        container.repository.publication_by_id,
        series.first_publication_id,
        container.authorization.read_scope(identity.user),
    )
    if not publication:
        raise HTTPException(status_code=404, detail="Series cover not found")
    page = await run_in_threadpool(
        container.repository.page, publication.id, publication.cover_page
    )
    if not page:
        raise HTTPException(status_code=404, detail="Series cover not found")
    try:
        path = await run_in_threadpool(
            container.thumbnails.cover, publication, page.page, 640
        )
    except ArchiveChanged as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ArchiveUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return FileResponse(
        path,
        media_type="image/webp",
        headers={
            "ETag": _etag(f"{publication.revision}-series-cover"),
            "Cache-Control": "private, no-cache",
        },
    )


# One handler, registered once per verb so each OpenAPI operation carries a
# unique id -- generators reject a spec that repeats one.
@router.get(
    "/api/v1/publications/{publication_id}/file",
    tags=["publications"],
    operation_id="downloadPublicationFile",
)
@router.head(
    "/api/v1/publications/{publication_id}/file",
    tags=["publications"],
    operation_id="headPublicationFile",
)
async def publication_file(
    request: Request,
    publication_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
):
    container = _container(request)
    publication = await _publication_or_404(request, publication_id, identity)
    etag = _etag(publication.revision)
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    try:
        path = await run_in_threadpool(container.archives.archive_path, publication)
    except ArchiveChanged as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ArchiveUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return FileResponse(
        path,
        media_type=CBZ_MEDIA_TYPE,
        filename=publication.filename,
        content_disposition_type="attachment",
        headers={"ETag": etag, "Cache-Control": "private, no-cache"},
    )


@router.get("/api/v1/publications/{publication_id}/pages", tags=["pages"])
async def page_manifest(
    request: Request,
    publication_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
    start: int = Query(default=1, ge=1),
    end: int | None = Query(default=None, ge=1),
) -> dict[str, object]:
    container = _container(request)
    publication = await _publication_or_404(request, publication_id, identity)
    start, effective_end = _resolve_range(
        publication, start, end, container.settings.page_range_limit
    )
    pages = await run_in_threadpool(
        container.repository.pages, publication.id, start, effective_end
    )
    try:
        pages = await run_in_threadpool(
            _enrich_dimensions, container, publication, pages
        )
    except ArchiveChanged as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ArchiveUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _manifest_body(
        base_url(request),
        publication,
        pages,
        start,
        effective_end,
        container.settings.page_range_limit,
    )


def _manifest_body(
    root: str,
    publication: Publication,
    pages: list[Page],
    start: int,
    end: int,
    limit: int,
) -> dict[str, object]:
    base = f"{root}/api/v1/publications/{publication.id}"
    body: dict[str, object] = {
        "publicationId": publication.id,
        "revision": publication.revision,
        "totalPages": publication.page_count,
        "start": start,
        "end": end,
        "pages": [
            {
                "number": item.number,
                "href": f"{base}/pages/{item.number}?revision={publication.revision}",
                "type": item.media_type,
                "length": item.uncompressed_size,
                "width": item.width,
                "height": item.height,
                "spread": item.is_spread,
            }
            for item in pages
        ],
    }
    if end < publication.page_count:
        next_end = min(publication.page_count, end + limit)
        body["next"] = f"{base}/pages?start={end + 1}&end={next_end}"
    return body


def _resolve_range(
    publication: Publication, start: int, end: int | None, limit: int
) -> tuple[int, int]:
    """Clamp and validate an inclusive 1-based page range against a publication."""
    if start > publication.page_count:
        raise HTTPException(
            status_code=416, detail="Start page is outside the publication"
        )
    last = end or min(publication.page_count, start + limit - 1)
    if last < start:
        raise HTTPException(
            status_code=422, detail="End page must not precede start page"
        )
    if last > publication.page_count:
        raise HTTPException(
            status_code=416, detail="End page is outside the publication"
        )
    if last - start + 1 > limit:
        raise HTTPException(
            status_code=422, detail=f"A range may contain at most {limit} pages"
        )
    return start, last


@router.get("/api/v1/publications/{publication_id}/range", tags=["pages"])
async def publication_range(
    request: Request,
    publication_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
    start: int = Query(default=1, ge=1),
    end: int | None = Query(default=None, ge=1),
):
    """Download a contiguous page range as a standalone CBZ."""
    container = _container(request)
    publication = await _publication_or_404(request, publication_id, identity)
    first, last = _resolve_range(
        publication, start, end, container.settings.page_range_limit
    )
    etag = _etag(f"{publication.revision}-range-{first}-{last}")
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    pages = await run_in_threadpool(
        container.repository.pages, publication.id, first, last
    )
    if len(pages) != last - first + 1:
        raise HTTPException(status_code=409, detail="Publication revision has changed")
    try:
        archive = await run_in_threadpool(_build_range, container, publication, pages)
    except ArchiveChanged as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ArchiveUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return FileResponse(
        archive,
        media_type=CBZ_MEDIA_TYPE,
        filename=f"{Path(publication.filename).stem} p{first}-{last}.cbz",
        content_disposition_type="attachment",
        headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        background=BackgroundTask(archive.unlink, missing_ok=True),
    )


def _build_range(
    container: Container, publication: Publication, pages: list[Page]
) -> Path:
    directory = container.settings.range_dir
    directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="range-", suffix=".cbz", dir=directory)
    os.close(handle)
    archive = Path(name)
    try:
        container.archives.write_range(publication, pages, archive)
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return archive


# One handler, registered once per verb so each OpenAPI operation carries a
# unique id -- generators reject a spec that repeats one.
@router.get(
    "/api/v1/publications/{publication_id}/pages/{number}",
    tags=["pages"],
    operation_id="readPublicationPage",
)
@router.head(
    "/api/v1/publications/{publication_id}/pages/{number}",
    tags=["pages"],
    operation_id="headPublicationPage",
)
async def publication_page(
    request: Request,
    publication_id: str,
    number: int,
    identity: Annotated[Identity, Depends(authenticated)],
    revision: str | None = None,
):
    container = _container(request)
    scope = container.authorization.read_scope(identity.user)
    item = await run_in_threadpool(
        container.repository.page, publication_id, number, scope
    )
    if not item:
        raise HTTPException(status_code=404, detail="Page not found")
    if revision and revision != item.publication.revision:
        raise HTTPException(status_code=409, detail="Publication revision has changed")
    etag = _etag(f"{item.publication.revision}-{item.page.crc:08x}")
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    try:
        cached_path = await run_in_threadpool(
            container.page_cache.page, item.publication, item.page
        )
    except ArchiveChanged as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ArchiveUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=31536000, immutable"
        if revision
        else "private, no-cache",
    }
    if cached_path:
        return FileResponse(
            cached_path, media_type=item.page.media_type, headers=headers
        )
    content = await run_in_threadpool(
        container.archives.open_page, item.publication, item.page
    )
    headers["Content-Length"] = str(item.page.uncompressed_size)
    return StreamingResponse(content, media_type=item.page.media_type, headers=headers)


# One handler, registered once per verb so each OpenAPI operation carries a
# unique id -- generators reject a spec that repeats one.
@router.get(
    "/api/v1/publications/{publication_id}/cover",
    tags=["pages"],
    operation_id="readPublicationCover",
)
@router.head(
    "/api/v1/publications/{publication_id}/cover",
    tags=["pages"],
    operation_id="headPublicationCover",
)
async def publication_cover(
    request: Request,
    publication_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
    width: int = Query(default=320),
    revision: str | None = None,
):
    container = _container(request)
    publication = await _publication_or_404(request, publication_id, identity)
    if revision and revision != publication.revision:
        raise HTTPException(status_code=409, detail="Publication revision has changed")
    item = await run_in_threadpool(
        container.repository.page, publication.id, publication.cover_page
    )
    if not item:
        raise HTTPException(status_code=404, detail="Cover page not found")
    try:
        path = await run_in_threadpool(
            container.thumbnails.cover, publication, item.page, width
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ArchiveChanged as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ArchiveUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return FileResponse(
        path,
        media_type="image/webp",
        headers={
            "ETag": _etag(f"{publication.revision}-cover-{width}"),
            "Cache-Control": "private, max-age=31536000, immutable",
        },
    )


@router.get("/api/v1/auth/me", tags=["authentication"])
async def me(
    identity: Annotated[Identity, Depends(authenticated)],
) -> dict[str, object]:
    return _public_user(identity.user)


@router.get("/api/v1/admin/users", tags=["administration"])
async def users(
    request: Request, _: Annotated[Identity, Depends(administrator)]
) -> list[dict[str, object]]:
    items = await run_in_threadpool(_container(request).repository.users)
    return [_public_user(item) for item in items]


@router.post("/api/v1/admin/users", status_code=201, tags=["administration"])
async def create_user(
    request: Request,
    body: UserCreate,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    try:
        user = await run_in_threadpool(
            _container(request).auth.create_user,
            body.username,
            body.password,
            is_admin=body.is_admin,
        )
    except InvalidUserInput as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except sqlite3.IntegrityError as error:
        raise HTTPException(
            status_code=409, detail="Username already exists"
        ) from error
    return _public_user(user)


@router.patch("/api/v1/admin/users/{user_id}", tags=["administration"])
async def update_user(
    request: Request,
    user_id: str,
    body: UserUpdate,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    if body.enabled is False and user_id == identity.user.id:
        raise HTTPException(
            status_code=422, detail="You cannot disable your own account"
        )
    service: AuthService = _container(request).auth
    existing = await run_in_threadpool(
        _container(request).repository.user_by_id, user_id
    )
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        user = existing
        if body.password is not None:
            user = await run_in_threadpool(
                service.reset_password, user_id, body.password
            )
        if body.enabled is not None:
            user = await run_in_threadpool(service.set_enabled, user_id, body.enabled)
    except (InvalidUserInput, LastAdministratorError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _public_user(user)


@router.get("/api/v1/admin/users/{user_id}/access", tags=["administration"])
async def user_access(
    request: Request,
    user_id: str,
    _: Annotated[Identity, Depends(administrator)],
) -> dict[str, object]:
    container = _container(request)
    user = await run_in_threadpool(container.repository.user_by_id, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    grants = (
        []
        if user.is_admin
        else await run_in_threadpool(container.access.grants, user_id)
    )
    return {
        "unrestricted": user.is_admin,
        "grants": [_public_grant(item) for item in grants],
    }


@router.put("/api/v1/admin/users/{user_id}/access", tags=["administration"])
async def update_user_access(
    request: Request,
    user_id: str,
    body: AccessUpdate,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    service = _container(request).access
    grants = [
        AccessGrant(user_id, item.library_id, item.category, item.series_id)
        for item in body.grants
    ]
    try:
        await run_in_threadpool(service.replace, user_id, grants)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    saved = await run_in_threadpool(service.grants, user_id)
    return {"unrestricted": False, "grants": [_public_grant(item) for item in saved]}


@router.get("/api/v1/admin/libraries", tags=["administration"])
async def admin_libraries(
    request: Request, _: Annotated[Identity, Depends(administrator)]
) -> dict[str, object]:
    container = _container(request)
    usage = await run_in_threadpool(container.repository.library_usage)
    available = await run_in_threadpool(container.libraries.available)
    return {
        "libraries": [_public_usage(item) for item in usage],
        "available": available,
    }


@router.post("/api/v1/admin/libraries", status_code=201, tags=["administration"])
async def add_library(
    request: Request,
    body: LibraryCreate,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    if scan_active(request):
        raise HTTPException(
            status_code=409, detail="Wait for the catalog scan to finish"
        )
    service = _container(request).libraries
    try:
        library = await run_in_threadpool(service.add, body.relative_path)
    except InvalidLibrary as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        **_public_library(library),
        "scanStarted": request.app.state.start_scan(library.id),
    }


@router.delete("/api/v1/admin/libraries/{library_id}", tags=["administration"])
async def remove_library(
    request: Request,
    library_id: str,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    if scan_active(request):
        raise HTTPException(
            status_code=409, detail="Wait for the catalog scan to finish"
        )
    service = _container(request).libraries
    try:
        library = await run_in_threadpool(service.remove, library_id)
    except InvalidLibrary as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _public_library(library)


@router.post("/api/v1/admin/catalog/scan", status_code=202, tags=["administration"])
async def scan_catalog(
    request: Request,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    if not request.app.state.start_scan():
        raise HTTPException(status_code=409, detail="A catalog scan is already running")
    return {"status": "accepted"}


@router.post(
    "/api/v1/admin/libraries/{library_id}/scan",
    status_code=202,
    tags=["administration"],
)
async def scan_library(
    request: Request,
    library_id: str,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    library = await run_in_threadpool(
        _container(request).repository.managed_library, library_id
    )
    if not library or not library.enabled:
        raise HTTPException(status_code=404, detail="Managed library not found")
    if not request.app.state.start_scan(library_id):
        raise HTTPException(status_code=409, detail="A catalog scan is already running")
    return {"status": "accepted", "libraryId": library_id}


@router.get("/api/v1/admin/settings", tags=["administration"])
async def admin_settings(
    request: Request, _: Annotated[Identity, Depends(administrator)]
) -> dict[str, object]:
    return await _settings_response(request)


@router.get("/api/v1/admin/metadata", tags=["administration"])
async def admin_metadata(
    request: Request, _: Annotated[Identity, Depends(administrator)]
) -> dict[str, object]:
    container = _container(request)
    items = await run_in_threadpool(
        container.repository.catalog_series, category="manga"
    )
    metadata = await run_in_threadpool(container.repository.all_series_metadata)
    return {
        "series": [_public_series(item, metadata.get(item.id)) for item in items],
        "lookup": request.app.state.metadata_status,
        "requestsPerMinute": container.configuration.mangabaka_request_limit(),
        "maximumRequestsPerMinute": 30,
    }


@router.post("/api/v1/admin/metadata/lookup", status_code=202, tags=["administration"])
async def admin_metadata_batch_lookup(
    request: Request,
    body: MetadataBatchInput,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    if not request.app.state.start_metadata_lookup(
        list(dict.fromkeys(body.series_ids))
    ):
        raise HTTPException(
            status_code=409, detail="A metadata lookup is already running"
        )
    return {"status": "accepted", "seriesCount": len(set(body.series_ids))}


@router.post(
    "/api/v1/admin/series/{series_id}/metadata/lookup",
    tags=["administration"],
)
async def admin_series_metadata_lookup(
    request: Request,
    series_id: str,
    body: MetadataLookupInput,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    container = _container(request)
    series = await _admin_manga_series(request, series_id)
    service = _metadata_service(container)
    try:
        lookup = await run_in_threadpool(service.lookup, series, body.query)
    except MetadataError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return _public_lookup(lookup)


@router.get(
    "/api/v1/admin/series/{series_id}/metadata/candidates/{provider_id}/cover",
    tags=["administration"],
)
async def admin_series_metadata_candidate_cover(
    request: Request,
    series_id: str,
    provider_id: int,
    _: Annotated[Identity, Depends(administrator)],
):
    container = _container(request)
    await _admin_manga_series(request, series_id)
    try:
        path = await run_in_threadpool(
            _metadata_service(container).candidate_cover, series_id, provider_id
        )
    except MetadataError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    stat = path.stat()
    return FileResponse(
        path,
        media_type="image/webp",
        headers={
            "ETag": _etag(f"candidate-{provider_id}-{stat.st_mtime_ns}"),
            "Cache-Control": "private, max-age=86400",
        },
    )


@router.post(
    "/api/v1/admin/series/{series_id}/metadata/match",
    tags=["administration"],
)
async def admin_series_metadata_match(
    request: Request,
    series_id: str,
    body: MetadataMatchInput,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    container = _container(request)
    series = await _admin_manga_series(request, series_id)
    try:
        metadata = await run_in_threadpool(
            _metadata_service(container).match, series, body.provider_id
        )
    except MetadataError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return _public_series(series, metadata)


@router.post(
    "/api/v1/admin/series/{series_id}/metadata/refresh",
    tags=["administration"],
)
async def admin_series_metadata_refresh(
    request: Request,
    series_id: str,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    container = _container(request)
    series = await _admin_manga_series(request, series_id)
    try:
        metadata = await run_in_threadpool(_metadata_service(container).refresh, series)
    except MetadataError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return _public_series(series, metadata)


@router.patch("/api/v1/admin/series/{series_id}/metadata", tags=["administration"])
async def admin_series_metadata_edit(
    request: Request,
    series_id: str,
    body: MetadataEditInput,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    container = _container(request)
    series = await _admin_manga_series(request, series_id)
    try:
        metadata = await run_in_threadpool(
            _metadata_service(container).update, series_id, body.values
        )
    except MetadataError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _public_series(series, metadata)


@router.delete("/api/v1/admin/series/{series_id}/metadata", tags=["administration"])
async def admin_series_metadata_unlink(
    request: Request,
    series_id: str,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, str]:
    require_api_csrf(request, identity, csrf_token)
    container = _container(request)
    await _admin_manga_series(request, series_id)
    await run_in_threadpool(_metadata_service(container).unlink, series_id)
    return {"status": "unlinked"}


@router.put("/api/v1/admin/settings", tags=["administration"])
async def update_settings(
    request: Request,
    body: SettingsUpdate,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    service = _container(request).configuration
    try:
        await run_in_threadpool(service.update, body.values)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return await _settings_response(request)


@router.post("/api/v1/admin/restart", status_code=202, tags=["administration"])
async def restart_application(
    request: Request,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, str]:
    require_api_csrf(request, identity, csrf_token)
    restarter = _container(request).restarter
    if not restarter.enabled:
        raise HTTPException(
            status_code=409,
            detail="Automatic restart is not enabled for this deployment",
        )
    restarter.request_restart()
    return {"status": "restarting"}


async def _publication_or_404(
    request: Request, publication_id: str, identity: Identity
) -> Publication:
    publication = await run_in_threadpool(
        _container(request).repository.publication_by_id,
        publication_id,
        _container(request).authorization.read_scope(identity.user),
    )
    if not publication:
        raise HTTPException(status_code=404, detail="Publication not found")
    return publication


async def _series_or_404(
    request: Request, series_id: str, identity: Identity
) -> CatalogSeries:
    container = _container(request)
    series = await run_in_threadpool(
        container.repository.catalog_series_by_id,
        series_id,
        container.authorization.read_scope(identity.user),
    )
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    return series


async def _admin_manga_series(request: Request, series_id: str) -> CatalogSeries:
    series = await run_in_threadpool(
        _container(request).repository.catalog_series_by_id, series_id
    )
    if not series or series.category.casefold() != "manga":
        raise HTTPException(status_code=404, detail="Manga series not found")
    return series


def _metadata_service(container: Container):
    if not container.metadata:
        raise HTTPException(status_code=503, detail="Metadata service unavailable")
    return container.metadata


def _enrich_dimensions(
    container: Container, publication: Publication, pages: list[Page]
) -> list[Page]:
    missing = [page for page in pages if page.width is None or page.height is None]
    measured = (
        container.archives.page_dimensions_many(publication, missing) if missing else {}
    )
    if measured:
        container.repository.update_page_dimensions(
            publication.id,
            [(number, width, height) for number, (width, height) in measured.items()],
        )
    enriched: list[Page] = []
    for page in pages:
        if page.number in measured:
            width, height = measured[page.number]
            page = replace(page, width=width, height=height)
        enriched.append(page)
    return enriched


def _public_user(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "username": user.username,
        "isAdmin": user.is_admin,
        "enabled": user.enabled,
        "createdAt": user.created_at.isoformat(),
    }


def _public_series(series: CatalogSeries, metadata) -> dict[str, object]:
    values = metadata.effective if metadata else {}
    title = values.get("title") or series.name
    return {
        "id": series.id,
        "libraryId": series.library_id,
        "library": series.library,
        "category": series.category,
        "localName": series.name,
        "title": title,
        "publicationCount": series.publication_count,
        "cover": f"/api/v1/series/{series.id}/cover",
        "metadata": {
            "provider": metadata.provider,
            "providerId": metadata.provider_id,
            "sourceUrl": metadata.canonical_url,
            "fetchedAt": metadata.fetched_at.isoformat(),
            "providerUpdatedAt": metadata.provider_updated_at,
            "values": values,
            "editedFields": sorted(metadata.overrides),
            "license": "CC BY-NC-SA 4.0",
        }
        if metadata
        else None,
    }


def _public_lookup(lookup) -> dict[str, object]:
    return {
        "seriesId": lookup.series_id,
        "searchedAt": lookup.searched_at.isoformat() if lookup.searched_at else None,
        "error": lookup.error,
        "candidates": [
            {
                "providerId": item.provider_id,
                "title": item.title,
                "alternativeTitles": item.alternative_titles,
                "authors": item.authors,
                "artists": item.artists,
                "description": item.description,
                "year": item.year,
                "mediaType": item.media_type,
                "status": item.status,
                "rating": item.rating,
                "publishers": item.publishers,
                "tags": item.tags,
                "sourceUrl": item.source_url,
            }
            for item in lookup.candidates
        ],
    }


def _public_grant(grant: AccessGrant) -> dict[str, str | None]:
    return {
        "libraryId": grant.library_id,
        "category": grant.category,
        "seriesId": grant.series_id,
    }


def _public_library(library: ManagedLibrary) -> dict[str, object]:
    return {
        "id": library.id,
        "name": library.name,
        "relativePath": library.relative_path,
        "enabled": library.enabled,
        "createdAt": library.created_at.isoformat(),
    }


def _public_usage(usage: LibraryUsage) -> dict[str, object]:
    return {
        **_public_library(usage.library),
        "publicationCount": usage.publication_count,
        "size": usage.size,
        "categories": [
            {
                "name": category.name,
                "publicationCount": category.publication_count,
                "size": category.size,
                "series": [
                    {
                        "id": series.id,
                        "name": series.name,
                        "publicationCount": series.publication_count,
                        "size": series.size,
                    }
                    for series in category.series
                ],
            }
            for category in usage.categories
        ],
    }


async def _settings_response(request: Request) -> dict[str, object]:
    container = _container(request)
    saved = await run_in_threadpool(container.configuration.saved)
    pending = await run_in_threadpool(container.configuration.pending_restart)
    memory = await run_in_threadpool(
        memory_limit_text, container.settings.deployment_memory_limit
    )
    return {
        "values": saved.editable_values(),
        "pendingRestart": pending,
        "restartEnabled": container.restarter.enabled,
        "deploymentMemoryLimit": memory,
    }


def scan_active(request: Request) -> bool:
    """Shared by both HTTP surfaces: library edits must not race a live scan."""
    task = request.app.state.scan_task
    return task is not None and not task.done()


def _etag(value: str) -> str:
    return f'"{value}"'


def _not_modified(request: Request, etag: str) -> bool:
    values = [
        value.strip() for value in request.headers.get("if-none-match", "").split(",")
    ]
    return etag in values or "*" in values


def _not_modified_response(etag: str) -> Response:
    return Response(status_code=304, headers={"ETag": etag})


def _catalog_modified(container: Container) -> str:
    return (
        container.scanner.status.catalog_modified_at
        or datetime.fromtimestamp(0, UTC).isoformat()
    )
