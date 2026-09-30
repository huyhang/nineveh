"""Fair admission for expensive work.

Nineveh does its blocking work -- extraction, resizing, password hashing,
SQLite -- on one shared thread pool. A request that waits for capacity *inside*
that pool holds a thread while it waits, so one account's backlog becomes
everyone's queue. The gates here wait on the event loop instead: only a request
that has been admitted takes a thread.

When a slot frees it goes to the waiting account that holds the fewest, so a
reader queued behind a script waits for at most one job, not for the script's
backlog. Waiting is the normal answer. Only a backlog far beyond anything a
reader app produces is refused, with a `Retry-After`.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from .config import Settings


class Throttled(Exception):
    """A request past a fairness ceiling; the caller should retry later."""

    def __init__(self, detail: str, retry_after: float = 5) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retry_after = max(1, math.ceil(retry_after))


@dataclass(frozen=True, slots=True)
class GateLimits:
    capacity: int
    per_key: int | None = None
    queue_per_key: int = 128
    timeout: float = 30.0


class FairGate:
    """Bounded concurrency, shared fairly between keys, without holding threads."""

    def __init__(self, name: str, limits: GateLimits) -> None:
        self.name = name
        self._limits = limits
        self._in_use = 0
        self._held: dict[str, int] = {}
        self._waiting: dict[str, deque[asyncio.Future[None]]] = {}
        self._last_served: dict[str, int] = {}
        self._turn = 0

    @asynccontextmanager
    async def slot(self, key: str) -> AsyncIterator[None]:
        await self.acquire(key)
        try:
            yield
        finally:
            self.release(key)

    async def acquire(self, key: str) -> None:
        queue = self._waiting.setdefault(key, deque())
        if len(queue) >= self._limits.queue_per_key:
            raise Throttled(f"Too many {self.name} requests are waiting")
        future = asyncio.get_running_loop().create_future()
        queue.append(future)
        self._dispatch()
        try:
            async with asyncio.timeout(self._limits.timeout):
                await future
        except TimeoutError as error:
            self._abandon(key, future)
            raise Throttled(f"The {self.name} queue is busy") from error
        except asyncio.CancelledError:
            self._abandon(key, future)
            raise

    def release(self, key: str) -> None:
        self._in_use -= 1
        remaining = self._held[key] - 1
        if remaining:
            self._held[key] = remaining
        else:
            del self._held[key]
        self._dispatch()
        self._forget_if_idle(key)

    def _abandon(self, key: str, future: asyncio.Future[None]) -> None:
        if future.done() and not future.cancelled():
            self.release(key)  # granted just as the wait ended: pass it on
            return
        future.cancel()
        queue = self._waiting.get(key)
        if queue is not None:
            with suppress(ValueError):
                queue.remove(future)
            if not queue:
                del self._waiting[key]
        self._forget_if_idle(key)

    def _dispatch(self) -> None:
        while self._in_use < self._limits.capacity:
            key = self._next_key()
            if key is None:
                return
            self._grant(key)

    def _next_key(self) -> str | None:
        eligible = [key for key in self._waiting if self._may_hold(key)]
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda key: (self._held.get(key, 0), self._last_served.get(key, 0)),
        )

    def _may_hold(self, key: str) -> bool:
        limit = self._limits.per_key
        return limit is None or self._held.get(key, 0) < limit

    def _grant(self, key: str) -> None:
        queue = self._waiting[key]
        future = queue.popleft()
        if not queue:
            del self._waiting[key]
        if future.done():  # cancelled while it queued
            return
        self._in_use += 1
        self._held[key] = self._held.get(key, 0) + 1
        self._turn += 1
        self._last_served[key] = self._turn
        future.set_result(None)

    def _forget_if_idle(self, key: str) -> None:
        if key not in self._held and key not in self._waiting:
            self._last_served.pop(key, None)


class RequestBudget:
    """A per-key token bucket: generous bursts pass, sustained floods do not."""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 4096,
    ) -> None:
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def take(self, key: str) -> float:
        """Spend one request: 0 when admitted, else seconds until one is free."""
        now = self._clock()
        tokens, stamp = self._buckets.pop(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - stamp) * self._rate)
        if tokens >= 1:
            tokens -= 1
            wait = 0.0
        else:
            wait = (1 - tokens) / self._rate
        self._buckets[key] = (tokens, now)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)
        return wait


@dataclass(frozen=True, slots=True)
class Admission:
    media: FairGate
    ranges: FairGate
    downloads: FairGate
    passwords: FairGate
    requests: RequestBudget

    @classmethod
    def from_settings(cls, settings: Settings) -> Admission:
        return cls(
            media=FairGate("image", GateLimits(settings.extract_workers)),
            ranges=FairGate(
                "page range",
                GateLimits(settings.range_workers, 1, queue_per_key=4, timeout=60),
            ),
            downloads=FairGate(
                "download",
                GateLimits(
                    settings.download_streams,
                    settings.download_streams_per_account,
                    queue_per_key=32,
                    timeout=300,
                ),
            ),
            passwords=FairGate(
                "sign-in", GateLimits(settings.hash_workers, queue_per_key=16)
            ),
            requests=RequestBudget(settings.account_requests_per_minute),
        )
