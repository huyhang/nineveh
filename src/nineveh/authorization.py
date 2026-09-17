from __future__ import annotations

from typing import Protocol

from .domain import Publication, User


class AuthorizationPolicy(Protocol):
    def can_read(self, user: User, publication: Publication) -> bool: ...

    def can_administer(self, user: User) -> bool: ...


class ReadAllPolicy:
    """Initial policy: enabled users can read everything; admins manage the service."""

    @staticmethod
    def can_read(user: User, publication: Publication) -> bool:
        return user.enabled

    @staticmethod
    def can_administer(user: User) -> bool:
        return user.enabled and user.is_admin
