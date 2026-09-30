"""The login guard: progressive delays, serialized attempts, shared verification."""

from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime

import pytest

from nineveh.admission import Throttled
from nineveh.auth import AuthenticationError, PublicAdminRefused
from nineveh.domain import User
from nineveh.logins import LoginGuard

ADDRESS = "203.0.113.9"
USER = User("u1", "reader", "hash", False, True, datetime.now(UTC))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Observer:
    def __init__(self) -> None:
        self.failures: list[tuple[str, str, type]] = []
        self.throttles: list[str] = []

    async def failed(self, address, username, error) -> None:
        self.failures.append((address, username, type(error)))

    async def throttled(self, address) -> None:
        self.throttles.append(address)


def guard(clock: Clock, observer: Observer | None = None, **kwargs) -> LoginGuard:
    return LoginGuard(observer, clock=clock, sleep=clock.sleep, **kwargs)


async def wrong() -> User:
    raise AuthenticationError("Invalid username or password")


async def right() -> User:
    return USER


_keys = itertools.count()


async def attempt(subject: LoginGuard, check, *, username="reader"):
    """One attempt with a credential no other attempt shares."""
    try:
        return await subject.verify(ADDRESS, username, f"key{next(_keys)}", check)
    except AuthenticationError:
        return None


def test_each_failure_doubles_the_wait_up_to_a_ceiling():
    clock = Clock()
    subject = guard(clock, max_delay=8)

    async def scenario():
        for _ in range(6):
            await attempt(subject, wrong)

    asyncio.run(scenario())
    assert clock.slept == [1, 2, 4, 8, 8]


def test_a_clean_attempt_never_waits_and_success_clears_the_account():
    clock = Clock()
    subject = guard(clock)

    async def scenario():
        await attempt(subject, right)
        await attempt(subject, wrong)
        await attempt(subject, right)  # waits 1 s, then clears
        await attempt(subject, right)

    asyncio.run(scenario())
    assert clock.slept == [1]


def test_signing_in_to_your_own_account_does_not_reset_the_address():
    """A guesser cannot launder an address by logging in as themselves."""
    clock = Clock()
    subject = guard(clock, address_grace=2)

    async def scenario():
        for victim in ("anna", "bert", "cleo"):
            await attempt(subject, wrong, username=victim)
            await attempt(subject, right, username="guesser")
        return subject.delay(ADDRESS, "dora")

    assert asyncio.run(scenario()) == 1  # three failures, past a grace of two


def test_an_address_failing_across_accounts_slows_after_its_grace():
    clock = Clock()
    subject = guard(clock, address_grace=3)

    async def scenario():
        for victim in ("a1", "a2", "a3"):
            await attempt(subject, wrong, username=victim)
        before = subject.delay(ADDRESS, "fresh")
        await attempt(subject, wrong, username="a4")
        return before, subject.delay(ADDRESS, "fresh")

    assert asyncio.run(scenario()) == (0, 1)


def test_failures_are_forgotten_after_a_quiet_period():
    clock = Clock()
    subject = guard(clock, forget_after=60)

    async def scenario():
        for _ in range(3):
            await attempt(subject, wrong)
        clock.now += 61
        return subject.delay(ADDRESS, "reader")

    assert asyncio.run(scenario()) == 0


def test_attempts_from_one_address_run_one_at_a_time():
    running = peak = 0

    async def slow() -> User:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        raise AuthenticationError("no")

    async def scenario():
        subject = LoginGuard()
        await asyncio.gather(
            *(attempt(subject, slow, username=f"user{n}") for n in range(8))
        )

    asyncio.run(scenario())
    assert peak == 1


def test_parallel_guesses_each_pay_the_growing_delay():
    """Opening more connections does not buy a guesser concurrent attempts."""
    clock = Clock()
    subject = guard(clock, max_waiting=64)

    async def scenario():
        await asyncio.gather(*(attempt(subject, wrong) for _ in range(5)))

    asyncio.run(scenario())
    assert clock.slept == [1, 2, 4, 8]


def test_a_flood_from_one_address_is_refused_once_the_queue_is_full():
    observer = Observer()

    async def scenario():
        subject = LoginGuard(observer, max_waiting=2)
        gate = asyncio.Event()

        async def blocked() -> User:
            await gate.wait()
            return USER

        first = [asyncio.create_task(attempt(subject, blocked)) for _ in range(2)]
        for _ in range(5):
            await asyncio.sleep(0)
        with pytest.raises(Throttled):
            await asyncio.wait_for(
                subject.verify(ADDRESS, "reader", "another", right), 1
            )
        gate.set()
        await asyncio.gather(*first)

    asyncio.run(scenario())
    assert observer.throttles == [ADDRESS]


def test_nothing_waits_in_line_past_the_queue_timeout():
    observer = Observer()

    async def scenario():
        subject = LoginGuard(observer, queue_timeout=0.05)
        gate = asyncio.Event()

        async def blocked() -> User:
            await gate.wait()
            return USER

        holder = asyncio.create_task(attempt(subject, blocked))
        await asyncio.sleep(0)
        with pytest.raises(Throttled):
            await subject.verify(ADDRESS, "reader", "late", right)
        gate.set()
        assert await holder == USER
        return subject._waiting, subject._locks

    assert asyncio.run(scenario()) == ({}, {})
    assert observer.throttles == [ADDRESS]


def test_identical_credentials_arriving_together_are_verified_once():
    """An OPDS app opening its catalog sends one password many times at once."""
    calls = 0

    async def once() -> User:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return USER

    async def scenario():
        subject = LoginGuard()
        return await asyncio.gather(
            *(subject.verify(ADDRESS, "reader", "same", once) for _ in range(12))
        )

    assert asyncio.run(scenario()) == [USER] * 12
    assert calls == 1


def test_a_shared_failure_is_recorded_once():
    observer = Observer()

    async def slow_wrong() -> User:
        await asyncio.sleep(0.01)
        raise AuthenticationError("no")

    async def scenario():
        subject = LoginGuard(observer)
        return await asyncio.gather(
            *(subject.verify(ADDRESS, "reader", "same", slow_wrong) for _ in range(5)),
            return_exceptions=True,
        )

    results = asyncio.run(scenario())
    assert all(isinstance(result, AuthenticationError) for result in results)
    assert observer.failures == [(ADDRESS, "reader", AuthenticationError)]


def test_a_follower_verifies_for_itself_when_the_leader_goes_away():
    async def scenario():
        subject = LoginGuard()
        started = asyncio.Event()

        async def hangs() -> User:
            started.set()
            await asyncio.sleep(10)
            return USER

        leader = asyncio.create_task(subject.verify(ADDRESS, "reader", "k", hangs))
        await started.wait()
        follower = asyncio.create_task(subject.verify(ADDRESS, "reader", "k", right))
        await asyncio.sleep(0)
        leader.cancel()
        return await asyncio.wait_for(follower, 1)

    assert asyncio.run(scenario()) == USER


def test_cancelling_leader_and_follower_together_cancels_both():
    async def scenario():
        subject = LoginGuard()
        started = asyncio.Event()

        async def hangs() -> User:
            started.set()
            await asyncio.sleep(10)
            return USER

        tasks = [
            asyncio.create_task(subject.verify(ADDRESS, "reader", "k", hangs))
            for _ in range(2)
        ]
        await started.wait()
        await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 1
        )
        return [type(result) for result in results], subject._flights

    assert asyncio.run(scenario()) == ([asyncio.CancelledError] * 2, {})


def test_a_refused_administrator_counts_as_a_failure():
    clock = Clock()
    observer = Observer()
    subject = guard(clock, observer)

    async def refused() -> User:
        raise PublicAdminRefused("Invalid username or password")

    async def scenario():
        await attempt(subject, refused, username="admin")
        return subject.delay(ADDRESS, "admin")

    assert asyncio.run(scenario()) == 1
    assert observer.failures == [(ADDRESS, "admin", PublicAdminRefused)]
