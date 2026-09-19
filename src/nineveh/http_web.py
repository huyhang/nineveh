from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import AuthenticationError, InvalidUserInput, LastAdministratorError
from .catalog import InvalidLibrary
from .deployment import memory_limit_text
from .domain import AccessGrant, Session
from .http_api import SESSION_COOKIE, scan_active
from .metadata import (
    EDITABLE_FIELDS,
    MAX_COVER_BYTES,
    REFRESH_REMINDER_DAYS,
    MetadataError,
    matches_state,
)
from .units import gibibytes, since, timestamp

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(include_in_schema=False)


templates.env.filters["gib"] = gibibytes
templates.env.filters["since"] = since
templates.env.filters["timestamp"] = timestamp


def _container(request: Request):
    return request.app.state.container


async def _browser_session(request: Request) -> Session | None:
    return await run_in_threadpool(
        _container(request).auth.session, request.cookies.get(SESSION_COOKIE)
    )


async def _require_browser_session(request: Request) -> Session:
    session = await _browser_session(request)
    if not session:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return session


async def _require_admin(request: Request) -> Session:
    session = await _require_browser_session(request)
    if not _container(request).authorization.can_administer(session.user):
        raise HTTPException(status_code=403, detail="Administrator access required")
    return session


def _verify_csrf(request: Request, session: Session, token: str) -> None:
    if not _container(request).auth.valid_csrf(session, token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await _browser_session(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"service_title": _container(request).settings.service_title, "session": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(..., max_length=64),
    password: str = Form(..., max_length=1024),
):
    origin = request.headers.get("origin")
    allowed_origin = _container(request).settings.public_base_url or str(
        request.base_url
    )
    if origin and origin.rstrip("/") != allowed_origin.rstrip("/"):
        raise HTTPException(status_code=403, detail="Invalid request origin")
    container = _container(request)
    try:
        user = await run_in_threadpool(container.auth.authenticate, username, password)
    except AuthenticationError:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "service_title": container.settings.service_title,
                "session": None,
                "error": "Invalid username or password.",
            },
            status_code=401,
        )
    session = await run_in_threadpool(container.auth.new_session, user)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        session.token,
        max_age=container.settings.session_hours * 3600,
        secure=container.settings.secure_cookies,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    session = await _require_browser_session(request)
    _verify_csrf(request, session, csrf_token)
    await run_in_threadpool(
        _container(request).auth.end_session, request.cookies.get(SESSION_COOKIE)
    )
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/", response_class=HTMLResponse)
async def catalog(
    request: Request,
    library: str | None = None,
    category: str | None = None,
    series: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
):
    session = await _browser_session(request)
    if not session:
        return RedirectResponse("/login", status_code=303)
    container = _container(request)
    scope = container.authorization.read_scope(session.user)

    moved = await _legacy_filter_redirect(container, scope, library, category, series)
    if moved:
        return moved

    query = (q or "").strip()
    view = (
        await _search_results(container, scope, query, page)
        if query
        else await _library_shelves(container, scope)
    )
    return templates.TemplateResponse(
        request,
        "catalog.html",
        {
            "service_title": container.settings.service_title,
            "session": session,
            "mode": "search" if query else "libraries",
            "query": query,
            **view,
        },
    )


async def _legacy_filter_redirect(container, scope, library, category, series):
    """Keep the pre-hierarchy `?library=&category=&series=` links working."""
    if not library:
        return None
    managed = await _managed_library_named(container, library, scope)
    if not managed:
        return None
    if category and series:
        matches = await run_in_threadpool(
            container.repository.catalog_series,
            library_id=managed.id,
            category=category,
            query=series,
            scope=scope,
        )
        exact = next((item for item in matches if item.name == series), None)
        if exact:
            return RedirectResponse(f"/series/{exact.id}", status_code=303)
    if category:
        return RedirectResponse(f"/libraries/{managed.id}/{category}", status_code=303)
    return RedirectResponse(f"/libraries/{managed.id}", status_code=303)


async def _search_results(container, scope, query: str, page: int) -> dict[str, object]:
    matches = await run_in_threadpool(
        container.repository.catalog_series, query=query, scope=scope
    )
    summaries = await run_in_threadpool(container.repository.series_metadata_summaries)
    page_size = container.settings.feed_page_size
    page_count = max(1, math.ceil(len(matches) / page_size))
    page = min(page, page_count)
    start = (page - 1) * page_size
    cards = _series_cards(matches[start : start + page_size], summaries)
    return {
        "page": page,
        "page_count": page_count,
        "libraries": [],
        "series_cards": cards,
        "metadata_attribution": any(card["metadata"] for card in cards),
        "recent_publications": [],
        "total": len(matches),
        "previous_url": _search_url(query, page - 1) if page > 1 else None,
        "next_url": _search_url(query, page + 1) if page < page_count else None,
    }


async def _library_shelves(container, scope) -> dict[str, object]:
    """The landing view reads neither the series index nor stored metadata."""
    visible = dict(await run_in_threadpool(container.repository.libraries, scope))
    managed = await run_in_threadpool(container.repository.managed_libraries)
    libraries = [(item, visible[item.name]) for item in managed if item.name in visible]
    recent = await run_in_threadpool(
        container.repository.publications, limit=6, scope=scope
    )
    return {
        "page": 1,
        "page_count": 1,
        "libraries": libraries,
        "series_cards": [],
        "metadata_attribution": False,
        "recent_publications": recent[0],
        "total": sum(count for _, count in libraries),
        "previous_url": None,
        "next_url": None,
    }


@router.get("/libraries/{library_id}", response_class=HTMLResponse)
async def library_detail(request: Request, library_id: str):
    session = await _require_browser_session(request)
    container = _container(request)
    scope = container.authorization.read_scope(session.user)
    library = await run_in_threadpool(container.repository.managed_library, library_id)
    visible = dict(await run_in_threadpool(container.repository.libraries, scope))
    if not library or library.name not in visible:
        raise HTTPException(status_code=404, detail="Library not found")
    categories = await run_in_threadpool(
        container.repository.categories, library.name, scope
    )
    return templates.TemplateResponse(
        request,
        "catalog.html",
        {
            "service_title": container.settings.service_title,
            "session": session,
            "mode": "categories",
            "library": library,
            "categories": categories,
            "total": visible[library.name],
        },
    )


@router.get("/libraries/{library_id}/{category}", response_class=HTMLResponse)
async def category_detail(request: Request, library_id: str, category: str):
    session = await _require_browser_session(request)
    container = _container(request)
    scope = container.authorization.read_scope(session.user)
    library = await run_in_threadpool(container.repository.managed_library, library_id)
    if not library:
        raise HTTPException(status_code=404, detail="Library not found")
    cards = await run_in_threadpool(
        container.repository.catalog_series,
        library_id=library_id,
        category=category,
        scope=scope,
    )
    visible_categories = dict(
        await run_in_threadpool(container.repository.categories, library.name, scope)
    )
    if category not in visible_categories:
        raise HTTPException(status_code=404, detail="Category not found")
    metadata = await run_in_threadpool(container.repository.series_metadata_summaries)
    series_cards = _series_cards(cards, metadata)
    return templates.TemplateResponse(
        request,
        "catalog.html",
        {
            "service_title": container.settings.service_title,
            "session": session,
            "mode": "series",
            "library": library,
            "category": category,
            "series_cards": series_cards,
            "metadata_attribution": any(card["metadata"] for card in series_cards),
            "total": visible_categories[category],
        },
    )


@router.get("/series/{series_id}", response_class=HTMLResponse)
async def series_detail(request: Request, series_id: str):
    session = await _require_browser_session(request)
    container = _container(request)
    scope = container.authorization.read_scope(session.user)
    item = await run_in_threadpool(
        container.repository.catalog_series_by_id, series_id, scope
    )
    if not item:
        raise HTTPException(status_code=404, detail="Series not found")
    metadata = await run_in_threadpool(container.repository.series_metadata, series_id)
    publications, _ = await run_in_threadpool(
        container.repository.publications,
        library=item.library,
        category=item.category,
        series=item.name,
        limit=10_000,
        scope=scope,
    )
    return templates.TemplateResponse(
        request,
        "series.html",
        {
            "service_title": container.settings.service_title,
            "session": session,
            "series": item,
            "metadata": metadata,
            "details": metadata.effective if metadata else {},
            "display_title": _series_title(item, metadata),
            "publications": publications,
        },
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_overview(request: Request):
    context = await _admin_context(request, "overview")
    container = _container(request)
    usage = await run_in_threadpool(container.repository.library_usage)
    context.update(
        {
            "scan_status": container.scanner.status,
            "library_count": len(usage),
            "user_count": await run_in_threadpool(container.repository.user_count),
            "total_size": sum(item.size for item in usage),
            "publication_count": sum(item.publication_count for item in usage),
            "untrusted_proxy": request.app.state.untrusted_proxy,
            "trusted_proxies": container.settings.forwarded_allow_ips,
        }
    )
    return templates.TemplateResponse(request, "admin.html", context)


@router.get("/admin/libraries", response_class=HTMLResponse)
async def admin_libraries_page(request: Request):
    context = await _admin_context(request, "libraries")
    container = _container(request)
    context["library_usage"] = await run_in_threadpool(
        container.repository.library_usage
    )
    context["scan_status"] = container.scanner.status
    context["available_libraries"] = await run_in_threadpool(
        container.libraries.available
    )
    return templates.TemplateResponse(request, "admin_libraries.html", context)


@router.get("/libraries/{library_id}/{category}/metadata", response_class=HTMLResponse)
async def library_metadata_page(
    request: Request,
    library_id: str,
    category: str,
    q: str | None = Query(default=None, max_length=200),
    state: str | None = Query(default=None, max_length=20),
):
    session = await _require_admin(request)
    message, error = await _take_flash(request)
    container = _container(request)
    library = await run_in_threadpool(container.repository.managed_library, library_id)
    if not library or not library.enabled or category.casefold() != "manga":
        raise HTTPException(status_code=404, detail="Manga library not found")
    items = await run_in_threadpool(
        container.repository.catalog_series,
        library_id=library_id,
        category="manga",
        query=(q or "").strip() or None,
    )
    states = await run_in_threadpool(container.repository.series_metadata_states)
    rows = [
        {
            "series": item,
            "metadata": states.get(item.id),
            "title": _series_title(item, states.get(item.id)),
        }
        for item in items
        if matches_state(state, states.get(item.id))
    ]
    return templates.TemplateResponse(
        request,
        "admin_metadata.html",
        {
            "service_title": container.settings.service_title,
            "session": session,
            "library": library,
            "category": category,
            "rows": rows,
            "query": q or "",
            "state_filter": state or "all",
            "refresh_reminder_days": REFRESH_REMINDER_DAYS,
            "metadata_status": request.app.state.metadata_status,
            "request_limit": container.configuration.mangabaka_request_limit(),
            "message": message,
            "error": error,
        },
    )


@router.get("/series/{series_id}/metadata", response_class=HTMLResponse)
async def series_metadata_page(request: Request, series_id: str):
    session = await _require_admin(request)
    message, error = await _take_flash(request)
    container = _container(request)
    series = await run_in_threadpool(
        container.repository.catalog_series_by_id, series_id
    )
    if not series or series.category.casefold() != "manga":
        raise HTTPException(status_code=404, detail="Manga series not found")
    metadata = await run_in_threadpool(container.repository.series_metadata, series_id)
    lookup = await run_in_threadpool(container.repository.metadata_lookup, series_id)
    return templates.TemplateResponse(
        request,
        "admin_metadata_detail.html",
        {
            "service_title": container.settings.service_title,
            "session": session,
            "series": series,
            "metadata": metadata,
            "details": metadata.effective if metadata else {},
            "lookup": lookup,
            "has_custom_cover": bool(
                container.metadata and container.metadata.covers.has_custom(series_id)
            ),
            "message": message,
            "error": error,
        },
    )


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    context = await _admin_context(request, "users")
    container = _container(request)
    users = await run_in_threadpool(container.repository.users)
    context["users"] = users
    context["library_usage"] = await run_in_threadpool(
        container.repository.library_usage
    )
    granted = await run_in_threadpool(container.repository.all_access_grants)
    context["grants"] = {
        user.id: {_grant_key(item) for item in granted.get(user.id, ())}
        for user in users
        if not user.is_admin
    }
    return templates.TemplateResponse(request, "admin_users.html", context)


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request):
    context = await _admin_context(request, "settings")
    container = _container(request)
    context["settings"] = await run_in_threadpool(container.configuration.saved)
    context["pending_restart"] = await run_in_threadpool(
        container.configuration.pending_restart
    )
    context["restart_enabled"] = container.restarter.enabled
    context["memory_limit"] = await run_in_threadpool(
        memory_limit_text, container.settings.deployment_memory_limit
    )
    return templates.TemplateResponse(request, "admin_settings.html", context)


async def _admin_context(request: Request, section: str) -> dict[str, object]:
    session = await _require_admin(request)
    message, error = await _take_flash(request)
    return {
        "service_title": _container(request).settings.service_title,
        "session": session,
        "admin_section": section,
        "message": message,
        "error": error,
    }


@router.post("/admin/users")
async def admin_create_user(
    request: Request,
    username: str = Form(..., max_length=64),
    password: str = Form(..., max_length=1024),
    csrf_token: str = Form(...),
    is_admin: bool = Form(False),
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    try:
        await run_in_threadpool(
            _container(request).auth.create_user,
            username,
            password,
            is_admin=is_admin,
        )
    except (InvalidUserInput, sqlite3.IntegrityError) as error:
        message = (
            "Username already exists"
            if isinstance(error, sqlite3.IntegrityError)
            else str(error)
        )
        return await _admin_redirect(request, "users", error=message)
    return await _admin_redirect(
        request, "users", message=f"Created user {username.strip()}."
    )


@router.post("/admin/users/{user_id}/enabled")
async def admin_enable_user(
    request: Request,
    user_id: str,
    enabled: bool = Form(...),
    csrf_token: str = Form(...),
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    if user_id == session.user.id and not enabled:
        return await _admin_redirect(
            request, "users", error="You cannot disable your own account."
        )
    try:
        user = await run_in_threadpool(
            _container(request).auth.set_enabled, user_id, enabled
        )
    except LastAdministratorError as error:
        return await _admin_redirect(request, "users", error=str(error))
    if not user:
        return await _admin_redirect(request, "users", error="User not found.")
    state = "enabled" if enabled else "disabled"
    return await _admin_redirect(
        request, "users", message=f"{user.username} is now {state}."
    )


@router.post("/admin/users/{user_id}/password")
async def admin_reset_password(
    request: Request,
    user_id: str,
    password: str = Form(..., max_length=1024),
    csrf_token: str = Form(...),
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    try:
        user = await run_in_threadpool(
            _container(request).auth.reset_password, user_id, password
        )
    except InvalidUserInput as error:
        return await _admin_redirect(request, "users", error=str(error))
    if not user:
        return await _admin_redirect(request, "users", error="User not found.")
    return await _admin_redirect(
        request, "users", message=f"Reset the password for {user.username}."
    )


@router.post("/admin/users/{user_id}/access")
async def admin_update_access(
    request: Request,
    user_id: str,
):
    session = await _require_admin(request)
    form = await request.form()
    _verify_csrf(request, session, str(form.get("csrf_token", "")))
    service = _container(request).access
    try:
        decoded = [
            _decode_grant(user_id, str(value)) for value in form.getlist("grant")
        ]
        await run_in_threadpool(service.replace, user_id, decoded)
    except ValueError as error:
        return await _admin_redirect(request, "users", error=str(error))
    return await _admin_redirect(request, "users", message="Reader access updated.")


@router.post("/admin/libraries")
async def admin_add_library(
    request: Request,
    relative_path: str = Form(..., max_length=255),
    csrf_token: str = Form(...),
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    if scan_active(request):
        return await _admin_redirect(
            request, "libraries", error="Wait for the catalog scan to finish."
        )
    service = _container(request).libraries
    try:
        library = await run_in_threadpool(service.add, relative_path)
    except InvalidLibrary as error:
        return await _admin_redirect(request, "libraries", error=str(error))
    request.app.state.start_scan(library.id)
    return await _admin_redirect(
        request,
        "libraries",
        message=f"Added {library.name} and started its first scan.",
    )


@router.post("/admin/libraries/{library_id}/remove")
async def admin_remove_library(
    request: Request, library_id: str, csrf_token: str = Form(...)
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    if scan_active(request):
        return await _admin_redirect(
            request, "libraries", error="Wait for the catalog scan to finish."
        )
    service = _container(request).libraries
    try:
        library = await run_in_threadpool(service.remove, library_id)
    except InvalidLibrary as error:
        return await _admin_redirect(request, "libraries", error=str(error))
    return await _admin_redirect(
        request,
        "libraries",
        message=f"Removed {library.name}; no media files were deleted.",
    )


@router.post("/admin/libraries/{library_id}/scan")
async def admin_scan_library(
    request: Request, library_id: str, csrf_token: str = Form(...)
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    library = await run_in_threadpool(
        _container(request).repository.managed_library, library_id
    )
    if not library or not library.enabled:
        return await _admin_redirect(
            request, "libraries", error="Managed library not found."
        )
    if not request.app.state.start_scan(library_id):
        return await _admin_redirect(
            request, "libraries", error="A catalog scan is already running."
        )
    return await _admin_redirect(
        request, "libraries", message=f"Started scanning {library.name}."
    )


@router.post("/libraries/{library_id}/{category}/metadata/lookup")
async def library_bulk_metadata_lookup(
    request: Request, library_id: str, category: str
):
    session = await _require_admin(request)
    form = await request.form()
    _verify_csrf(request, session, str(form.get("csrf_token", "")))
    series_ids = list(dict.fromkeys(str(value) for value in form.getlist("series_id")))
    target = (library_id, category)
    if not series_ids:
        return await _library_metadata_redirect(
            request, *target, error="Select at least one manga series."
        )
    if len(series_ids) > 500:
        return await _library_metadata_redirect(
            request, *target, error="Select no more than 500 series."
        )
    available = {
        item.id
        for item in await run_in_threadpool(
            _container(request).repository.catalog_series,
            library_id=library_id,
            category=category,
        )
    }
    if category.casefold() != "manga" or any(
        series_id not in available for series_id in series_ids
    ):
        return await _library_metadata_redirect(
            request, *target, error="One or more selected manga series were not found."
        )
    if not request.app.state.start_metadata_lookup(series_ids):
        return await _library_metadata_redirect(
            request, *target, error="A metadata lookup is already running."
        )
    return await _library_metadata_redirect(
        request, *target, message=f"Started looking up {len(series_ids)} series."
    )


@router.post("/series/{series_id}/metadata/lookup")
async def series_metadata_lookup(
    request: Request,
    series_id: str,
    query: str = Form(default="", max_length=200),
    csrf_token: str = Form(...),
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    container = _container(request)
    series = await run_in_threadpool(
        container.repository.catalog_series_by_id, series_id
    )
    if not series or not container.metadata:
        return await _metadata_redirect(
            request, series_id, error="Manga series not found."
        )
    try:
        await run_in_threadpool(container.metadata.lookup, series, query or None)
    except MetadataError as error:
        return await _metadata_redirect(request, series_id, error=str(error))
    return await _metadata_redirect(
        request, series_id, message="MangaBaka suggestions updated."
    )


@router.post("/series/{series_id}/metadata/match")
async def series_metadata_match(
    request: Request,
    series_id: str,
    provider_id: int = Form(...),
    csrf_token: str = Form(...),
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    container = _container(request)
    series = await run_in_threadpool(
        container.repository.catalog_series_by_id, series_id
    )
    if not series or not container.metadata:
        return await _metadata_redirect(
            request, series_id, error="Manga series not found."
        )
    try:
        await run_in_threadpool(container.metadata.match, series, provider_id)
    except MetadataError as error:
        return await _metadata_redirect(request, series_id, error=str(error))
    return await _metadata_redirect(
        request, series_id, message="MangaBaka metadata linked."
    )


@router.post("/series/{series_id}/metadata/refresh")
async def series_metadata_refresh(
    request: Request, series_id: str, csrf_token: str = Form(...)
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    container = _container(request)
    series = await run_in_threadpool(
        container.repository.catalog_series_by_id, series_id
    )
    if not series or not container.metadata:
        return await _metadata_redirect(
            request, series_id, error="Manga series not found."
        )
    try:
        await run_in_threadpool(container.metadata.refresh, series)
    except MetadataError as error:
        return await _metadata_redirect(request, series_id, error=str(error))
    return await _metadata_redirect(
        request, series_id, message="Metadata refreshed; local edits were preserved."
    )


@router.post("/series/{series_id}/metadata/edit")
async def series_metadata_edit(request: Request, series_id: str):
    session = await _require_admin(request)
    form = await request.form()
    _verify_csrf(request, session, str(form.get("csrf_token", "")))
    service = _container(request).metadata
    if not service:
        return await _metadata_redirect(
            request, series_id, error="Metadata service unavailable."
        )
    values = {name: str(form.get(name, "")) for name in EDITABLE_FIELDS}
    try:
        await run_in_threadpool(service.update, series_id, values)
    except MetadataError as error:
        return await _metadata_redirect(request, series_id, error=str(error))
    return await _metadata_redirect(
        request, series_id, message="Series metadata saved."
    )


@router.post("/series/{series_id}/metadata/cover")
async def series_metadata_cover(
    request: Request,
    series_id: str,
    cover: Annotated[UploadFile, File()],
    csrf_token: str = Form(...),
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    container = _container(request)
    series = await run_in_threadpool(
        container.repository.catalog_series_by_id, series_id
    )
    service = container.metadata
    if not series or series.category.casefold() != "manga" or not service:
        return RedirectResponse("/", status_code=303)
    payload = await cover.read(MAX_COVER_BYTES + 1)
    try:
        await run_in_threadpool(service.covers.save_custom, series_id, payload)
    except MetadataError as error:
        return await _metadata_redirect(request, series_id, error=str(error))
    return await _metadata_redirect(
        request, series_id, message="Custom series cover uploaded."
    )


@router.post("/series/{series_id}/metadata/cover/remove")
async def remove_series_metadata_cover(
    request: Request, series_id: str, csrf_token: str = Form(...)
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    container = _container(request)
    series = await run_in_threadpool(
        container.repository.catalog_series_by_id, series_id
    )
    service = container.metadata
    if not series or series.category.casefold() != "manga" or not service:
        return RedirectResponse("/", status_code=303)
    await run_in_threadpool(service.covers.remove_custom, series_id)
    return await _metadata_redirect(request, series_id, message="Custom cover removed.")


@router.post("/series/{series_id}/metadata/unlink")
async def series_metadata_unlink(
    request: Request, series_id: str, csrf_token: str = Form(...)
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    container = _container(request)
    series = await run_in_threadpool(
        container.repository.catalog_series_by_id, series_id
    )
    service = container.metadata
    if not series or series.category.casefold() != "manga" or not service:
        return RedirectResponse("/", status_code=303)
    await run_in_threadpool(service.unlink, series_id)
    return await _metadata_redirect(
        request,
        series_id,
        message="MangaBaka metadata unlinked; custom cover preserved.",
    )


@router.post("/admin/settings")
async def admin_update_settings(request: Request):
    session = await _require_admin(request)
    form = await request.form()
    _verify_csrf(request, session, str(form.get("csrf_token", "")))
    service = _container(request).configuration
    values = {
        key: str(form.get(key, ""))
        for key in _container(request).settings.editable_values()
    }
    try:
        await run_in_threadpool(service.update, values)
    except ValueError as error:
        return await _admin_redirect(request, "settings", error=str(error))
    if form.get("action") == "restart":
        restarter = _container(request).restarter
        if not restarter.enabled:
            return await _admin_redirect(
                request,
                "settings",
                error="Settings saved, but automatic restart is not enabled.",
            )
        restarter.request_restart()
        return await _admin_redirect(
            request, "settings", message="Settings saved. Nineveh is restarting."
        )
    if not service.pending_restart():
        return await _admin_redirect(
            request, "settings", message="Settings saved and applied."
        )
    return await _admin_redirect(
        request, "settings", message="Settings saved. Restart Nineveh to apply them."
    )


@router.post("/admin/scan")
async def admin_scan(request: Request, csrf_token: str = Form(...)):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    if not request.app.state.start_scan():
        return await _admin_redirect(
            request, error="A catalog scan is already running."
        )
    return await _admin_redirect(request, message="Catalog scan started.")


async def _flash_redirect(
    request: Request,
    path: str,
    *,
    message: str | None = None,
    error: str | None = None,
):
    """Park the notice on the session, then redirect to a clean URL.

    Notices used to travel as query parameters, which put them in the access
    log and let anyone forge a banner by handing an administrator a link.
    """
    await run_in_threadpool(
        _container(request).auth.set_flash,
        request.cookies.get(SESSION_COOKIE),
        message,
        error,
    )
    return RedirectResponse(path, status_code=303)


async def _take_flash(request: Request) -> tuple[str | None, str | None]:
    return await run_in_threadpool(
        _container(request).auth.take_flash, request.cookies.get(SESSION_COOKIE)
    )


async def _admin_redirect(
    request: Request,
    section: str = "overview",
    *,
    message: str | None = None,
    error: str | None = None,
):
    path = "/admin" if section == "overview" else f"/admin/{section}"
    return await _flash_redirect(request, path, message=message, error=error)


async def _metadata_redirect(
    request: Request,
    series_id: str,
    *,
    message: str | None = None,
    error: str | None = None,
):
    return await _flash_redirect(
        request, f"/series/{series_id}/metadata", message=message, error=error
    )


async def _library_metadata_redirect(
    request: Request,
    library_id: str,
    category: str,
    *,
    message: str | None = None,
    error: str | None = None,
):
    return await _flash_redirect(
        request,
        f"/libraries/{library_id}/{category}/metadata",
        message=message,
        error=error,
    )


def _decode_grant(user_id: str, value: str) -> AccessGrant:
    parts = value.split("|")
    if len(parts) == 2 and parts[0] == "library":
        return AccessGrant(user_id, parts[1])
    if len(parts) == 3 and parts[0] == "category":
        return AccessGrant(user_id, parts[1], parts[2])
    if len(parts) == 4 and parts[0] == "series":
        return AccessGrant(user_id, parts[1], parts[2], parts[3])
    raise ValueError("Invalid access selection")


def _grant_key(grant: AccessGrant) -> str:
    if grant.series_id:
        return f"series|{grant.library_id}|{grant.category}|{grant.series_id}"
    if grant.category:
        return f"category|{grant.library_id}|{grant.category}"
    return f"library|{grant.library_id}"


async def _managed_library_named(container, name: str, scope):
    visible = dict(await run_in_threadpool(container.repository.libraries, scope))
    if name not in visible:
        return None
    libraries = await run_in_threadpool(container.repository.managed_libraries)
    return next((item for item in libraries if item.name == name), None)


def _series_title(series, metadata) -> str:
    """Summaries, state rows and full records all expose `.title`."""
    title = metadata.title if metadata is not None else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return series.name


def _series_cards(series_items, metadata_by_series) -> list[dict[str, object]]:
    return [
        {
            "series": item,
            "metadata": metadata_by_series.get(item.id),
            "title": _series_title(item, metadata_by_series.get(item.id)),
        }
        for item in series_items
    ]


def _search_url(query: str, page: int) -> str:
    return f"/?{urlencode({'q': query, 'page': page})}"
