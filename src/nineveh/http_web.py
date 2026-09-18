from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import AuthenticationError, InvalidUserInput, LastAdministratorError
from .catalog import InvalidLibrary
from .deployment import memory_limit_text
from .domain import AccessGrant, Session
from .http_api import SESSION_COOKIE, scan_active
from .units import gibibytes

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(include_in_schema=False)


templates.env.filters["gib"] = gibibytes


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
    filters = {
        "library": library or "",
        "category": category or "",
        "series": series or "",
        "q": q or "",
    }
    view = await _catalog_view(container, session, filters, page)
    return templates.TemplateResponse(
        request,
        "catalog.html",
        {
            "service_title": container.settings.service_title,
            "session": session,
            "filters": filters,
            "page": page,
            **view,
        },
    )


async def _catalog_view(
    container, session: Session, filters: dict[str, str], page: int
) -> dict:
    """Everything the catalog template needs beyond the request and session."""
    page_size = container.settings.feed_page_size
    scope = container.authorization.read_scope(session.user)
    publications, total = await run_in_threadpool(
        container.repository.publications,
        library=filters["library"] or None,
        category=filters["category"] or None,
        series=filters["series"] or None,
        query=filters["q"] or None,
        limit=page_size,
        offset=(page - 1) * page_size,
        scope=scope,
    )
    libraries = await run_in_threadpool(container.repository.libraries, scope)
    series_options = (
        await run_in_threadpool(
            container.repository.series,
            filters["library"],
            filters["category"],
            scope,
        )
        if filters["library"] and filters["category"]
        else []
    )
    page_count = max(1, math.ceil(total / page_size))
    return {
        "publications": publications,
        "total": total,
        "libraries": libraries,
        "series_options": series_options,
        "page_count": page_count,
        "previous_url": _catalog_url(filters, page - 1) if page > 1 else None,
        "next_url": _catalog_url(filters, page + 1) if page < page_count else None,
    }


@router.get("/admin", response_class=HTMLResponse)
async def admin_overview(
    request: Request,
    message: str | None = Query(default=None, max_length=200),
    error: str | None = Query(default=None, max_length=200),
):
    context = await _admin_context(request, "overview", message, error)
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
async def admin_libraries_page(
    request: Request,
    message: str | None = Query(default=None, max_length=200),
    error: str | None = Query(default=None, max_length=200),
):
    context = await _admin_context(request, "libraries", message, error)
    container = _container(request)
    context["library_usage"] = await run_in_threadpool(
        container.repository.library_usage
    )
    context["scan_status"] = container.scanner.status
    context["available_libraries"] = await run_in_threadpool(
        container.libraries.available
    )
    return templates.TemplateResponse(request, "admin_libraries.html", context)


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    message: str | None = Query(default=None, max_length=200),
    error: str | None = Query(default=None, max_length=200),
):
    context = await _admin_context(request, "users", message, error)
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
async def admin_settings_page(
    request: Request,
    message: str | None = Query(default=None, max_length=200),
    error: str | None = Query(default=None, max_length=200),
):
    context = await _admin_context(request, "settings", message, error)
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


async def _admin_context(
    request: Request,
    section: str,
    message: str | None,
    error: str | None,
) -> dict[str, object]:
    session = await _require_admin(request)
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
        return _admin_redirect("users", error=message)
    return _admin_redirect("users", message=f"Created user {username.strip()}.")


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
        return _admin_redirect("users", error="You cannot disable your own account.")
    try:
        user = await run_in_threadpool(
            _container(request).auth.set_enabled, user_id, enabled
        )
    except LastAdministratorError as error:
        return _admin_redirect("users", error=str(error))
    if not user:
        return _admin_redirect("users", error="User not found.")
    state = "enabled" if enabled else "disabled"
    return _admin_redirect("users", message=f"{user.username} is now {state}.")


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
        return _admin_redirect("users", error=str(error))
    if not user:
        return _admin_redirect("users", error="User not found.")
    return _admin_redirect("users", message=f"Reset the password for {user.username}.")


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
        return _admin_redirect("users", error=str(error))
    return _admin_redirect("users", message="Reader access updated.")


@router.post("/admin/libraries")
async def admin_add_library(
    request: Request,
    relative_path: str = Form(..., max_length=255),
    csrf_token: str = Form(...),
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    if scan_active(request):
        return _admin_redirect(
            "libraries", error="Wait for the catalog scan to finish."
        )
    service = _container(request).libraries
    try:
        library = await run_in_threadpool(service.add, relative_path)
    except InvalidLibrary as error:
        return _admin_redirect("libraries", error=str(error))
    request.app.state.start_scan(library.id)
    return _admin_redirect(
        "libraries", message=f"Added {library.name} and started its first scan."
    )


@router.post("/admin/libraries/{library_id}/remove")
async def admin_remove_library(
    request: Request, library_id: str, csrf_token: str = Form(...)
):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    if scan_active(request):
        return _admin_redirect(
            "libraries", error="Wait for the catalog scan to finish."
        )
    service = _container(request).libraries
    try:
        library = await run_in_threadpool(service.remove, library_id)
    except InvalidLibrary as error:
        return _admin_redirect("libraries", error=str(error))
    return _admin_redirect(
        "libraries", message=f"Removed {library.name}; no media files were deleted."
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
        return _admin_redirect("libraries", error="Managed library not found.")
    if not request.app.state.start_scan(library_id):
        return _admin_redirect("libraries", error="A catalog scan is already running.")
    return _admin_redirect("libraries", message=f"Started scanning {library.name}.")


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
        return _admin_redirect("settings", error=str(error))
    if form.get("action") == "restart":
        restarter = _container(request).restarter
        if not restarter.enabled:
            return _admin_redirect(
                "settings",
                error="Settings saved, but automatic restart is not enabled.",
            )
        restarter.request_restart()
        return _admin_redirect(
            "settings", message="Settings saved. Nineveh is restarting."
        )
    return _admin_redirect(
        "settings", message="Settings saved. Restart Nineveh to apply them."
    )


@router.post("/admin/scan")
async def admin_scan(request: Request, csrf_token: str = Form(...)):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    if not request.app.state.start_scan():
        return _admin_redirect(error="A catalog scan is already running.")
    return _admin_redirect(message="Catalog scan started.")


def _admin_redirect(
    section: str = "overview",
    *,
    message: str | None = None,
    error: str | None = None,
):
    parameters = {
        key: value
        for key, value in {"message": message, "error": error}.items()
        if value
    }
    suffix = f"?{urlencode(parameters)}" if parameters else ""
    path = "/admin" if section == "overview" else f"/admin/{section}"
    return RedirectResponse(f"{path}{suffix}", status_code=303)


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


def _catalog_url(filters: dict[str, str], page: int) -> str:
    parameters = {key: value for key, value in filters.items() if value}
    parameters["page"] = str(page)
    return f"/?{urlencode(parameters)}"
