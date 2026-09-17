from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nineveh.auth import (
    AuthenticationError,
    AuthService,
    InvalidUserInput,
    LastAdministratorError,
)
from nineveh.authorization import ReadAllPolicy
from nineveh.database import SQLiteRepository

PASSWORD = "a sufficiently long password"


@pytest.fixture
def service(tmp_path) -> AuthService:
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    return AuthService(repository, session_hours=1)


def _drain(semaphore, expected: int) -> None:
    """A BoundedSemaphore admits exactly `expected` holders, then refuses."""
    assert all(semaphore.acquire(blocking=False) for _ in range(expected))
    assert semaphore.acquire(blocking=False) is False
    for _ in range(expected):
        semaphore.release()


def test_hash_workers_default_to_two(service: AuthService):
    _drain(service._hash_slots, 2)


def test_hash_workers_are_configurable(tmp_path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    _drain(AuthService(repository, hash_workers=5)._hash_slots, 5)


def test_authentication_still_works_with_a_single_hash_worker(tmp_path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    service = AuthService(repository, hash_workers=1)
    user = service.create_user("reader", PASSWORD)
    assert service.authenticate("reader", PASSWORD).id == user.id


def test_authentication_round_trip(service: AuthService):
    user = service.create_user("reader", PASSWORD)
    assert service.authenticate("reader", PASSWORD).id == user.id
    with pytest.raises(AuthenticationError):
        service.authenticate("reader", "wrong password")


def test_usernames_are_matched_case_insensitively(service: AuthService):
    service.create_user("Reader", PASSWORD)
    assert service.authenticate("reader", PASSWORD).username == "Reader"


def test_an_unknown_user_is_rejected(service: AuthService):
    with pytest.raises(AuthenticationError):
        service.authenticate("nobody", PASSWORD)


@pytest.mark.parametrize("username", ["ab", "a b", "-lead", "x" * 65, ""])
def test_invalid_usernames_are_rejected(service: AuthService, username: str):
    with pytest.raises(InvalidUserInput):
        service.create_user(username, PASSWORD)


@pytest.mark.parametrize("password", ["short", "x" * 1025])
def test_invalid_passwords_are_rejected(service: AuthService, password: str):
    with pytest.raises(InvalidUserInput):
        service.create_user("reader", password)


def test_sessions_round_trip_and_can_be_ended(service: AuthService):
    user = service.create_user("reader", PASSWORD)
    session = service.new_session(user)

    restored = service.session(session.token)
    assert restored is not None
    assert restored.user.id == user.id
    assert service.valid_csrf(restored, session.csrf_token)
    assert not service.valid_csrf(restored, "forged")
    assert not service.valid_csrf(restored, None)

    service.end_session(session.token)
    assert service.session(session.token) is None


def test_an_absent_or_unknown_token_has_no_session(service: AuthService):
    assert service.session(None) is None
    assert service.session("not-a-token") is None


def test_an_expired_session_is_not_restored(tmp_path):
    repository = SQLiteRepository(tmp_path / "nineveh.sqlite3")
    repository.initialize()
    service = AuthService(repository, session_hours=1)
    user = service.create_user("reader", PASSWORD)
    expired = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    repository.create_session(user.id, AuthService._token_hash("t"), "csrf", expired)
    assert service.session("t") is None


def test_changing_a_password_invalidates_existing_sessions(service: AuthService):
    user = service.create_user("reader", PASSWORD)
    session = service.new_session(user)
    service.reset_password(user.id, "an entirely different password")
    assert service.session(session.token) is None


def test_disabling_a_user_invalidates_the_basic_auth_cache(service: AuthService):
    user = service.create_user("reader", PASSWORD)
    service.authenticate("reader", PASSWORD)
    service.set_enabled(user.id, False)
    with pytest.raises(AuthenticationError):
        service.authenticate("reader", PASSWORD)


def test_re_enabling_restores_access(service: AuthService):
    user = service.create_user("reader", PASSWORD)
    service.set_enabled(user.id, False)
    service.set_enabled(user.id, True)
    assert service.authenticate("reader", PASSWORD).enabled


def test_setting_enabled_on_an_unknown_user_returns_none(service: AuthService):
    assert service.set_enabled("missing", False) is None


def test_the_final_administrator_cannot_be_disabled(service: AuthService):
    admin = service.create_user("admin", PASSWORD, is_admin=True)
    with pytest.raises(LastAdministratorError):
        service.set_enabled(admin.id, False)


def test_a_second_administrator_may_be_disabled(service: AuthService):
    service.create_user("admin", PASSWORD, is_admin=True)
    spare = service.create_user("admin2", PASSWORD, is_admin=True)
    assert service.set_enabled(spare.id, False) is not None


def test_bootstrap_creates_the_first_administrator_only_once(service: AuthService):
    created = service.bootstrap_admin("admin", PASSWORD)
    assert created is not None and created.is_admin
    assert service.bootstrap_admin("admin", PASSWORD) is None


def test_bootstrap_without_a_password_fails_loudly(service: AuthService):
    with pytest.raises(RuntimeError, match="NINEVEH_ADMIN_PASSWORD"):
        service.bootstrap_admin("admin", None)


def test_the_read_all_policy_grants_enabled_users_everything(service: AuthService):
    reader = service.create_user("reader", PASSWORD)
    admin = service.create_user("admin", PASSWORD, is_admin=True)
    policy = ReadAllPolicy()
    assert policy.can_read(reader, None)
    assert not policy.can_administer(reader)
    assert policy.can_administer(admin)


def test_a_disabled_user_can_neither_read_nor_administer(service: AuthService):
    admin = service.create_user("admin", PASSWORD, is_admin=True)
    service.create_user("admin2", PASSWORD, is_admin=True)
    disabled = service.set_enabled(admin.id, False)
    policy = ReadAllPolicy()
    assert not policy.can_read(disabled, None)
    assert not policy.can_administer(disabled)
