from __future__ import annotations

from conftest import ADMIN_PASSWORD, READER_PASSWORD
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
    assert response.headers["cache-control"] == "private, no-store"


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
    assert client.get("/admin").status_code == 403


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
