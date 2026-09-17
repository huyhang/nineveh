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
from .domain import Session
from .http_api import SESSION_COOKIE

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(include_in_schema=False)


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
    if not session.user.is_admin:
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
    view = await _catalog_view(container, filters, page)
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


async def _catalog_view(container, filters: dict[str, str], page: int) -> dict:
    """Everything the catalog template needs beyond the request and session."""
    page_size = container.settings.feed_page_size
    publications, total = await run_in_threadpool(
        container.repository.publications,
        library=filters["library"] or None,
        category=filters["category"] or None,
        series=filters["series"] or None,
        query=filters["q"] or None,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    libraries = await run_in_threadpool(container.repository.libraries)
    series_options = (
        await run_in_threadpool(
            container.repository.series, filters["library"], filters["category"]
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
async def admin_page(
    request: Request,
    message: str | None = Query(default=None, max_length=200),
    error: str | None = Query(default=None, max_length=200),
):
    session = await _require_admin(request)
    container = _container(request)
    users = await run_in_threadpool(container.repository.users)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "service_title": container.settings.service_title,
            "session": session,
            "users": users,
            "scan_status": container.scanner.status,
            "message": message,
            "error": error,
        },
    )


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
        return _admin_redirect(error=message)
    return _admin_redirect(message=f"Created user {username.strip()}.")


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
        return _admin_redirect(error="You cannot disable your own account.")
    try:
        user = await run_in_threadpool(
            _container(request).auth.set_enabled, user_id, enabled
        )
    except LastAdministratorError as error:
        return _admin_redirect(error=str(error))
    if not user:
        return _admin_redirect(error="User not found.")
    state = "enabled" if enabled else "disabled"
    return _admin_redirect(message=f"{user.username} is now {state}.")


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
        return _admin_redirect(error=str(error))
    if not user:
        return _admin_redirect(error="User not found.")
    return _admin_redirect(message=f"Reset the password for {user.username}.")


@router.post("/admin/scan")
async def admin_scan(request: Request, csrf_token: str = Form(...)):
    session = await _require_admin(request)
    _verify_csrf(request, session, csrf_token)
    if not request.app.state.start_scan():
        return _admin_redirect(error="A catalog scan is already running.")
    return _admin_redirect(message="Catalog scan started.")


def _admin_redirect(*, message: str | None = None, error: str | None = None):
    parameters = {
        key: value
        for key, value in {"message": message, "error": error}.items()
        if value
    }
    suffix = f"?{urlencode(parameters)}" if parameters else ""
    return RedirectResponse(f"/admin{suffix}", status_code=303)


def _catalog_url(filters: dict[str, str], page: int) -> str:
    parameters = {key: value for key, value in filters.items() if value}
    parameters["page"] = str(page)
    return f"/?{urlencode(parameters)}"
