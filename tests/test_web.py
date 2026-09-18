from __future__ import annotations

import time
from dataclasses import replace

from conftest import ADMIN_PASSWORD, READER_PASSWORD, authorization
from fastapi.testclient import TestClient

NEW_PASSWORD = "another sufficiently long password"


def _login(client: TestClient, username: str = "admin", password: str = ADMIN_PASSWORD):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _csrf(client: TestClient, path: str = "/admin") -> str:
    page = client.get(path)
    return page.text.split('name="csrf_token" value="')[1].split('"')[0]


def _user_id(client: TestClient, username: str) -> str:
    users = client.get("/api/v1/admin/users").json()
    return next(user["id"] for user in users if user["username"] == username)


def test_signed_out_visitors_are_sent_to_the_login_page(client: TestClient):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_login_page_renders(client: TestClient):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text
    assert "data-theme-toggle" in response.text
    assert "/static/theme.js" in response.text
    assert response.headers["cache-control"] == "private, no-store"

    script = client.get("/static/theme.js")
    assert script.status_code == 200
    assert "nineveh-theme" in script.text


def test_a_bad_password_re_renders_the_form(client: TestClient):
    response = client.post("/login", data={"username": "admin", "password": "nope"})
    assert response.status_code == 401
    assert "Invalid username or password." in response.text


def test_a_cross_origin_login_is_refused(client: TestClient):
    response = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_signing_in_sets_a_hardened_cookie_and_shows_the_catalog(client: TestClient):
    assert _login(client).status_code == 303
    assert "nineveh_session" in client.cookies

    catalog = client.get("/")
    assert catalog.status_code == 200
    assert "The First Issue" in catalog.text
    assert "Example Series" in catalog.text
    assert 'href="/" aria-current="page">Library</a>' in catalog.text
    assert 'href="/admin"' in catalog.text


def test_the_login_page_redirects_an_authenticated_visitor(client: TestClient):
    _login(client)
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_the_catalog_filters_and_paginates(client: TestClient):
    _login(client)
    filtered = client.get("/?library=Main%20Library&category=comics")
    assert filtered.status_code == 200
    assert "Example Series" in filtered.text

    empty = client.get("/?q=nothing-matches-this")
    assert "No publications found" in empty.text


def test_signing_out_clears_the_session(client: TestClient):
    _login(client)
    token = _csrf(client, "/")
    response = client.post(
        "/logout", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_signing_out_requires_a_valid_csrf_token(client: TestClient):
    _login(client)
    response = client.post("/logout", data={"csrf_token": "forged"})
    assert response.status_code == 403


def test_the_admin_page_is_administrator_only(client: TestClient, reader: dict):
    _login(client, "reader", READER_PASSWORD)
    for path in ("/admin", "/admin/users", "/admin/libraries", "/admin/settings"):
        assert client.get(path).status_code == 403


def test_an_administrator_can_manage_users_from_the_browser(client: TestClient):
    _login(client)
    token = _csrf(client)

    created = client.post(
        "/admin/users",
        data={
            "username": "webuser",
            "password": READER_PASSWORD,
            "csrf_token": token,
            "is_admin": False,
        },
        follow_redirects=True,
    )
    assert "Created user webuser" in created.text
    assert "webuser" in created.text


def test_creating_a_duplicate_user_reports_an_error(client: TestClient, reader: dict):
    _login(client)
    response = client.post(
        "/admin/users",
        data={
            "username": "reader",
            "password": READER_PASSWORD,
            "csrf_token": _csrf(client),
        },
        follow_redirects=True,
    )
    assert "Username already exists" in response.text


def test_admin_actions_reject_a_forged_csrf_token(client: TestClient):
    _login(client)
    for path, data in (
        ("/admin/scan", {"csrf_token": "forged"}),
        (
            "/admin/users",
            {"username": "x", "password": READER_PASSWORD, "csrf_token": "forged"},
        ),
    ):
        assert client.post(path, data=data).status_code == 403


def test_an_administrator_can_disable_and_re_enable_a_reader(
    client: TestClient, reader: dict
):
    _login(client)
    user_id = _user_id(client, "reader")
    token = _csrf(client)

    disabled = client.post(
        f"/admin/users/{user_id}/enabled",
        data={"enabled": "false", "csrf_token": token},
        follow_redirects=True,
    )
    assert "is now disabled" in disabled.text

    enabled = client.post(
        f"/admin/users/{user_id}/enabled",
        data={"enabled": "true", "csrf_token": token},
        follow_redirects=True,
    )
    assert "is now enabled" in enabled.text


def test_an_administrator_cannot_disable_their_own_account(client: TestClient):
    _login(client)
    me = client.get("/api/v1/auth/me").json()
    response = client.post(
        f"/admin/users/{me['id']}/enabled",
        data={"enabled": "false", "csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert "cannot disable your own account" in response.text


def test_password_resets_report_success_and_failure(client: TestClient, reader: dict):
    _login(client)
    user_id = _user_id(client, "reader")
    token = _csrf(client)

    ok = client.post(
        f"/admin/users/{user_id}/password",
        data={"password": NEW_PASSWORD, "csrf_token": token},
        follow_redirects=True,
    )
    assert "Reset the password" in ok.text

    weak = client.post(
        f"/admin/users/{user_id}/password",
        data={"password": "short", "csrf_token": token},
        follow_redirects=True,
    )
    assert "at least 12 characters" in weak.text


def test_resetting_an_unknown_user_reports_not_found(client: TestClient):
    _login(client)
    response = client.post(
        "/admin/users/missing/password",
        data={"password": NEW_PASSWORD, "csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert "User not found." in response.text


def test_an_administrator_can_start_a_scan_from_the_browser(client: TestClient):
    _login(client)
    response = client.post(
        "/admin/scan", data={"csrf_token": _csrf(client)}, follow_redirects=True
    )
    assert (
        "Catalog scan started." in response.text or "already running" in response.text
    )


def test_admin_console_shows_capacity_and_access_controls(client: TestClient):
    _login(client)
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Indexed media" in page.text
    assert ">Library</a>" in page.text
    assert 'href="/admin" aria-current="page">Admin</a>' in page.text
    assert 'href="/admin/users"' in page.text
    assert "Create account" not in page.text

    library_page = client.get("/admin/libraries")
    assert "Main Library" in library_page.text
    assert 'aria-current="page">Libraries</a>' in library_page.text

    client.post(
        "/admin/users",
        data={
            "username": "viewer",
            "password": READER_PASSWORD,
            "csrf_token": _csrf(client),
        },
    )
    users_page = client.get("/admin/users")
    assert "Read access" in users_page.text
    assert 'aria-current="page">Users</a>' in users_page.text
    settings_page = client.get("/admin/settings")
    assert "Docker memory ceiling" in settings_page.text
    assert 'aria-current="page">Settings</a>' in settings_page.text


def test_browser_can_update_reader_access(client: TestClient):
    _login(client)
    client.post(
        "/admin/users",
        data={
            "username": "viewer",
            "password": READER_PASSWORD,
            "csrf_token": _csrf(client),
        },
    )
    user_id = _user_id(client, "viewer")
    library = client.get("/api/v1/admin/libraries").json()["libraries"][0]
    response = client.post(
        f"/admin/users/{user_id}/access",
        data={
            "csrf_token": _csrf(client),
            "grant": f"library|{library['id']}",
        },
        follow_redirects=True,
    )
    assert "Reader access updated" in response.text
    assert (
        len(
            client.get(
                "/opds/v2/publications.json",
                headers=authorization("viewer", READER_PASSWORD),
            ).json()["publications"]
        )
        == 1
    )


def test_browser_library_and_settings_errors_are_friendly(client: TestClient):
    _login(client)
    token = _csrf(client)
    missing_library = client.post(
        "/admin/libraries",
        data={"relative_path": "missing", "csrf_token": token},
        follow_redirects=True,
    )
    assert "No directory named missing under the data root" in missing_library.text
    missing_scan = client.post(
        "/admin/libraries/missing/scan",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert "Managed library not found" in missing_scan.text

    values = client.app.state.container.settings.editable_values()
    values["feed_page_size"] = "0"
    invalid = client.post(
        "/admin/settings",
        data={**values, "csrf_token": token, "action": "save"},
        follow_redirects=True,
    )
    assert "must be between" in invalid.text

    values["feed_page_size"] = "18"
    saved = client.post(
        "/admin/settings",
        data={**values, "csrf_token": token, "action": "restart"},
        follow_redirects=True,
    )
    assert "automatic restart is not enabled" in saved.text


def test_browser_can_add_scan_and_remove_a_library(client: TestClient, library):
    settings, _ = library
    directory = settings.data_dir / "Web Library"
    (directory / "comics").mkdir(parents=True)
    _login(client)
    token = _csrf(client)
    added = client.post(
        "/admin/libraries",
        data={"relative_path": "Web Library", "csrf_token": token},
        follow_redirects=True,
    )
    assert "Added Web Library" in added.text
    library_id = next(
        item["id"]
        for item in client.get("/api/v1/admin/libraries").json()["libraries"]
        if item["name"] == "Web Library"
    )
    for _ in range(200):
        if client.app.state.scan_task.done():
            break
        time.sleep(0.01)

    scan = client.post(
        f"/admin/libraries/{library_id}/scan",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert "Started scanning Web Library" in scan.text
    for _ in range(200):
        if client.app.state.scan_task.done():
            break
        time.sleep(0.01)
    removed = client.post(
        f"/admin/libraries/{library_id}/remove",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert "no media files were deleted" in removed.text
    assert directory.exists()


def test_browser_can_save_settings_and_request_an_injected_restart(client: TestClient):
    class Restart:
        enabled = True

        def __init__(self):
            self.requested = False

        def request_restart(self):
            self.requested = True

    _login(client)
    controller = Restart()
    client.app.state.container = replace(
        client.app.state.container, restarter=controller
    )
    values = client.app.state.container.settings.editable_values()
    response = client.post(
        "/admin/settings",
        data={**values, "csrf_token": _csrf(client), "action": "restart"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert controller.requested


def test_browser_rejects_a_malformed_access_selection(client: TestClient):
    _login(client)
    client.post(
        "/admin/users",
        data={
            "username": "viewer",
            "password": READER_PASSWORD,
            "csrf_token": _csrf(client),
        },
    )
    response = client.post(
        f"/admin/users/{_user_id(client, 'viewer')}/access",
        data={"csrf_token": _csrf(client), "grant": "broken"},
        follow_redirects=True,
    )
    assert "Invalid access selection" in response.text


def test_every_management_form_rejects_a_forged_csrf_token(client: TestClient):
    """Cookie-authenticated writes are only safe while the token is checked."""
    _login(client)
    library = client.get("/api/v1/admin/libraries").json()["libraries"][0]
    client.post(
        "/admin/users",
        data={
            "username": "victim",
            "password": READER_PASSWORD,
            "csrf_token": _csrf(client),
        },
    )
    user_id = _user_id(client, "victim")
    forms = [
        ("/admin/libraries", {"relative_path": "Main Library"}),
        (f"/admin/libraries/{library['id']}/scan", {}),
        (f"/admin/libraries/{library['id']}/remove", {}),
        (f"/admin/users/{user_id}/access", {"grant": f"library|{library['id']}"}),
        ("/admin/settings", {"feed_page_size": "12"}),
        ("/admin/scan", {}),
    ]
    for path, payload in forms:
        response = client.post(
            path, data={**payload, "csrf_token": "forged"}, follow_redirects=False
        )
        assert response.status_code == 403, path


def test_the_add_library_panel_explains_itself_when_nothing_is_addable(
    client: TestClient,
):
    _login(client)
    page = client.get("/admin/libraries")
    assert 'name="relative_path"' not in page.text
    assert "Create another folder there to add one." in page.text


def test_dark_mode_survives_without_javascript(client: TestClient):
    """theme.js sets data-theme; the fallback must not depend on it."""
    stylesheet = client.get("/static/style.css").text
    assert ":root:not([data-theme])" in stylesheet


def test_the_settings_page_reports_the_detected_memory_ceiling(client: TestClient):
    _login(client)
    page = client.get("/admin/settings")
    assert "Docker memory ceiling" in page.text
    assert "Unlimited or not detected" in page.text or "iB" in page.text


def test_a_discarded_proxy_header_is_surfaced_on_the_admin_page(client: TestClient):
    """The misconfiguration used to be silent; it should name the fix."""
    _login(client)
    assert "Forwarded headers are being ignored" not in client.get("/admin").text

    client.get("/api/v1/health/live", headers={"X-Forwarded-Proto": "https"})

    page = client.get("/admin")
    assert "Forwarded headers are being ignored" in page.text
    assert "NINEVEH_FORWARDED_ALLOW_IPS=" in page.text


def test_an_honoured_proxy_header_raises_no_warning(client: TestClient):
    _login(client)
    client.get("/api/v1/health/live", headers={"X-Forwarded-Proto": "http"})
    assert "Forwarded headers are being ignored" not in client.get("/admin").text
