"""Fair admission: waits on the event loop, and takes turns between accounts."""

from __future__ import annotations

import asyncio

import pytest

from nineveh.admission import FairGate, GateLimits, RequestBudget, Throttled


def run(coroutine):
    return asyncio.run(coroutine)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_capacity_bounds_concurrent_holders():
    async def scenario():
        gate = FairGate("image", GateLimits(2))
        active = peak = 0

        async def job(key: str):
            nonlocal active, peak
            async with gate.slot(key):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(job(f"k{n % 3}") for n in range(12)))
        return peak

    assert run(scenario()) == 2


def test_a_waiting_account_goes_before_another_accounts_backlog():
    """A reader behind a script waits for one job, not for the whole backlog."""

    async def scenario():
        gate = FairGate("image", GateLimits(1))
        order: list[str] = []
        release = asyncio.Event()

        async def job(key: str, label: str):
            async with gate.slot(key):
                order.append(label)
                await release.wait()

        tasks = [asyncio.create_task(job("script", f"s{n}")) for n in range(10)]
        await _settle()
        tasks.append(asyncio.create_task(job("reader", "reader")))
        await _settle()
        for _ in range(11):
            release.set()
            await _settle()
            release.clear()
        await asyncio.gather(*tasks)
        return order

    order = run(scenario())
    assert order.index("reader") == 1  # right after the job already running


def test_equal_holders_are_served_in_turn():
    async def scenario():
        gate = FairGate("image", GateLimits(1))
        order: list[str] = []

        async def job(key: str):
            async with gate.slot(key):
                order.append(key)
                await asyncio.sleep(0)

        await asyncio.gather(*(job(key) for key in "aaabbbccc"))
        return "".join(order)

    served = run(scenario())
    assert served.startswith("a")
    assert "aa" not in served[1:7]


def test_one_key_never_holds_more_than_its_share():
    async def scenario():
        gate = FairGate("download", GateLimits(8, per_key=2))
        active = peak = 0

        async def job():
            nonlocal active, peak
            async with gate.slot("one"):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(job() for _ in range(6)))
        return peak

    assert run(scenario()) == 2


def test_a_runaway_backlog_is_refused_not_queued():
    async def scenario():
        gate = FairGate("image", GateLimits(1, queue_per_key=3))
        await gate.acquire("script")
        waiting = [asyncio.create_task(gate.acquire("script")) for _ in range(3)]
        await _settle()
        with pytest.raises(Throttled):
            await gate.acquire("script")
        # Another account is not affected by that backlog.
        other = asyncio.create_task(gate.acquire("reader"))
        await _settle()
        for task in (*waiting, other):
            task.cancel()
        await asyncio.gather(*waiting, other, return_exceptions=True)

    run(scenario())


def test_a_wait_that_times_out_is_refused_and_leaves_nothing_behind():
    async def scenario():
        gate = FairGate("image", GateLimits(1, timeout=0.05))
        await gate.acquire("holder")
        with pytest.raises(Throttled) as refused:
            await gate.acquire("late")
        assert refused.value.retry_after >= 1
        gate.release("holder")
        await asyncio.wait_for(gate.acquire("late"), 1)  # slot is free again
        gate.release("late")
        return gate._in_use, gate._waiting, gate._held, gate._last_served

    assert run(scenario()) == (0, {}, {}, {})


def test_a_cancelled_waiter_does_not_consume_a_slot():
    async def scenario():
        gate = FairGate("image", GateLimits(1))
        await gate.acquire("holder")
        waiter = asyncio.create_task(gate.acquire("gone"))
        await _settle()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        gate.release("holder")
        await asyncio.wait_for(gate.acquire("next"), 1)
        return gate._in_use, gate._held

    assert run(scenario()) == (1, {"next": 1})


def test_a_request_budget_allows_bursts_and_refills():
    now = [0.0]
    budget = RequestBudget(60, clock=lambda: now[0])
    assert all(budget.take("reader") == 0 for _ in range(60))
    assert budget.take("reader") == pytest.approx(1.0)
    assert budget.take("other") == 0
    now[0] += 1.0
    assert budget.take("reader") == 0


def test_a_request_budget_forgets_the_oldest_keys():
    budget = RequestBudget(1, max_keys=2)
    budget.take("a")
    budget.take("b")
    budget.take("c")
    assert list(budget._buckets) == ["b", "c"]
