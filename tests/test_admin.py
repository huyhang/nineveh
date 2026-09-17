from __future__ import annotations

from conftest import ADMIN_PASSWORD, READER_PASSWORD, authorization
from fastapi.testclient import TestClient

NEW_PASSWORD = "another sufficiently long password"


def test_me_reports_the_authenticated_account(client: TestClient):
    body = client.get("/api/v1/auth/me", headers=authorization()).json()
    assert body["username"] == "admin"
    assert body["isAdmin"] is True
    assert body["enabled"] is True


def test_listing_users_requires_an_administrator(client: TestClient, reader: dict):
    assert client.get("/api/v1/admin/users", headers=reader).status_code == 403
    listed = client.get("/api/v1/admin/users", headers=authorization()).json()
    assert {user["username"] for user in listed} == {"admin", "reader"}


def test_creating_a_duplicate_user_conflicts(client: TestClient, reader: dict):
    response = client.post(
        "/api/v1/admin/users",
        headers=authorization(),
        json={"username": "reader", "password": READER_PASSWORD},
    )
    assert response.status_code == 409


def test_creating_a_user_with_a_bad_name_is_rejected(client: TestClient):
    response = client.post(
        "/api/v1/admin/users",
        headers=authorization(),
        json={"username": "a b", "password": READER_PASSWORD},
    )
    assert response.status_code == 422


def test_creating_a_user_with_a_short_password_is_rejected(client: TestClient):
    response = client.post(
        "/api/v1/admin/users",
        headers=authorization(),
        json={"username": "someone", "password": "short"},
    )
    assert response.status_code == 422


def test_resetting_a_password_takes_effect_immediately(
    client: TestClient, reader: dict
):
    users = client.get("/api/v1/admin/users", headers=authorization()).json()
    reader_id = next(user["id"] for user in users if user["username"] == "reader")

    response = client.patch(
        f"/api/v1/admin/users/{reader_id}",
        headers=authorization(),
        json={"password": NEW_PASSWORD},
    )
    assert response.status_code == 200
    assert client.get("/api/v1/auth/me", headers=reader).status_code == 401
    updated = authorization("reader", NEW_PASSWORD)
    assert client.get("/api/v1/auth/me", headers=updated).status_code == 200


def test_disabling_a_user_revokes_access(client: TestClient, reader: dict):
    users = client.get("/api/v1/admin/users", headers=authorization()).json()
    reader_id = next(user["id"] for user in users if user["username"] == "reader")

    assert client.get("/api/v1/auth/me", headers=reader).status_code == 200
    client.patch(
        f"/api/v1/admin/users/{reader_id}",
        headers=authorization(),
        json={"enabled": False},
    )
    assert client.get("/api/v1/auth/me", headers=reader).status_code == 401


def test_an_administrator_cannot_disable_themselves(client: TestClient):
    me = client.get("/api/v1/auth/me", headers=authorization()).json()
    response = client.patch(
        f"/api/v1/admin/users/{me['id']}",
        headers=authorization(),
        json={"enabled": False},
    )
    assert response.status_code == 422
    assert "own account" in response.json()["detail"]


def test_the_last_administrator_cannot_be_disabled(client: TestClient):
    """A second admin disabling the first is fine; disabling the last one is not."""
    client.post(
        "/api/v1/admin/users",
        headers=authorization(),
        json={"username": "admin2", "password": NEW_PASSWORD, "is_admin": True},
    )
    second = authorization("admin2", NEW_PASSWORD)
    users = client.get("/api/v1/admin/users", headers=second).json()
    first_id = next(user["id"] for user in users if user["username"] == "admin")
    second_id = next(user["id"] for user in users if user["username"] == "admin2")

    assert (
        client.patch(
            f"/api/v1/admin/users/{first_id}", headers=second, json={"enabled": False}
        ).status_code
        == 200
    )
    last = client.patch(
        f"/api/v1/admin/users/{second_id}",
        headers=second,
        json={"enabled": False},
    )
    assert last.status_code == 422


def test_patching_an_unknown_user_is_404(client: TestClient):
    response = client.patch(
        "/api/v1/admin/users/missing", headers=authorization(), json={"enabled": True}
    )
    assert response.status_code == 404


def test_a_reader_cannot_start_a_scan(client: TestClient, reader: dict):
    assert client.post("/api/v1/admin/catalog/scan", headers=reader).status_code == 403


def test_an_administrator_can_start_a_scan(client: TestClient):
    response = client.post("/api/v1/admin/catalog/scan", headers=authorization())
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}


def test_basic_auth_clients_do_not_need_a_csrf_token(client: TestClient):
    """CSRF only applies to cookie-borne identities; API clients are unaffected."""
    response = client.post(
        "/api/v1/admin/users",
        headers=authorization(),
        json={"username": "apiuser", "password": READER_PASSWORD},
    )
    assert response.status_code == 201


def test_cookie_sessions_must_present_a_csrf_token(client: TestClient):
    login = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert login.status_code == 303

    without = client.post("/api/v1/admin/catalog/scan")
    assert without.status_code == 403
    assert without.json()["detail"] == "Invalid CSRF token"

    admin_page = client.get("/admin")
    token = admin_page.text.split('name="csrf_token" value="')[1].split('"')[0]
    with_token = client.post(
        "/api/v1/admin/catalog/scan", headers={"X-CSRF-Token": token}
    )
    assert with_token.status_code in {202, 409}
