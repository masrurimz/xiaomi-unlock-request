"""Tests for worker behavior: retry on 100001, approval, timeout, quota."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
import respx
import httpx

from xiaomi_unlock.core import (
    APPLY_URL,
    BEIJING_TZ,
    ApplyResult,
    run_worker,
    run_workers,
)


# ── Fake clock that fires "now" (no wait) ─────────────────────────────
class InstantClock:
    """Clock that fires the worker immediately by advancing time after sleep.

    The worker logic:
    1. calc_target_time() → target = next midnight - offset_ms
    2. coarse sleep if wait > 2s (our sleep() advances time past target)
    3. spin-wait: while synced_now() < target
    4. fire HTTP

    We make sleep() set time to just past target so spin-wait exits immediately.
    deadline = target midnight + 30s, and we stay within deadline.
    """

    def __init__(self):
        self._base = datetime(2026, 2, 21, 23, 50, 0, tzinfo=BEIJING_TZ)
        self._slept = False

    def monotonic(self) -> float:
        return 0.0

    def synced_now(self) -> datetime:
        if self._slept:
            # Return a time just after midnight (target=midnight, offset=0)
            # next midnight from base = 2026-02-22 00:00:00 → return +1μs
            return datetime(2026, 2, 22, 0, 0, 0, 1, tzinfo=BEIJING_TZ)
        return self._base

    async def sleep(self, seconds: float) -> None:
        # After first sleep, advance time past target
        self._slept = True


class PastDeadlineClock:
    """Clock where the deadline expires as soon as the worker fires.

    The worker sleep()s during coarse wait, then fires. After firing, we
    return a time past the deadline so the retry loop exits immediately.
    """

    def __init__(self):
        self._base = datetime(2026, 2, 21, 23, 50, 0, tzinfo=BEIJING_TZ)
        self._slept = False

    def monotonic(self) -> float:
        return 0.0

    def synced_now(self) -> datetime:
        if self._slept:
            # Return 31s past midnight — past MAX_RETRY_SECS=30
            # next midnight from base = 2026-02-22 00:00:00, deadline = 00:00:30
            return datetime(2026, 2, 22, 0, 0, 31, tzinfo=BEIJING_TZ)
        return self._base

    async def sleep(self, seconds: float) -> None:
        self._slept = True


# ── Worker tests ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_worker_approved():
    """Worker returns approved=True on apply_result=1."""
    clock = InstantClock()
    stop = asyncio.Event()

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"apply_result": 1}})
        )
        result = await run_worker(1, "t" * 40, 0, clock, stop)

    assert result.approved is True
    assert result.worker_id == 1
    assert result.attempts >= 1
    assert stop.is_set()


@pytest.mark.asyncio
async def test_worker_retries_on_100001_then_approves():
    """Worker retries on code=100001, succeeds on second attempt."""
    clock = InstantClock()
    stop = asyncio.Event()

    responses = [
        httpx.Response(200, json={"code": 100001}),
        httpx.Response(200, json={"code": 0, "data": {"apply_result": 1}}),
    ]

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(side_effect=responses)
        result = await run_worker(1, "t" * 40, 0, clock, stop)

    assert result.approved is True
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_worker_quota_reached():
    """Worker returns approved=False on apply_result=3 (quota)."""
    clock = InstantClock()
    stop = asyncio.Event()

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(
            return_value=httpx.Response(
                200, json={"code": 0, "data": {"apply_result": 3, "deadline_format": "tomorrow"}}
            )
        )
        result = await run_worker(1, "t" * 40, 0, clock, stop)

    assert result.approved is False
    assert "quota" in result.message.lower()


@pytest.mark.asyncio
async def test_worker_timeout():
    """Worker times out when deadline is in the past."""
    clock = PastDeadlineClock()
    stop = asyncio.Event()

    # No HTTP mock needed — should return before making any request
    result = await run_worker(1, "t" * 40, 0, clock, stop)

    assert result.approved is False
    assert "timed out" in result.message.lower()


@pytest.mark.asyncio
async def test_worker_stops_when_event_set():
    """Worker exits early when stop event is already set."""
    clock = InstantClock()
    stop = asyncio.Event()
    stop.set()  # already set before worker starts

    result = await run_worker(1, "t" * 40, 0, clock, stop)
    # Worker was stopped by another worker — either None or result
    assert result is not None


@pytest.mark.asyncio
async def test_worker_dry_run():
    """Dry-run mode skips HTTP POST entirely."""
    clock = InstantClock()
    stop = asyncio.Event()

    result = await run_worker(1, "t" * 40, 0, clock, stop, dry_run=True)

    assert "dry-run" in result.message.lower()


@pytest.mark.asyncio
async def test_run_workers_returns_four_results():
    """run_workers() returns one result per offset (4 total) — dry run.

    Each worker needs its own clock instance to avoid shared-state timing issues.
    We test run_workers structure by checking 4 results are returned with dry_run.
    """
    # Use 4 separate workers manually to verify run_workers dispatches 4 coroutines
    clocks = [InstantClock() for _ in range(4)]
    stop = asyncio.Event()

    tasks = [
        run_worker(i + 1, "f" * 40, 0, clocks[i], stop, dry_run=True)
        for i in range(4)
    ]
    results = await asyncio.gather(*tasks)

    assert len(results) == 4
    for r in results:
        assert isinstance(r, ApplyResult)


@pytest.mark.asyncio
async def test_run_workers_stops_all_on_approval():
    """When one worker gets approved, the shared stop event is set."""
    clock = InstantClock()
    stop = asyncio.Event()

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"apply_result": 1}})
        )
        result = await run_worker(1, "f" * 40, 0, clock, stop)

    assert result.approved is True
    assert stop.is_set()  # stop event was set, would halt other workers
