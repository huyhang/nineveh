from __future__ import annotations

from typing import Protocol

from .domain import AccessGrant, Publication, ReadScope, User
from .ports import AccessRepository, UserRepository


class AccessManagementRepository(AccessRepository, UserRepository, Protocol):
    pass


class AuthorizationPolicy(Protocol):
    def read_scope(self, user: User) -> ReadScope: ...

    def can_read(self, user: User, publication: Publication | None) -> bool: ...

    def can_administer(self, user: User) -> bool: ...


class ReadAllPolicy:
    """Initial policy: enabled users can read everything; admins manage the service."""

    @staticmethod
    def read_scope(user: User) -> ReadScope:
        return ReadScope(unrestricted=user.enabled)

    @staticmethod
    def can_read(user: User, publication: Publication | None) -> bool:
        return user.enabled

    @staticmethod
    def can_administer(user: User) -> bool:
        return user.enabled and user.is_admin


class GrantPolicy:
    """Default-deny reader policy backed by persisted hierarchical grants."""

    def __init__(self, repository: AccessRepository) -> None:
        self._repository = repository

    def read_scope(self, user: User) -> ReadScope:
        if not user.enabled:
            return ReadScope()
        if user.is_admin:
            return ReadScope(unrestricted=True)
        return ReadScope(user_id=user.id)

    def can_read(self, user: User, publication: Publication | None) -> bool:
        if publication is None:
            return self.read_scope(user).unrestricted
        if user.enabled and user.is_admin:
            return True
        if not user.enabled:
            return False
        return any(
            _grant_matches(grant, publication)
            for grant in self._repository.access_grants(user.id)
        )

    @staticmethod
    def can_administer(user: User) -> bool:
        return user.enabled and user.is_admin


class AccessService:
    def __init__(self, repository: AccessManagementRepository) -> None:
        self._repository = repository

    def grants(self, user_id: str) -> list[AccessGrant]:
        return self._repository.access_grants(user_id)

    def replace(self, user_id: str, grants: list[AccessGrant]) -> None:
        user = self._repository.user_by_id(user_id)
        if not user:
            raise ValueError("User not found")
        if user.is_admin:
            raise ValueError("Administrators already have access to every library")
        candidates = [
            AccessGrant(user_id, item.library_id, item.category, item.series_id)
            for item in grants
        ]
        library_wide = {item.library_id for item in candidates if item.category is None}
        category_wide = {
            (item.library_id, item.category)
            for item in candidates
            if item.category and item.series_id is None
        }
        normalized = list(
            {
                (item.library_id, item.category, item.series_id): item
                for item in candidates
                if item.library_id not in library_wide
                and (item.library_id, item.category) not in category_wide
            }.values()
        )
        normalized.extend(
            AccessGrant(user_id, library_id) for library_id in sorted(library_wide)
        )
        normalized.extend(
            AccessGrant(user_id, library_id, category)
            for library_id, category in sorted(category_wide)
            if library_id not in library_wide
        )
        self._repository.replace_access_grants(user_id, normalized)


def _grant_matches(grant: AccessGrant, publication: Publication) -> bool:
    if grant.library_id != publication.library_id:
        return False
    if grant.category and grant.category.casefold() != publication.category.casefold():
        return False
    return not grant.series_id or grant.series_id == publication.series_id
