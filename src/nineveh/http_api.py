from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from pydantic import BaseModel, ConfigDict, Field, RootModel, WithJsonSchema
from pydantic.alias_generators import to_camel
from starlette.background import BackgroundTask

from .archives import (
    ArchiveChanged,
    ArchiveUnavailable,
    PageRenditionService,
    ThumbnailService,
)
from .auth import (
    AuthenticationError,
    AuthService,
    InvalidUserInput,
    LastAdministratorError,
)
from .catalog import IMAGE_TYPES, InvalidLibrary
from .deployment import memory_limit_text
from .domain import (
    AccessGrant,
    CatalogSeries,
    LibrarianEvent,
    LibrarianToken,
    LibraryUsage,
    ManagedLibrary,
    Page,
    Publication,
    ReadingProgress,
    Session,
    User,
)
from .librarian import (
    LibrarianConflict,
    LibrarianError,
    LibrarianNotFound,
    LibrarianTooLarge,
)
from .metadata import MetadataError
from .opds import CBZ_MEDIA_TYPE, NAVIGATION_PATH, PUBLICATIONS_PATH
from .opds import url as opds_url

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from .app import Container

SESSION_COOKIE = "nineveh_session"
NavigationEntries = tuple[str, dict[str, str], list[tuple[str, int, str]]]
basic_auth = HTTPBasic(auto_error=False)
# A declared scheme rather than a hand-parsed header, so the contract says so
# and a client generated from the librarian slice knows it needs a token.
librarian_bearer = HTTPBearer(
    scheme_name="LibrarianToken",
    description="An `nvh_` secret, issued under Admin → Librarian.",
    auto_error=False,
)


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


class SpreadDetectionInput(BaseModel):
    enabled: bool


class SpreadStartInput(BaseModel):
    """`null` restores automatic detection for the volume."""

    page: int | None = Field(default=None, ge=2)


ReadingMode = Literal["single", "double", "scroll"]


class ProgressUpdate(BaseModel):
    page: int = Field(ge=1)
    mode: ReadingMode
    completed: bool = False


# --------------------------------------------------------------------------
# Response bodies of the reading app's slice of the contract
# --------------------------------------------------------------------------

# The builders emit `datetime.isoformat()`. Kept a string so validation cannot
# re-render it (a parsed datetime would come back with `Z`, not `+00:00`), and
# documented as a date so generators still decode one.
Timestamp = Annotated[str, WithJsonSchema({"type": "string", "format": "date-time"})]
# Metadata numbers arrive as whichever of int or float the provider or editor
# produced. A `float` field would turn 91 into 91.0; this passes both through.
Number = Annotated[int | float, WithJsonSchema({"type": "number"})]
# Pages and covers are rendered at a few fixed widths and any other is a 422,
# so the contract lists them rather than leaving a client to learn from the
# error. Validated as a plain int: a query value arrives as text, which a
# `Literal[640, ...]` refuses outright; the service checks membership.
PageWidth = Annotated[
    int,
    WithJsonSchema(
        {"type": "integer", "enum": sorted(PageRenditionService.ALLOWED_WIDTHS)}
    ),
]
CoverWidth = Annotated[
    int,
    WithJsonSchema({"type": "integer", "enum": sorted(ThumbnailService.ALLOWED_WIDTHS)}),
]


def _open_to_clients(schema: dict[str, object]) -> None:
    """Publish a response as open to new fields, whatever the server enforces.

    `extra="forbid"` checks our own builders. Published, it becomes
    `additionalProperties: false`, which tells a client to reject any field
    added later -- swift-openapi-generator enforces exactly that -- so every
    compatible addition would break apps already installed.
    """
    schema.pop("additionalProperties", None)


class ResponseBody(BaseModel):
    """A response the route validates on the way out.

    Handlers keep returning the builders' plain dicts. Unknown keys are refused
    rather than dropped, so a builder that grows a field fails its tests until
    its model grows too, and the published contract cannot fall behind. Strict,
    so a value of the wrong type is refused too: lax validation would turn a
    builder's "2" into the documented 2 here and nowhere else.
    Fields defaulting to None are the ones a builder omits when they do not
    apply; routes that carry any set `response_model_exclude_unset` so they
    stay omitted instead of turning into nulls.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        strict=True,
        json_schema_extra=_open_to_clients,
    )


class UserAccount(ResponseBody):
    id: str
    username: str
    is_admin: bool
    enabled: bool
    created_at: Timestamp


class ReadingPosition(ResponseBody):
    publication_id: str
    page: int
    mode: ReadingMode
    completed: bool
    updated_at: Timestamp


class ManifestPage(ResponseBody):
    number: int
    href: str
    type: str
    length: int
    # Null only when the image could not be measured.
    width: int | None
    height: int | None
    spread: bool


class PageManifest(ResponseBody):
    publication_id: str
    revision: str
    total_pages: int
    start: int
    end: int
    pages: list[ManifestPage]
    next: str | None = None
    pairing_anchor: int | None = None


class MetadataValues(BaseModel):
    """Keys stay snake_case: they are the field names the editor posts back."""

    model_config = ConfigDict(
        extra="forbid", strict=True, json_schema_extra=_open_to_clients
    )

    title: str | None
    alternative_titles: list[str]
    authors: list[str]
    artists: list[str]
    description: str | None
    published_start: str | None
    published_end: str | None
    status: str | None
    content_rating: str | None
    media_type: str | None
    rating: Number | None
    publishers: list[str]
    tags: list[str]
    final_volume: Number | None
    total_chapters: Number | None


class SeriesMetadataRecord(ResponseBody):
    provider: str
    provider_id: int
    source_url: str
    fetched_at: Timestamp
    # The provider's own string, passed through unparsed.
    provider_updated_at: str | None
    values: MetadataValues
    edited_fields: list[str]
    license: str


class SeriesDetail(ResponseBody):
    id: str
    library_id: str
    library: str
    category: Literal["comics", "manga"]
    local_name: str
    title: str
    publication_count: int
    cover: str
    metadata: SeriesMetadataRecord | None


class NamedEntry(ResponseBody):
    name: str


class SeriesEntry(ResponseBody):
    name: str
    # `urn:uuid:` and the id `/api/v1/series/{series_id}` takes.
    identifier: str | None = None
    # Omitted when the volume's number is not one, such as "12a" or "Special".
    position: Number | None = None


class SeriesMembership(ResponseBody):
    series: list[SeriesEntry]


class PublicationMetadata(ResponseBody):
    type: str = Field(alias="@type")
    identifier: str
    title: str
    modified: Timestamp
    number_of_pages: int
    belongs_to: SeriesMembership
    description: str | None = None
    author: list[NamedEntry] | None = None


class LinkProperties(ResponseBody):
    length: int


class PublicationLink(ResponseBody):
    rel: str
    href: str
    type: str
    title: str
    templated: bool | None = None
    properties: LinkProperties | None = None


class PublicationImage(ResponseBody):
    href: str
    type: str
    width: int


class PublicationDetail(ResponseBody):
    """The same OPDS 2.0 publication object a publications feed lists."""

    metadata: PublicationMetadata
    links: list[PublicationLink]
    images: list[PublicationImage]


# The OPDS feeds follow a published spec, but which of its fields Nineveh
# fills -- and the query values an app files its catalog by -- are Nineveh's,
# so they are described like any other response.


class OpdsLink(ResponseBody):
    rel: str
    href: str
    type: str
    # Set only on the search link, whose href is a URI template.
    templated: bool | None = None


class NavigationProperties(ResponseBody):
    number_of_items: int


class NavigationLink(ResponseBody):
    """A library, category or series one level down, with how much it holds."""

    title: str
    href: str
    type: str
    properties: NavigationProperties


class FeedMetadata(ResponseBody):
    title: str
    modified: Timestamp
    number_of_items: int


class PublicationFeedMetadata(FeedMetadata):
    items_per_page: int
    current_page: int


class NavigationFeed(ResponseBody):
    """The catalog root, a library's categories, or a category's series."""

    metadata: FeedMetadata
    links: list[OpdsLink]
    navigation: list[NavigationLink]


class PublicationFeed(ResponseBody):
    """One page of publications; the `next` link, when present, is the rest."""

    metadata: PublicationFeedMetadata
    links: list[OpdsLink]
    publications: list[PublicationDetail]


class AuthenticationLabels(ResponseBody):
    login: str
    password: str


class AuthenticationMethod(ResponseBody):
    type: str
    labels: AuthenticationLabels


class AuthenticationDocument(ResponseBody):
    """OPDS Authentication 1.0: how to sign in, readable before signing in."""

    id: str
    title: str
    description: str
    authentication: list[AuthenticationMethod]
    links: list[OpdsLink]


# Routes that send a file. Left to FastAPI, each would be documented as JSON --
# its default -- and a generated client would try to decode a cover as JSON.
# A HEAD answer has no body at all, so its operation documents headers only:
# a client told to expect a body fails every HEAD request it makes.
ETAG_HEADER = {"ETag": {"schema": {"type": "string"}}}


def _file_body(*media_types: str) -> dict[str, object]:
    """Route options for a GET that answers with a file of one of these types."""
    binary = {"schema": {"type": "string", "format": "binary"}}
    return {
        "response_class": Response,
        "responses": {
            200: {
                "description": "The file.",
                "headers": ETAG_HEADER,
                "content": {media_type: binary for media_type in media_types},
            }
        },
    }


def _file_headers() -> dict[str, object]:
    """Route options for the HEAD twin of a file route."""
    return {
        "response_class": Response,
        "responses": {
            200: {
                "description": "The file's headers, without its body.",
                "headers": {
                    **ETAG_HEADER,
                    "Content-Length": {"schema": {"type": "integer"}},
                },
            }
        },
    }


# Every type the scanner admits as a page, plus the WebP of a resized copy.
PAGE_TYPES = sorted({*IMAGE_TYPES.values(), "image/webp"})


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
    response_model=AuthenticationDocument,
    response_model_exclude_unset=True,
    tags=["opds"],
)
async def opds_authentication(request: Request) -> dict[str, object]:
    return _container(request).opds.authentication_document(base_url(request))


@router.get(
    "/opds/v2/catalog.json",
    response_class=OpdsResponse,
    response_model=NavigationFeed,
    response_model_exclude_unset=True,
    tags=["opds"],
)
async def opds_catalog(
    request: Request, identity: Annotated[Identity, Depends(authenticated)]
) -> dict[str, object]:
    container = _container(request)
    scope = container.authorization.read_scope(identity.user)
    libraries = await run_in_threadpool(container.repository.libraries, scope)
    return container.opds.root_feed(
        base_url(request), libraries, _catalog_modified(container)
    )


@router.get(
    "/opds/v2/navigation.json",
    response_class=OpdsResponse,
    response_model=NavigationFeed,
    response_model_exclude_unset=True,
    tags=["opds"],
)
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


@router.get(
    "/opds/v2/publications.json",
    response_class=OpdsResponse,
    response_model=PublicationFeed,
    response_model_exclude_unset=True,
    tags=["opds"],
)
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


@router.get(
    "/api/v1/publications/{publication_id}",
    response_model=PublicationDetail,
    response_model_exclude_unset=True,
    tags=["publications"],
)
async def publication_detail(
    request: Request,
    publication_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
) -> dict[str, object]:
    publication = await _publication_or_404(request, publication_id, identity)
    return _container(request).opds.publication(base_url(request), publication)


@router.get("/api/v1/series/{series_id}", response_model=SeriesDetail, tags=["series"])
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
    **_file_body("image/webp"),
)
@router.head(
    "/api/v1/series/{series_id}/cover",
    tags=["series"],
    operation_id="headSeriesCover",
    **_file_headers(),
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
    **_file_body(CBZ_MEDIA_TYPE),
)
@router.head(
    "/api/v1/publications/{publication_id}/file",
    tags=["publications"],
    operation_id="headPublicationFile",
    **_file_headers(),
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


@router.get(
    "/api/v1/publications/{publication_id}/pages",
    response_model=PageManifest,
    response_model_exclude_unset=True,
    tags=["pages"],
)
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
    pairing_anchor = None
    if container.spreads:
        try:
            pairing_anchor = await run_in_threadpool(
                container.spreads.anchor_for, publication
            )
        except ArchiveChanged as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArchiveUnavailable as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
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
        pairing_anchor,
    )


def _manifest_body(
    root: str,
    publication: Publication,
    pages: list[Page],
    start: int,
    end: int,
    limit: int,
    pairing_anchor: int | None = None,
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
    if pairing_anchor is not None:
        body["pairingAnchor"] = pairing_anchor
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


@router.get(
    "/api/v1/publications/{publication_id}/range",
    tags=["pages"],
    **_file_body(CBZ_MEDIA_TYPE),
)
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
    **_file_body(*PAGE_TYPES),
)
@router.head(
    "/api/v1/publications/{publication_id}/pages/{number}",
    tags=["pages"],
    operation_id="headPublicationPage",
    **_file_headers(),
)
async def publication_page(
    request: Request,
    publication_id: str,
    number: int,
    identity: Annotated[Identity, Depends(authenticated)],
    revision: str | None = None,
    width: Annotated[
        PageWidth | None,
        Query(description="Serve a copy no wider than this instead of the original."),
    ] = None,
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
    if width is not None:
        resized = await _page_rendition(request, container, item, width, revision)
        if resized is not None:
            return resized
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


async def _page_rendition(request: Request, container, item, width: int, revision):
    """A width-bounded copy, or None when the original is the better answer."""
    try:
        path = await run_in_threadpool(
            container.renditions.rendition, item.publication, item.page, width
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ArchiveChanged as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ArchiveUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if path is None:
        return None
    etag = _etag(f"{item.publication.revision}-{item.page.crc:08x}-w{width}")
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    return FileResponse(
        path,
        media_type="image/webp",
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=31536000, immutable"
            if revision
            else "private, no-cache",
        },
    )


# One handler, registered once per verb so each OpenAPI operation carries a
# unique id -- generators reject a spec that repeats one.
@router.get(
    "/api/v1/publications/{publication_id}/cover",
    tags=["pages"],
    operation_id="readPublicationCover",
    **_file_body("image/webp"),
)
@router.head(
    "/api/v1/publications/{publication_id}/cover",
    tags=["pages"],
    operation_id="headPublicationCover",
    **_file_headers(),
)
async def publication_cover(
    request: Request,
    publication_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
    width: Annotated[CoverWidth, Query()] = 320,
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


@router.get(
    "/api/v1/publications/{publication_id}/progress",
    response_model=ReadingPosition,
    tags=["reader"],
)
async def reading_progress(
    request: Request,
    publication_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
) -> dict[str, object]:
    """Where the authenticated reader left off in this publication."""
    publication = await _publication_or_404(request, publication_id, identity)
    progress = await run_in_threadpool(
        _container(request).repository.reading_progress,
        identity.user.id,
        publication.id,
    )
    if not progress:
        raise HTTPException(status_code=404, detail="Nothing has been read yet")
    return _public_progress(progress)


@router.put(
    "/api/v1/publications/{publication_id}/progress",
    response_model=ReadingPosition,
    tags=["reader"],
)
async def save_reading_progress(
    request: Request,
    publication_id: str,
    body: ProgressUpdate,
    identity: Annotated[Identity, Depends(authenticated)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    """Record a reading position. Only the final page may complete a volume."""
    require_api_csrf(request, identity, csrf_token)
    container = _container(request)
    publication = await _publication_or_404(request, publication_id, identity)
    try:
        saved = await run_in_threadpool(
            container.reader.save_progress,
            identity.user.id,
            publication,
            body.page,
            body.mode,
            body.completed,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _public_progress(saved)


@router.delete(
    "/api/v1/publications/{publication_id}/progress",
    status_code=204,
    tags=["reader"],
)
async def clear_reading_progress(
    request: Request,
    publication_id: str,
    identity: Annotated[Identity, Depends(authenticated)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
):
    """Forget the reading position, returning the publication to unread."""
    require_api_csrf(request, identity, csrf_token)
    publication = await _publication_or_404(request, publication_id, identity)
    await run_in_threadpool(
        _container(request).reader.mark_as_unread, identity.user.id, publication.id
    )
    return Response(status_code=204)


def _public_progress(progress: ReadingProgress) -> dict[str, object]:
    return {
        "publicationId": progress.publication_id,
        "page": progress.page,
        "mode": progress.mode,
        "completed": progress.completed,
        "updatedAt": progress.updated_at.isoformat(),
    }


@router.get("/api/v1/auth/me", response_model=UserAccount, tags=["authentication"])
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
    "/api/v1/admin/libraries/{library_id}/metadata/auto-match",
    status_code=202,
    tags=["administration"],
)
async def admin_library_metadata_auto_match(
    request: Request,
    library_id: str,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    container = _container(request)
    _metadata_service(container)
    library = await run_in_threadpool(container.repository.managed_library, library_id)
    if not library or not library.enabled:
        raise HTTPException(status_code=404, detail="Managed library not found")
    current = request.app.state.metadata_task
    if current is not None and not current.done():
        raise HTTPException(status_code=409, detail="A metadata job is already running")
    items = await run_in_threadpool(
        container.repository.catalog_series, library_id=library_id, category="manga"
    )
    if not items:
        raise HTTPException(status_code=409, detail="This library has no manga series")
    states = await run_in_threadpool(container.repository.series_metadata_states)
    series_ids = [
        item.id
        for item in items
        if not states.get(item.id) or not states[item.id].matched
    ]
    if not series_ids:
        raise HTTPException(
            status_code=409, detail="Every manga series is already linked"
        )
    try:
        job = await run_in_threadpool(
            container.repository.create_metadata_auto_match_job,
            library_id,
            series_ids,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    request.app.state.start_metadata_auto_match(job.id)
    return {
        "status": "accepted",
        "jobId": job.id,
        "seriesCount": job.total,
        "statusUrl": f"/api/v1/admin/libraries/{library_id}/metadata/auto-match",
    }


@router.get(
    "/api/v1/admin/libraries/{library_id}/metadata/auto-match",
    tags=["administration"],
)
async def admin_library_metadata_auto_match_status(
    request: Request,
    library_id: str,
    _: Annotated[Identity, Depends(administrator)],
) -> dict[str, object]:
    container = _container(request)
    library = await run_in_threadpool(container.repository.managed_library, library_id)
    if not library or not library.enabled:
        raise HTTPException(status_code=404, detail="Managed library not found")
    job = await run_in_threadpool(
        container.repository.latest_metadata_auto_match_job, library_id
    )
    if not job:
        return {"job": None}
    return {
        "job": {
            "id": job.id,
            "status": job.status,
            "total": job.total,
            "completed": job.completed,
            "linked": job.linked,
            "review": job.review,
            "failed": job.failed,
            "createdAt": job.created_at.isoformat(),
            "updatedAt": job.updated_at.isoformat(),
        }
    }


@router.post(
    "/api/v1/admin/series/{series_id}/spread-detection",
    tags=["administration"],
)
async def admin_series_spread_detection(
    request: Request,
    series_id: str,
    body: SpreadDetectionInput,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf_token)
    container = _container(request)
    series = await run_in_threadpool(
        container.repository.catalog_series_by_id, series_id
    )
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    await run_in_threadpool(
        container.repository.set_series_spread_detection, series_id, body.enabled
    )
    started = False
    if body.enabled:
        started = request.app.state.start_spread_detection([series_id], force=True)
    return {"enabled": body.enabled, "analysisStarted": started}


@router.put(
    "/api/v1/admin/publications/{publication_id}/spread-start",
    tags=["administration"],
)
async def admin_publication_spread_start(
    request: Request,
    publication_id: str,
    body: SpreadStartInput,
    identity: Annotated[Identity, Depends(administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    """Pin one volume's pairing start, or send null to restore detection."""
    require_api_csrf(request, identity, csrf_token)
    container = _container(request)
    publication = await run_in_threadpool(
        container.repository.publication_by_id, publication_id
    )
    if not publication:
        raise HTTPException(status_code=404, detail="Publication not found")
    if body.page is not None and body.page > publication.page_count:
        raise HTTPException(
            status_code=422, detail="Start page is past the end of this volume"
        )
    await run_in_threadpool(
        container.repository.set_publication_spread_override,
        publication_id,
        body.page,
    )
    return {"publicationId": publication_id, "page": body.page}


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
    **_file_body("image/webp"),
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


# --------------------------------------------------------------------------
# Librarian agent
#
# Two rules shape this surface. Every route is named for one operation, so a
# small model picks between four distinct verbs rather than a family of
# lookalikes; and staging is a different scope from committing, so a token can
# be allowed to propose an upload without being allowed to perform one.
# --------------------------------------------------------------------------


class LibrarianTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(min_length=1)
    library_ids: list[str] = Field(default_factory=list)


class LibrarianTokenUpdate(BaseModel):
    """Every field optional: a PATCH may change one facet and leave the rest."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    scopes: list[str] | None = Field(default=None, min_length=1)
    library_ids: list[str] | None = None


class LibrarianCommitInput(BaseModel):
    filename: str | None = Field(default=None, max_length=255)


# Response bodies of the librarian's slice, held to the same rules as the
# reading app's: validated on the way out, published open to new fields.


class LibraryEntry(ResponseBody):
    id: str
    name: str
    relative_path: str


class LibraryList(ResponseBody):
    libraries: list[LibraryEntry]


class TitleMatch(ResponseBody):
    series_id: str
    library_id: str
    library: str
    category: Literal["comics", "manga"]
    local_name: str
    publication_count: int
    score: float
    matched_on: str = Field(description="localName, title or alternativeTitle.")
    matched_value: str


class SeriesSummary(ResponseBody):
    series_id: str
    library_id: str
    library: str
    category: Literal["comics", "manga"]
    local_name: str
    title: str
    publication_count: int


class SeriesCandidates(ResponseBody):
    """Resolving a title ranks matches; a metadata search lists summaries and
    never names a confident match."""

    candidates: list[TitleMatch | SeriesSummary]
    confident_match: str | None
    ambiguous: bool


class InventoryVolume(ResponseBody):
    id: str
    filename: str
    title: str
    number: str | None
    size: int
    page_count: int


class VolumeInventory(ResponseBody):
    publication_count: int
    total_size: int
    latest: InventoryVolume | None
    filenames: list[str]
    publications: list[InventoryVolume]


class ProviderTotals(BaseModel):
    """Keys stay snake_case, like the metadata values they are copied from.
    One the provider has no figure for is left out rather than sent as null."""

    model_config = ConfigDict(
        extra="forbid", strict=True, json_schema_extra=_open_to_clients
    )

    total_chapters: Number | None = None
    final_volume: Number | None = None
    status: str | None = None


class SeriesInventoryDetail(ResponseBody):
    series: SeriesSummary
    inventory: VolumeInventory
    provider_totals: ProviderTotals


class DuplicateVolume(ResponseBody):
    publication_id: str
    filename: str


class StagedIngest(ResponseBody):
    ingest_id: str
    state: Literal["staged"]
    series_id: str
    filename: str
    suggested_filename: str
    # Null when the series' filenames share no numbering to follow.
    sibling_pattern: str | None
    target_path: str
    size: int
    page_count: int
    sha256: str
    duplicate_of: DuplicateVolume | None
    created_at: Timestamp


class PendingIngests(ResponseBody):
    pending: list[StagedIngest]


class IngestOutcome(ResponseBody):
    """What became of an upload whose staged record is gone."""

    ingest_id: str
    state: str = Field(description="placed, conflict or rejected.")
    # Null unless the upload was placed.
    relative_path: str | None
    committed_at: Timestamp
    summary: str


# Named, so a generated client gets a type called this rather than one named
# after the route.
class IngestStatus(RootModel[StagedIngest | IngestOutcome]):
    """Still staged, or what became of it."""


class PlacedIngest(ResponseBody):
    ingest_id: str
    state: Literal["placed"]
    series_id: str
    filename: str
    relative_path: str
    size: int
    page_count: int
    scan_started: bool


async def librarian_identity(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(librarian_bearer)
    ],
) -> LibrarianToken:
    secret = credentials.credentials if credentials else None
    token = await run_in_threadpool(_container(request).librarian_auth.verify, secret)
    if token is None:
        raise HTTPException(status_code=401, detail="Librarian authentication required")
    return token


def _correlation(request: Request) -> str:
    """Group a resolve/stage/commit chain so the feed reads as one action."""
    return request.headers.get("x-correlation-id") or str(uuid.uuid4())


async def _require_librarian_scope(
    request: Request, token: LibrarianToken, scope: str
) -> None:
    if token.permits(scope):
        return
    await run_in_threadpool(
        _container(request).audit.record,
        kind="usage",
        action="scope.denied",
        severity="security",
        outcome="denied",
        summary=f"“{token.name}” was refused {scope}",
        token=token,
        correlation_id=_correlation(request),
        detail={"scope": scope},
    )
    raise HTTPException(status_code=403, detail=f"Librarian token lacks {scope}")


def _librarian_error(error: LibrarianError) -> HTTPException:
    if isinstance(error, LibrarianNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, LibrarianConflict):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, LibrarianTooLarge):
        return HTTPException(status_code=413, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


@router.get(
    "/api/v1/librarian/libraries", response_model=LibraryList, tags=["librarian"]
)
async def librarian_libraries(
    request: Request,
    token: Annotated[LibrarianToken, Depends(librarian_identity)],
) -> dict[str, object]:
    await _require_librarian_scope(request, token, "catalog:read")
    container = _container(request)
    libraries = await run_in_threadpool(container.librarian.libraries, token)
    await run_in_threadpool(
        container.audit.record,
        kind="usage",
        action="libraries.list",
        severity="info",
        outcome="ok",
        summary=f"“{token.name}” listed {len(libraries)} libraries",
        token=token,
        correlation_id=_correlation(request),
    )
    return {
        "libraries": [
            {"id": item.id, "name": item.name, "relativePath": item.relative_path}
            for item in libraries
        ]
    }


@router.get(
    "/api/v1/librarian/series", response_model=SeriesCandidates, tags=["librarian"]
)
async def librarian_series(
    request: Request,
    token: Annotated[LibrarianToken, Depends(librarian_identity)],
    query: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    library: Annotated[str | None, Query(max_length=200)] = None,
    author: Annotated[str | None, Query(max_length=200)] = None,
    artist: Annotated[str | None, Query(max_length=200)] = None,
    publisher: Annotated[str | None, Query(max_length=200)] = None,
    status_filter: Annotated[str | None, Query(alias="status", max_length=64)] = None,
    tag: Annotated[str | None, Query(max_length=64)] = None,
    title: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, object]:
    """One collection, two questions.

    Title resolution and metadata filtering are the same lookup with different
    predicates, so they share a route; the harness exposes them as two
    distinctly named tools. Keeping them as sibling endpoints is what makes a
    small model pick the wrong one.
    """
    container = _container(request)
    correlation = _correlation(request)
    filters = (author, artist, publisher, status_filter, tag, title)
    if query is None and not any(filters):
        raise HTTPException(
            status_code=422, detail="Provide query, or at least one metadata filter"
        )
    if query is not None:
        await _require_librarian_scope(request, token, "catalog:read")
        try:
            resolution = await run_in_threadpool(
                container.librarian.resolve, token, query, library=library, limit=limit
            )
        except LibrarianError as error:
            raise _librarian_error(error) from error
        await run_in_threadpool(
            container.audit.record,
            kind="usage",
            action="series.resolve",
            severity="info",
            outcome="ok",
            summary=(
                f"“{token.name}” resolved “{query}” → "
                + (
                    f"{resolution.candidates[0].series.name} "
                    f"({resolution.candidates[0].score:.2f})"
                    if resolution.candidates
                    else "no match"
                )
            ),
            token=token,
            correlation_id=correlation,
            detail={"query": query, "matches": len(resolution.candidates)},
        )
        return {
            "candidates": [_match_payload(item) for item in resolution.candidates],
            "confidentMatch": resolution.confident_match,
            "ambiguous": resolution.ambiguous,
        }
    await _require_librarian_scope(request, token, "metadata:read")
    try:
        found = await run_in_threadpool(
            lambda: container.librarian.search_metadata(
                token,
                author=author,
                artist=artist,
                publisher=publisher,
                status=status_filter,
                tag=tag,
                title=title,
                limit=limit,
            )
        )
    except LibrarianError as error:
        raise _librarian_error(error) from error
    await run_in_threadpool(
        container.audit.record,
        kind="usage",
        action="metadata.search",
        severity="info",
        outcome="ok",
        summary=f"“{token.name}” searched metadata → {len(found)} series",
        token=token,
        correlation_id=correlation,
        detail={
            name: value
            for name, value in (
                ("author", author),
                ("artist", artist),
                ("publisher", publisher),
                ("status", status_filter),
                ("tag", tag),
                ("title", title),
            )
            if value
        },
    )
    return {
        "candidates": [_series_summary(series, metadata) for series, metadata in found],
        "confidentMatch": None,
        "ambiguous": len(found) != 1,
    }


@router.get(
    "/api/v1/librarian/series/{series_id}",
    response_model=SeriesInventoryDetail,
    response_model_exclude_unset=True,
    tags=["librarian"],
)
async def librarian_series_detail(
    request: Request,
    series_id: str,
    token: Annotated[LibrarianToken, Depends(librarian_identity)],
) -> dict[str, object]:
    await _require_librarian_scope(request, token, "catalog:read")
    container = _container(request)
    try:
        inventory = await run_in_threadpool(
            container.librarian.inventory, token, series_id
        )
    except LibrarianError as error:
        raise _librarian_error(error) from error
    latest = inventory.latest
    await run_in_threadpool(
        container.audit.record,
        kind="usage",
        action="series.inventory",
        severity="info",
        outcome="ok",
        summary=(
            f"“{token.name}” read {inventory.series.name} — "
            f"{len(inventory.publications)} volumes"
        ),
        token=token,
        correlation_id=_correlation(request),
        subject=("series", inventory.series.id, inventory.series.name),
    )
    return {
        "series": _series_summary(inventory.series, inventory.metadata),
        "inventory": {
            "publicationCount": len(inventory.publications),
            "totalSize": inventory.total_size,
            "latest": _publication_payload(latest) if latest else None,
            "filenames": [item.filename for item in inventory.publications],
            "publications": [
                _publication_payload(item) for item in inventory.publications
            ],
        },
        "providerTotals": inventory.provider_totals,
    }


@router.post(
    "/api/v1/librarian/ingest",
    status_code=201,
    response_model=StagedIngest,
    tags=["librarian"],
)
async def librarian_ingest_stage(
    request: Request,
    token: Annotated[LibrarianToken, Depends(librarian_identity)],
    series_id: Annotated[str, Form(max_length=64)],
    filename: Annotated[str, Form(max_length=255)],
    file: Annotated[UploadFile, File()],
) -> dict[str, object]:
    await _require_librarian_scope(request, token, "ingest:stage")
    container = _container(request)
    correlation = _correlation(request)
    try:
        staged = await run_in_threadpool(
            container.ingest.stage, token, series_id, filename, file.file
        )
    except LibrarianError as error:
        await run_in_threadpool(
            container.audit.record,
            kind="usage",
            action="ingest.stage",
            severity="notice",
            outcome="rejected",
            summary=f"“{token.name}” upload rejected — {error}",
            token=token,
            correlation_id=correlation,
            detail={"filename": filename, "seriesId": series_id},
        )
        raise _librarian_error(error) from error
    finally:
        await file.close()
    await run_in_threadpool(
        container.audit.record,
        kind="usage",
        action="ingest.stage",
        severity="notice",
        outcome="ok",
        summary=(
            f"“{token.name}” staged {staged.suggested_filename} "
            f"({_readable(staged.size)}, {staged.page_count} pages)"
        ),
        token=token,
        correlation_id=correlation,
        subject=("ingest", staged.id, staged.suggested_filename),
        detail={"targetPath": staged.target_path, "sha256": staged.sha256},
    )
    return _staged_payload(staged)


@router.get(
    "/api/v1/librarian/ingest", response_model=PendingIngests, tags=["librarian"]
)
async def librarian_ingest_pending(
    request: Request,
    token: Annotated[LibrarianToken, Depends(librarian_identity)],
) -> dict[str, object]:
    """What is waiting for a commit — the approve-at-the-desk queue."""
    await _require_librarian_scope(request, token, "ingest:stage")
    pending = await run_in_threadpool(_container(request).ingest.pending, token)
    return {"pending": [_staged_payload(item) for item in pending]}


@router.get(
    "/api/v1/librarian/ingest/{ingest_id}",
    response_model=IngestStatus,
    tags=["librarian"],
)
async def librarian_ingest_detail(
    request: Request,
    ingest_id: str,
    token: Annotated[LibrarianToken, Depends(librarian_identity)],
) -> dict[str, object]:
    await _require_librarian_scope(request, token, "ingest:stage")
    container = _container(request)
    staged = await run_in_threadpool(container.ingest.staged, token, ingest_id)
    if staged is not None:
        return _staged_payload(staged)
    # The staged record is gone, but the activity feed remembers what happened
    # to it. An agent that lost its connection can still learn the outcome.
    events = await run_in_threadpool(
        container.audit.events, correlation_id=None, action="ingest.commit", limit=200
    )
    for event in events:
        if event.subject_id == ingest_id:
            return {
                "ingestId": ingest_id,
                "state": "placed" if event.outcome == "ok" else event.outcome,
                "relativePath": (event.detail or {}).get("relativePath"),
                "committedAt": event.created_at.isoformat(),
                "summary": event.summary,
            }
    raise HTTPException(status_code=404, detail="Staged upload not found or expired")


@router.post(
    "/api/v1/librarian/ingest/{ingest_id}/commit",
    response_model=PlacedIngest,
    tags=["librarian"],
)
async def librarian_ingest_commit(
    request: Request,
    ingest_id: str,
    token: Annotated[LibrarianToken, Depends(librarian_identity)],
    body: LibrarianCommitInput | None = None,
) -> dict[str, object]:
    await _require_librarian_scope(request, token, "ingest:commit")
    container = _container(request)
    correlation = _correlation(request)
    try:
        placed = await run_in_threadpool(
            container.ingest.commit,
            token,
            ingest_id,
            body.filename if body else None,
        )
    except LibrarianError as error:
        await run_in_threadpool(
            container.audit.record,
            kind="usage",
            action="ingest.commit",
            severity="important",
            outcome="conflict" if isinstance(error, LibrarianConflict) else "rejected",
            summary=f"“{token.name}” commit refused — {error}",
            token=token,
            correlation_id=correlation,
            subject=("ingest", ingest_id, ingest_id),
        )
        raise _librarian_error(error) from error
    await run_in_threadpool(
        container.audit.record,
        kind="usage",
        action="ingest.commit",
        severity="important",
        outcome="ok",
        summary=f"“{token.name}” placed {placed.relative_path}",
        token=token,
        correlation_id=correlation,
        subject=("ingest", ingest_id, placed.filename),
        detail={"relativePath": placed.relative_path, "size": placed.size},
    )
    request.app.state.start_scan(placed.library_id)
    return {
        "ingestId": ingest_id,
        "state": "placed",
        "seriesId": placed.series_id,
        "filename": placed.filename,
        "relativePath": placed.relative_path,
        "size": placed.size,
        "pageCount": placed.page_count,
        "scanStarted": True,
    }


@router.delete(
    "/api/v1/librarian/ingest/{ingest_id}", status_code=204, tags=["librarian"]
)
async def librarian_ingest_discard(
    request: Request,
    ingest_id: str,
    token: Annotated[LibrarianToken, Depends(librarian_identity)],
) -> Response:
    await _require_librarian_scope(request, token, "ingest:stage")
    container = _container(request)
    if not await run_in_threadpool(container.ingest.discard, token, ingest_id):
        raise HTTPException(status_code=404, detail="Staged upload not found")
    await run_in_threadpool(
        container.audit.record,
        kind="usage",
        action="ingest.discard",
        severity="notice",
        outcome="ok",
        summary=f"“{token.name}” discarded a staged upload",
        token=token,
        correlation_id=_correlation(request),
        subject=("ingest", ingest_id, ingest_id),
    )
    return Response(status_code=204)


@router.get("/api/v1/admin/librarian-tokens", tags=["admin"])
async def list_librarian_tokens(
    request: Request,
    identity: Annotated[Identity, Depends(administrator)],
    include_revoked: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    tokens = await run_in_threadpool(
        _container(request).librarian_auth.tokens, include_revoked=include_revoked
    )
    return {"tokens": [_token_payload(item) for item in tokens]}


@router.post("/api/v1/admin/librarian-tokens", status_code=201, tags=["admin"])
async def create_librarian_token(
    request: Request,
    body: LibrarianTokenCreate,
    identity: Annotated[Identity, Depends(administrator)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf)
    try:
        token, secret = await run_in_threadpool(
            _container(request).librarian_auth.issue,
            body.name,
            tuple(body.scopes),
            tuple(body.library_ids),
            actor=identity.user.username,
        )
    except LibrarianError as error:
        raise _librarian_error(error) from error
    # The only time the secret exists in a response. It is stored as a hash.
    return {**_token_payload(token), "secret": secret}


@router.patch("/api/v1/admin/librarian-tokens/{token_id}", tags=["admin"])
async def update_librarian_token(
    request: Request,
    token_id: str,
    body: LibrarianTokenUpdate,
    identity: Annotated[Identity, Depends(administrator)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    """Re-scope a live token. The secret is never rotated by this call."""
    require_api_csrf(request, identity, csrf)
    try:
        updated = await run_in_threadpool(
            lambda: _container(request).librarian_auth.update(
                token_id,
                name=body.name,
                scopes=tuple(body.scopes) if body.scopes is not None else None,
                library_ids=(
                    tuple(body.library_ids) if body.library_ids is not None else None
                ),
                actor=identity.user.username,
            )
        )
    except LibrarianError as error:
        raise _librarian_error(error) from error
    if updated is None:
        raise HTTPException(status_code=404, detail="Librarian token not found")
    return _token_payload(updated)


@router.delete("/api/v1/admin/librarian-tokens/{token_id}", tags=["admin"])
async def revoke_librarian_token(
    request: Request,
    token_id: str,
    identity: Annotated[Identity, Depends(administrator)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, object]:
    require_api_csrf(request, identity, csrf)
    revoked = await run_in_threadpool(
        _container(request).librarian_auth.revoke,
        token_id,
        actor=identity.user.username,
    )
    if revoked is None:
        raise HTTPException(status_code=404, detail="Librarian token not found")
    return _token_payload(revoked)


@router.get("/api/v1/admin/librarian/activity", tags=["admin"])
async def librarian_activity(
    request: Request,
    identity: Annotated[Identity, Depends(administrator)],
    token_id: Annotated[str | None, Query()] = None,
    severity: Annotated[
        Literal["info", "notice", "important", "security"] | None, Query()
    ] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    outcome: Annotated[str | None, Query(max_length=32)] = None,
    correlation_id: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query(max_length=40)] = None,
    until: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, object]:
    """Lifecycle and usage in one chronological feed.

    Severity is a floor, so the default view can hide read noise without
    hiding writes; `info` opts back into the resolves.
    """
    container = _container(request)
    current = {
        item.id: item.scopes
        for item in await run_in_threadpool(
            container.librarian_auth.tokens, include_revoked=True
        )
    }
    events = await run_in_threadpool(
        lambda: container.audit.events(
            token_id=token_id,
            severity=severity,
            action=action,
            outcome=outcome,
            correlation_id=correlation_id,
            since=since,
            until=until,
            limit=limit,
        )
    )
    return {"events": [_event_payload(item, current) for item in events]}


def _match_payload(match) -> dict[str, object]:
    series = match.series
    return {
        "seriesId": series.id,
        "libraryId": series.library_id,
        "library": series.library,
        "category": series.category,
        "localName": series.name,
        "publicationCount": series.publication_count,
        "score": round(match.score, 4),
        "matchedOn": match.matched_on,
        "matchedValue": match.matched_value,
    }


def _series_summary(series: CatalogSeries, metadata) -> dict[str, object]:
    return {
        "seriesId": series.id,
        "libraryId": series.library_id,
        "library": series.library,
        "category": series.category,
        "localName": series.name,
        "title": (metadata.title if metadata else None) or series.name,
        "publicationCount": series.publication_count,
    }


def _publication_payload(item: Publication) -> dict[str, object]:
    return {
        "id": item.id,
        "filename": item.filename,
        "title": item.title,
        "number": item.number,
        "size": item.size,
        "pageCount": item.page_count,
    }


def _staged_payload(staged) -> dict[str, object]:
    duplicate = staged.duplicate_of
    return {
        "ingestId": staged.id,
        "state": staged.state,
        "seriesId": staged.series_id,
        "filename": staged.filename,
        "suggestedFilename": staged.suggested_filename,
        "siblingPattern": staged.sibling_pattern,
        "targetPath": staged.target_path,
        "size": staged.size,
        "pageCount": staged.page_count,
        "sha256": staged.sha256,
        "duplicateOf": (
            {"publicationId": duplicate[0], "filename": duplicate[1]}
            if duplicate
            else None
        ),
        "createdAt": staged.created_at.isoformat(),
    }


def _token_payload(token: LibrarianToken) -> dict[str, object]:
    return {
        "id": token.id,
        "name": token.name,
        "scopes": list(token.scopes),
        "libraryIds": list(token.library_ids),
        "createdAt": token.created_at.isoformat(),
        "lastUsedAt": token.last_used_at.isoformat() if token.last_used_at else None,
        "revokedAt": token.revoked_at.isoformat() if token.revoked_at else None,
    }


def _event_payload(
    event: LibrarianEvent, current_scopes: dict[str, tuple[str, ...]]
) -> dict[str, object]:
    effective = list(event.scopes_at_time)
    now = current_scopes.get(event.token_id or "")
    return {
        "id": event.id,
        "kind": event.kind,
        "action": event.action,
        "severity": event.severity,
        "outcome": event.outcome,
        "summary": event.summary,
        "createdAt": event.created_at.isoformat(),
        "tokenId": event.token_id,
        "tokenName": event.token_name,
        "actor": event.actor,
        "correlationId": event.correlation_id,
        "scopesAtTime": effective,
        # The one comparison worth surfacing: this action ran under different
        # permissions than the token carries now.
        "scopesDiffer": now is not None and list(now) != effective and bool(effective),
        "subject": (
            {
                "type": event.subject_type,
                "id": event.subject_id,
                "label": event.subject_label,
            }
            if event.subject_type
            else None
        ),
        "detail": event.detail,
    }


def _readable(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size} B"
