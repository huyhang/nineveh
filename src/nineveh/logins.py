"""Progressive delays against password guessing, without lockouts.

Each failure makes the next attempt for the same address and account wait
longer -- 1 s, 2 s, 4 s, up to 30 s -- and an address failing across many
accounts is slowed too, once it is past a household's worth of typos. Attempts
from one address run one at a time, so opening more connections buys a guesser
nothing, and a burst of identical credentials (an OPDS app opening its
catalog) is verified once. No account is ever locked, so there is no lockout to
turn against a real reader, and failures are forgotten after a quiet period.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Protocol

from .admission import Throttled
from .auth import AuthenticationError
from .domain import User

Check = Callable[[], Awaitable[User]]


class SignInObserver(Protocol):
    async def failed(
        self, address: str, username: str, error: AuthenticationError
    ) -> None: ...

    async def throttled(self, address: str) -> None: ...


class SilentObserver:
    async def failed(
        self, address: str, username: str, error: AuthenticationError
    ) -> None:
        return None

    async def throttled(self, address: str) -> None:
        return None


class LoginGuard:
    def __init__(
        self,
        observer: SignInObserver | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        address_grace: int = 10,
        forget_after: float = 15 * 60,
        max_waiting: int = 16,
        queue_timeout: float | None = None,
        max_keys: int = 4096,
    ) -> None:
        self._observer = observer or SilentObserver()
        self._clock = clock
        self._sleep = sleep
        self._base = base_delay
        self._max = max_delay
        self._grace = address_grace
        self._forget_after = forget_after
        self._max_waiting = max_waiting
        # Nothing waits in line for longer than two maximum delays: a guesser's
        # abandoned attempts drain away instead of queueing for minutes.
        self._queue_timeout = 2 * max_delay if queue_timeout is None else queue_timeout
        self._max_keys = max_keys
        self._failures: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._flights: dict[str, asyncio.Future[User]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiting: dict[str, int] = {}

    async def verify(
        self, address: str, username: str, credential: str, check: Check
    ) -> User:
        """Run `check` under the guard; identical concurrent credentials share it."""
        flight = self._flights.get(credential)
        if flight is None:
            return await self._lead(address, username, credential, check)
        try:
            return await asyncio.shield(flight)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if not flight.cancelled() or (task is not None and task.cancelling()):
                raise  # this request itself is being cancelled
        # The request that was verifying went away; verify this one instead.
        return await self.verify(address, username, credential, check)

    def delay(self, address: str, username: str) -> float:
        now = self._clock()
        own = self._count(_account_key(address, username), now)
        shared = self._count(_address_key(address), now) - self._grace
        exponent = max(own, shared)
        if exponent <= 0:
            return 0.0
        return min(self._max, self._base * 2 ** min(exponent - 1, 16))

    async def _lead(
        self, address: str, username: str, credential: str, check: Check
    ) -> User:
        flight: asyncio.Future[User] = asyncio.get_running_loop().create_future()
        self._flights[credential] = flight
        try:
            user = await self._attempt(address, username, check)
        except asyncio.CancelledError:
            flight.cancel()
            raise
        except Exception as error:
            flight.set_exception(error)
            flight.exception()  # followers may be none; mark it retrieved
            raise
        finally:
            del self._flights[credential]
        flight.set_result(user)
        return user

    async def _attempt(self, address: str, username: str, check: Check) -> User:
        if self._waiting.get(address, 0) >= self._max_waiting:
            await self._refuse(address, username)
        self._waiting[address] = self._waiting.get(address, 0) + 1
        lock = self._locks.setdefault(address, asyncio.Lock())
        try:
            try:
                async with asyncio.timeout(self._queue_timeout):
                    await lock.acquire()
            except TimeoutError:
                await self._refuse(address, username)
            try:
                return await self._guarded(address, username, check)
            finally:
                lock.release()
        finally:
            self._waiting[address] -= 1
            if not self._waiting[address]:
                del self._waiting[address]
                del self._locks[address]

    async def _refuse(self, address: str, username: str) -> None:
        await self._observer.throttled(address)
        raise Throttled(
            "Too many sign-in attempts from this address",
            self.delay(address, username) or 5,
        )

    async def _guarded(self, address: str, username: str, check: Check) -> User:
        delay = self.delay(address, username)
        if delay:
            await self._sleep(delay)
        try:
            user = await check()
        except AuthenticationError as error:
            self._record_failure(address, username)
            await self._observer.failed(address, username, error)
            raise
        # Only the account's own count clears: a guesser signing in to an
        # account of their own must not reset the address they guess from.
        self._failures.pop(_account_key(address, username), None)
        return user

    def _count(self, key: str, now: float) -> int:
        entry = self._failures.get(key)
        if entry is None:
            return 0
        count, last = entry
        if now - last > self._forget_after:
            del self._failures[key]
            return 0
        return count

    def _record_failure(self, address: str, username: str) -> None:
        now = self._clock()
        for key in (_address_key(address), _account_key(address, username)):
            self._failures[key] = (self._count(key, now) + 1, now)
            self._failures.move_to_end(key)
        while len(self._failures) > self._max_keys:
            self._failures.popitem(last=False)


def _address_key(address: str) -> str:
    return f"address\0{address}"


def _account_key(address: str, username: str) -> str:
    return f"account\0{address}\0{username.strip().casefold()}"
