from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .domain import Session, User
from .ports import UserRepository


class AuthenticationError(Exception):
    pass


class InvalidUserInput(ValueError):
    pass


class LastAdministratorError(ValueError):
    pass


class AuthService:
    USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")

    def __init__(
        self,
        repository: UserRepository,
        session_hours: int = 168,
        hash_workers: int = 2,
    ) -> None:
        self._repository = repository
        self._session_hours = session_hours
        # OWASP's memory-constrained Argon2id profile: 19 MiB, 2 iterations, one lane.
        self._hasher = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(24))
        # Bounds peak hashing memory at roughly `hash_workers` x 19 MiB.
        self._hash_slots = threading.BoundedSemaphore(hash_workers)
        self._basic_cache: OrderedDict[str, tuple[User, float]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def bootstrap_admin(self, username: str, password: str | None) -> User | None:
        if self._repository.user_count() != 0:
            return None
        if not password:
            raise RuntimeError(
                "No users exist. Set NINEVEH_ADMIN_PASSWORD or "
                "NINEVEH_ADMIN_PASSWORD_FILE for the first startup."
            )
        return self.create_user(username, password, is_admin=True)

    def authenticate(self, username: str, password: str) -> User:
        cache_key = self._credential_key(username, password)
        with self._cache_lock:
            cached = self._basic_cache.get(cache_key)
            if cached and cached[1] > time.monotonic():
                self._basic_cache.move_to_end(cache_key)
                return cached[0]
            self._basic_cache.pop(cache_key, None)
        user = self._repository.user_by_username(username)
        candidate_hash = user.password_hash if user else self._dummy_hash
        try:
            with self._hash_slots:
                valid = self._hasher.verify(candidate_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if not user or not user.enabled or not valid:
            raise AuthenticationError("Invalid username or password")
        if self._hasher.check_needs_rehash(user.password_hash):
            with self._hash_slots:
                password_hash = self._hasher.hash(password)
            updated = self._repository.update_user(user.id, password_hash=password_hash)
            if updated:
                user = updated
        with self._cache_lock:
            self._basic_cache[cache_key] = (user, time.monotonic() + 300)
            self._basic_cache.move_to_end(cache_key)
            while len(self._basic_cache) > 128:
                self._basic_cache.popitem(last=False)
        return user

    def create_user(
        self, username: str, password: str, *, is_admin: bool = False
    ) -> User:
        username = username.strip()
        self._validate_username(username)
        self._validate_password(password)
        with self._hash_slots:
            password_hash = self._hasher.hash(password)
        return self._repository.create_user(username, password_hash, is_admin)

    def reset_password(self, user_id: str, password: str) -> User | None:
        self._validate_password(password)
        with self._hash_slots:
            password_hash = self._hasher.hash(password)
        self._clear_basic_cache()
        return self._repository.update_user(user_id, password_hash=password_hash)

    def set_enabled(self, user_id: str, enabled: bool) -> User | None:
        user = self._repository.user_by_id(user_id)
        if not user:
            return None
        if (
            not enabled
            and user.is_admin
            and self._repository.enabled_admin_count() <= 1
        ):
            raise LastAdministratorError(
                "The final enabled administrator cannot be disabled"
            )
        self._clear_basic_cache()
        return self._repository.update_user(user_id, enabled=enabled)

    def new_session(self, user: User) -> Session:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(hours=self._session_hours)
        self._repository.create_session(
            user.id, self._token_hash(token), csrf_token, expires_at.isoformat()
        )
        return Session(token, csrf_token, user, expires_at)

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        result = self._repository.session(
            self._token_hash(token), datetime.now(UTC).isoformat()
        )
        if not result:
            return None
        user, csrf_token, expires_at = result
        return Session(token, csrf_token, user, datetime.fromisoformat(expires_at))

    def end_session(self, token: str | None) -> None:
        if token:
            self._repository.delete_session(self._token_hash(token))

    @staticmethod
    def valid_csrf(session: Session, supplied: str | None) -> bool:
        return bool(supplied) and hmac.compare_digest(session.csrf_token, supplied)

    @classmethod
    def _validate_username(cls, username: str) -> None:
        if not cls.USERNAME_PATTERN.fullmatch(username):
            raise InvalidUserInput(
                "Username must be 3-64 characters and contain only letters, numbers, ., _, or -."
            )

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 12:
            raise InvalidUserInput("Password must be at least 12 characters")
        if len(password) > 1024:
            raise InvalidUserInput("Password is too long")

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _credential_key(username: str, password: str) -> str:
        value = f"{username.casefold()}\0{password}".encode()
        return hashlib.sha256(value).hexdigest()

    def _clear_basic_cache(self) -> None:
        with self._cache_lock:
            self._basic_cache.clear()
