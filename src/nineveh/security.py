"""Abuse-facing signals: failed sign-ins, throttled accounts, refused storage.

`SecurityLog` owns the mechanics -- identity, timestamp, storage -- while
callers supply the sentence, because they hold the facts. It mirrors
`AuditTrail` deliberately: the two feeds sit side by side for an administrator
but are stored apart, so abuse noise can never crowd the librarian's permission
history out of view. The table is bounded by a trigger, so a password spray can
bury old signals but never grow the database.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .auth import AuthenticationError, PublicAdminRefused
from .domain import SecurityEvent
from .ports import SecurityRepository

LOGGER = logging.getLogger(__name__)


class SecurityLog:
    def __init__(
        self,
        repository: SecurityRepository,
        *,
        clock: Callable[[], float] = time.monotonic,
        quiet_seconds: float = 60.0,
        max_keys: int = 4096,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._quiet = quiet_seconds
        self._max_keys = max_keys
        self._recent: OrderedDict[str, float] = OrderedDict()

    def record(
        self,
        kind: str,
        summary: str,
        detail: dict[str, Any] | None = None,
        *,
        once_per: str | None = None,
    ) -> SecurityEvent | None:
        """Store one signal. `once_per` keys a signal recorded at most once a minute."""
        if once_per is not None and self._recently(f"{kind}\0{once_per}"):
            return None
        event = SecurityEvent(
            id=str(uuid.uuid4()),
            kind=kind,
            summary=summary,
            created_at=datetime.now(UTC),
            detail=detail,
        )
        LOGGER.warning("security event %s: %s", kind, summary)
        return self._repository.record_security_event(event)

    def recent(
        self, *, kind: str | None = None, limit: int = 20
    ) -> list[SecurityEvent]:
        return self._repository.security_events(kind=kind, limit=limit)

    def _recently(self, key: str) -> bool:
        now = self._clock()
        last = self._recent.get(key)
        if last is not None and now - last < self._quiet:
            return True
        self._recent[key] = now
        self._recent.move_to_end(key)
        while len(self._recent) > self._max_keys:
            self._recent.popitem(last=False)
        return False


class SignInEvents:
    """Feeds the login guard's observations into the security log."""

    def __init__(self, log: SecurityLog) -> None:
        self._log = log

    async def failed(
        self, address: str, username: str, error: AuthenticationError
    ) -> None:
        if isinstance(error, PublicAdminRefused):
            kind = "sign_in.admin_public"
            summary = (
                f"Administrator “{username}” tried to sign in through the public "
                f"origin from {address}"
            )
        else:
            kind, summary = (
                "sign_in.failed",
                f"Failed sign-in for “{username}” from {address}",
            )
        await asyncio.to_thread(
            self._log.record, kind, summary, {"username": username, "address": address}
        )

    async def throttled(self, address: str) -> None:
        await asyncio.to_thread(
            self._log.record,
            "sign_in.throttled",
            f"Sign-in attempts from {address} were refused while others queued",
            {"address": address},
            once_per=address,
        )
