from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

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
from .domain import Page, Publication, Session, User
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
    request: Request, _: Annotated[Identity, Depends(authenticated)]
) -> dict[str, object]:
    container = _container(request)
    libraries = await run_in_threadpool(container.repository.libraries)
    return container.opds.root_feed(
        base_url(request), libraries, _catalog_modified(container)
    )


@router.get("/opds/v2/navigation.json", response_class=OpdsResponse, tags=["opds"])
async def opds_navigation(
    request: Request,
    _: Annotated[Identity, Depends(authenticated)],
    library: str,
    category: str | None = None,
) -> dict[str, object]:
    container = _container(request)
    root = base_url(request)
    title, parameters, entries = await (
        _category_entries(container, root, library)
        if category is None
        else _series_entries(container, root, library, category)
    )
    return container.opds.navigation_feed(
        root,
        title=title,
        parameters=parameters,
        entries=entries,
        modified=_catalog_modified(container),
    )


async def _category_entries(
    container: Container, root: str, library: str
) -> NavigationEntries:
    groups = await run_in_threadpool(container.repository.categories, library)
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
    container: Container, root: str, library: str, category: str
) -> NavigationEntries:
    groups = await run_in_threadpool(container.repository.series, library, category)
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
    _: Annotated[Identity, Depends(authenticated)],
    library: str | None = None,
    category: str | None = None,
    series: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
) -> dict[str, object]:
    container = _container(request)
    page_size = container.settings.feed_page_size
    items, total = await run_in_threadpool(
        container.repository.publications,
        library=library,
        category=category,
        series=series,
        query=q,
        limit=page_size,
        offset=(page - 1) * page_size,
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


@router.api_route(
    "/api/v1/publications/{publication_id}/file",
    methods=["GET", "HEAD"],
    tags=["publications"],
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


@router.api_route(
    "/api/v1/publications/{publication_id}/pages/{number}",
    methods=["GET", "HEAD"],
    tags=["pages"],
)
async def publication_page(
    request: Request,
    publication_id: str,
    number: int,
    identity: Annotated[Identity, Depends(authenticated)],
    revision: str | None = None,
):
    container = _container(request)
    item = await run_in_threadpool(container.repository.page, publication_id, number)
    if not item:
        raise HTTPException(status_code=404, detail="Page not found")
    if not container.authorization.can_read(identity.user, item.publication):
        raise HTTPException(status_code=403, detail="Publication access denied")
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


@router.api_route(
    "/api/v1/publications/{publication_id}/cover",
    methods=["GET", "HEAD"],
    tags=["pages"],
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


async def _publication_or_404(
    request: Request, publication_id: str, identity: Identity
) -> Publication:
    publication = await run_in_threadpool(
        _container(request).repository.publication_by_id, publication_id
    )
    if not publication:
        raise HTTPException(status_code=404, detail="Publication not found")
    if not _container(request).authorization.can_read(identity.user, publication):
        raise HTTPException(status_code=403, detail="Publication access denied")
    return publication


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
