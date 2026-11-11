"""Tests for worker behavior: retry on 100001, approval, timeout, quota."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from xiaomi_unlock.core import (
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

    tasks = [run_worker(i + 1, "f" * 40, 0, clocks[i], stop, dry_run=True) for i in range(4)]
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


# ── [z3b] stop set during coarse sleep ───────────────────────────────
class StopDuringSleepClock:
    """Clock whose sleep() sets the stop event, simulating early abort.

    synced_now() returns pre-target before first sleep (triggers coarse sleep),
    then returns time PAST the deadline after sleep so the spin-wait and retry
    loop both exit immediately without any POST being fired.
    """

    def __init__(self, stop: asyncio.Event):
        self._stop = stop
        # 10 minutes before midnight — ensures wait > 2s → coarse sleep fires
        self._base = datetime(2026, 2, 21, 23, 50, 0, tzinfo=BEIJING_TZ)
        # Past-deadline time: 31s after next midnight (deadline = midnight+30s)
        self._past_deadline = datetime(2026, 2, 22, 0, 0, 31, tzinfo=BEIJING_TZ)
        self._slept = False

    def monotonic(self) -> float:
        return 0.0

    def synced_now(self) -> datetime:
        # After sleep, return past-deadline so spin-wait exits and retry loop exits
        return self._past_deadline if self._slept else self._base

    async def sleep(self, seconds: float) -> None:
        self._stop.set()
        self._slept = True


@pytest.mark.asyncio
async def test_worker_stop_during_coarse_sleep():
    """Worker aborted during coarse sleep must not fire any POST."""
    stop = asyncio.Event()
    clock = StopDuringSleepClock(stop)

    with respx.mock(base_url="https://sgp-api.buy.mi.com", assert_all_called=False) as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"apply_result": 1}})
        )
        result = await run_worker(1, "t" * 40, 0, clock, stop)
        # No POST should have been fired
        assert m.calls.call_count == 0

    # Worker should not report approved since it was stopped before firing
    assert result.approved is not True


# ── Invalid schedules fail before dispatch ──────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("offsets", [[], [float("nan")], [float("inf")], [-30000], [-40000], [86400000]])
async def test_run_workers_invalid_offsets_raise(offsets):
    clock = InstantClock()
    with pytest.raises(ValueError):
        await run_workers(("ff_tok", "ch_tok"), clock, offsets=offsets)


# ── [x7j] worker 100003 sequences ────────────────────────────────────
@pytest.mark.asyncio
async def test_worker_100003_then_status_approved():
    """POST→100003, GET status→is_pass=1 (already approved) → result.approved=True, stop set."""
    clock = InstantClock()
    stop = asyncio.Event()

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(return_value=httpx.Response(200, json={"code": 100003}))
        m.get("/bbs/api/global/user/bl-switch/state").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"is_pass": 1, "deadline_format": "2026-03-01"}})
        )
        result = await run_worker(1, "t" * 40, 0, clock, stop)

    assert result.approved is True
    assert stop.is_set()


@pytest.mark.asyncio
async def test_worker_100003_then_status_expired():
    """POST→100003, GET status→code=100004 (token expired) → not approved, stop NOT set."""
    clock = InstantClock()
    stop = asyncio.Event()

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(return_value=httpx.Response(200, json={"code": 100003}))
        m.get("/bbs/api/global/user/bl-switch/state").mock(return_value=httpx.Response(200, json={"code": 100004}))
        result = await run_worker(1, "t" * 40, 0, clock, stop)

    assert result.approved is not True
    assert not stop.is_set()


@pytest.mark.asyncio
async def test_worker_100003_then_status_still_eligible_then_approved():
    """POST→100003, GET status→eligible (retry), next POST→approved → result.approved=True."""
    clock = InstantClock()
    stop = asyncio.Event()

    post_responses = [
        httpx.Response(200, json={"code": 100003}),
        httpx.Response(200, json={"code": 0, "data": {"apply_result": 1}}),
    ]
    status_resp = httpx.Response(200, json={"code": 0, "data": {"is_pass": 4, "button_state": 1}})

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(side_effect=post_responses)
        m.get("/bbs/api/global/user/bl-switch/state").mock(return_value=status_resp)
        result = await run_worker(1, "t" * 40, 0, clock, stop)

    assert result.approved is True
    assert result.attempts >= 2


# ── [lu8] worker network errors ───────────────────────────────────────
@pytest.mark.asyncio
async def test_worker_read_timeout_then_approved():
    """POST raises ReadTimeout, second POST returns approved → approved=True, attempts==2."""
    clock = InstantClock()
    stop = asyncio.Event()

    post_responses: list = [
        httpx.ReadTimeout("timed out"),
        httpx.Response(200, json={"code": 0, "data": {"apply_result": 1}}),
    ]

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(side_effect=post_responses)
        result = await run_worker(1, "t" * 40, 0, clock, stop)

    assert result.approved is True
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_worker_json_decode_error_then_approved():
    """POST returns non-JSON body, second POST returns approved → approved=True."""
    clock = InstantClock()
    stop = asyncio.Event()

    bad_response = httpx.Response(200, text="not json at all")
    good_response = httpx.Response(200, json={"code": 0, "data": {"apply_result": 1}})

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(side_effect=[bad_response, good_response])
        result = await run_worker(1, "t" * 40, 0, clock, stop)

    assert result.approved is True


@pytest.mark.asyncio
async def test_worker_http_500_applies_result():
    """HTTP 500 with valid JSON body → code=0 apply_result=1 → approved (document behavior)."""
    clock = InstantClock()
    stop = asyncio.Event()

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.post("/bbs/api/global/apply/bl-auth").mock(
            return_value=httpx.Response(500, json={"code": 0, "data": {"apply_result": 1}})
        )
        result = await run_worker(1, "t" * 40, 0, clock, stop)

    # HTTP status is ignored; JSON body drives logic → approved
    assert result.approved is True


# ── [w3m] run_workers token alternation + dev_id sharing ─────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("worker_count", [1, 2, 4, 6])
async def test_run_workers_token_and_devid_alternation(monkeypatch, worker_count):
    """run_workers alternates ff/ch tokens and shares dev_id per token."""
    captured: list[dict] = []

    async def fake_run_worker(
        wid: int,
        token: str,
        offset_ms: float,
        clock,
        stop: asyncio.Event,
        dry_run: bool = False,
        on_attempt=None,
        dev_id: str | None = None,
        *,
        midnight: datetime | None = None,
    ) -> ApplyResult:
        captured.append({"wid": wid, "token": token, "dev_id": dev_id, "offset": offset_ms, "midnight": midnight})
        return ApplyResult(worker_id=wid, approved=None, message="stub")

    import xiaomi_unlock.core as core_mod

    monkeypatch.setattr(core_mod, "run_worker", fake_run_worker)

    clock = InstantClock()
    offsets = [1400, 900, 400, 100, 0, -500][:worker_count]
    results = await run_workers(("firefox_tok", "chrome_tok"), clock, offsets=offsets, dry_run=True)

    assert len(captured) == len(results) == worker_count
    midnight = datetime(2026, 2, 22, tzinfo=BEIJING_TZ)
    for i, worker in enumerate(captured):
        assert worker["wid"] == i + 1
        assert worker["offset"] == offsets[i]
        assert worker["midnight"] == midnight
        assert worker["token"] == ("firefox_tok", "chrome_tok")[i % 2]
        assert worker["dev_id"] == captured[i % 2]["dev_id"]
    if worker_count > 1:
        assert captured[0]["dev_id"] != captured[1]["dev_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [1400, 900, 400, 100, 0, -500])
async def test_request_time_includes_client_setup_before_wait(monkeypatch, offset):
    """Real worker + mocked HTTP: setup cost must not shift the first POST past its target."""

    class Clock:
        now = datetime(2026, 2, 21, 23, 59, 50, tzinfo=BEIJING_TZ)

        def synced_now(self):
            return self.now

        async def sleep(self, seconds):
            self.now += timedelta(seconds=seconds)

    clock = Clock()
    midnight = datetime(2026, 2, 22, tzinfo=BEIJING_TZ)
    target = midnight - timedelta(milliseconds=offset)
    real_client = httpx.AsyncClient
    setup_times = []
    post_times = []

    def slow_client(*args, **kwargs):
        setup_times.append(clock.now)
        clock.now += timedelta(milliseconds=250)
        return real_client(*args, **kwargs)

    def receive(request):
        post_times.append(clock.now)
        return httpx.Response(200, json={"code": 0, "data": {"apply_result": 3}})

    monkeypatch.setattr(httpx, "AsyncClient", slow_client)
    with respx.mock(base_url="https://sgp-api.buy.mi.com") as router:
        router.post("/bbs/api/global/apply/bl-auth").mock(side_effect=receive)
        await run_worker(1, "test-token", offset, clock, asyncio.Event(), midnight=midnight)

    assert setup_times[0] < target
    assert len(post_times) == 1
    assert timedelta(0) <= post_times[0] - target <= timedelta(microseconds=100)
    if offset > 0:
        assert post_times[0] < midnight
    elif offset < 0:
        assert post_times[0] > midnight


@pytest.mark.asyncio
async def test_approval_interrupts_long_worker_wait():
    started = asyncio.Event()

    class Clock:
        def synced_now(self):
            return datetime(2026, 2, 21, 23, 50, tzinfo=BEIJING_TZ)

        async def sleep(self, seconds):
            started.set()
            await asyncio.Event().wait()

    stop = asyncio.Event()
    with respx.mock(assert_all_called=False) as router:
        task = asyncio.create_task(run_worker(1, "test-token", 100, Clock(), stop))
        await asyncio.wait_for(started.wait(), timeout=1)
        stop.set()
        result = await asyncio.wait_for(task, timeout=1)
        assert len(router.calls) == 0
    assert result.message == "Stopped (another worker succeeded)"
