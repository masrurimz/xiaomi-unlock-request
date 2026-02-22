"""Core business logic: NTP, HTTP, timing, worker protocol.

No Click or Rich imports — fully framework-agnostic for testability.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import httpx
import ntplib

# ── Constants ──────────────────────────────────────────────────────────
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

NTP_SERVERS = [
    "time.apple.com",
    "time.google.com",
    "ntp.aliyun.com",
    "pool.ntp.org",
    "ntp0.ntp-servers.net",
    "ntp1.ntp-servers.net",
]

TIME_OFFSETS_MS: list[float] = [1400, 900, 400, 100]

STATUS_URL = "https://sgp-api.buy.mi.com/bbs/api/global/user/bl-switch/state"
APPLY_URL = "https://sgp-api.buy.mi.com/bbs/api/global/apply/bl-auth"

APP_VER = "500411"
APP_VER_NAME = "5.4.11"
MAX_RETRY_SECS = 30


# ── Clock Protocol ────────────────────────────────────────────────────
@runtime_checkable
class Clock(Protocol):
    """Protocol for injectable time source (enables deterministic testing)."""

    def monotonic(self) -> float:
        """Return monotonic clock value in seconds."""
        ...

    def synced_now(self) -> datetime:
        """Return current time in Beijing timezone, NTP-adjusted."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for the given number of seconds."""
        ...


class RealClock:
    """Production clock: uses system monotonic + NTP offset."""

    def __init__(self, start_beijing: datetime, start_mono: float) -> None:
        self._start_beijing = start_beijing
        self._start_mono = start_mono

    def monotonic(self) -> float:
        return time.monotonic()

    def synced_now(self) -> datetime:
        return self._start_beijing + timedelta(seconds=time.monotonic() - self._start_mono)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# ── Typed results ──────────────────────────────────────────────────────
@dataclass
class NtpResult:
    success: bool
    beijing_time: datetime | None = None
    server: str = ""
    error: str = ""


@dataclass
class StatusResult:
    eligible: bool
    message: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApplyResult:
    worker_id: int
    approved: bool | None  # True=approved, False=rejected/quota, None=maybe
    attempts: int = 0
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ── Utilities ──────────────────────────────────────────────────────────
def gen_device_id() -> str:
    """Generate a random device ID (SHA1 hex, uppercase)."""
    return hashlib.sha1(f"{random.random()}-{time.time()}".encode()).hexdigest().upper()


def build_cookie(token: str, dev_id: str) -> str:
    """Build the Xiaomi API cookie string."""
    return f"new_bbs_serviceToken={token};versionCode={APP_VER};versionName={APP_VER_NAME};deviceId={dev_id};"


def calc_target_time(clock: Clock, offset_ms: float) -> tuple[datetime, datetime]:
    """Return (target_fire_time, deadline) for a worker given offset in ms.

    target = next midnight Beijing − offset_ms
    deadline = next midnight Beijing + MAX_RETRY_SECS
    """
    cur = clock.synced_now()
    # Midnight that just started today (00:00:00 of current calendar day)
    today_midnight = cur.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_since_midnight = (cur - today_midnight).total_seconds()
    # If workers start within MAX_RETRY_SECS after midnight (e.g. countdown just ended),
    # target the midnight we just crossed — not tomorrow's midnight 24h away.
    if seconds_since_midnight < MAX_RETRY_SECS:
        midnight = today_midnight
    else:
        midnight = today_midnight + timedelta(days=1)
    target = midnight - timedelta(milliseconds=offset_ms)
    deadline = midnight + timedelta(seconds=MAX_RETRY_SECS)
    return target, deadline


# ── NTP ────────────────────────────────────────────────────────────────
def sync_ntp(servers: list[str] = NTP_SERVERS) -> NtpResult:
    """Try each NTP server in order; return first successful result."""
    client = ntplib.NTPClient()
    for server in servers:
        try:
            resp = client.request(server, version=3)
            beijing = datetime.fromtimestamp(resp.tx_time, tz=timezone.utc).astimezone(BEIJING_TZ)
            return NtpResult(success=True, beijing_time=beijing, server=server)
        except Exception as e:
            last_err = str(e)
    return NtpResult(success=False, error=last_err if servers else "No NTP servers configured")


# ── Status Check ──────────────────────────────────────────────────────
async def check_status(token: str, client: httpx.AsyncClient | None = None) -> StatusResult:
    """Check Xiaomi account unlock eligibility.

    Returns StatusResult(eligible=True) if account can send unlock request.
    """
    dev = gen_device_id()
    headers = {"Cookie": build_cookie(token, dev)}

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=15.0))
    assert client is not None

    try:
        r = await client.get(STATUS_URL, headers=headers)
        data = r.json()
    except Exception as e:
        return StatusResult(eligible=False, message=f"Network error: {e}")
    finally:
        if own_client and client is not None:
            await client.aclose()

    return parse_status_response(data)


def parse_status_response(data: dict[str, Any]) -> StatusResult:
    """Parse raw Xiaomi status API response into typed StatusResult."""
    code = data.get("code")

    if code == 100004:
        return StatusResult(
            eligible=False,
            message="Token expired — get a fresh one.",
            raw=data,
        )

    info = data.get("data", {})
    is_pass = info.get("is_pass")
    btn = info.get("button_state")
    deadline = info.get("deadline_format", "")

    if is_pass == 1:
        return StatusResult(
            eligible=False,
            message=f"Already approved — unlock available until {deadline}",
            raw=data,
        )

    if is_pass == 4:
        if btn == 1:
            return StatusResult(eligible=True, message="Eligible to send unlock request", raw=data)
        if btn == 2:
            return StatusResult(
                eligible=True,
                message=f"Blocked until {deadline} — will try anyway",
                raw=data,
            )
        if btn == 3:
            return StatusResult(
                eligible=True,
                message="Account < 30 days old — will try anyway",
                raw=data,
            )

    return StatusResult(
        eligible=False,
        message=f"Unknown status: is_pass={is_pass}, button_state={btn}",
        raw=data,
    )


def parse_apply_response(data: dict[str, Any]) -> tuple[str, bool | None]:
    """Parse raw apply API response.

    Returns (message, approved):
      approved=True  → approved
      approved=False → quota/blocked (stop)
      approved=None  → maybe approved (100003)
      raises ValueError for retry-able rejections (100001)
    """
    code = data.get("code")
    info = data.get("data", {})
    result = info.get("apply_result")

    if code == 0:
        if result == 1:
            return "Approved!", True
        if result == 3:
            dl = info.get("deadline_format", "?")
            return f"Quota reached until {dl}", False
        if result == 4:
            dl = info.get("deadline_format", "?")
            return f"Blocked until {dl}", False

    if code == 100001:
        raise ValueError("rejected")  # caller should retry

    if code == 100003:
        return "Possibly approved (100003)", None

    return f"Unexpected response: code={code}", False


# ── Worker ────────────────────────────────────────────────────────────
async def run_worker(
    wid: int,
    token: str,
    offset_ms: float,
    clock: Clock,
    stop: asyncio.Event,
    dry_run: bool = False,
    on_attempt: Callable[[int, int, datetime], None] | None = None,
    dev_id: str | None = None,
) -> ApplyResult:
    """Single AQLR worker: waits until target time, then fires + retries.

    Args:
        wid: Worker ID (1-4)
        token: Xiaomi session token
        offset_ms: Fire this many ms before midnight
        clock: Injectable clock for testability
        stop: Shared event — set when any worker succeeds
        dry_run: If True, skip the actual POST
        on_attempt: Optional callback(wid, attempt, fire_time) for UI updates
        dev_id: Stable device ID to reuse (generated if not provided)
    """
    dev = dev_id or gen_device_id()
    target, deadline = calc_target_time(clock, offset_ms)

    # Coarse sleep (wake 1s early for spin-wait)
    wait = (target - clock.synced_now()).total_seconds()
    if wait > 2:
        await clock.sleep(wait - 1)

    # Spin-wait for precision
    while clock.synced_now() < target:
        await clock.sleep(0.0001)

    if dry_run:
        return ApplyResult(
            worker_id=wid,
            approved=None,
            attempts=0,
            message="[dry-run] Skipped POST",
        )

    headers = {
        "Cookie": build_cookie(token, dev),
        "Content-Type": "application/json; charset=utf-8",
        "Accept-Encoding": "gzip, deflate, br",
        "User-Agent": "okhttp/4.12.0",
        "Connection": "keep-alive",
    }
    body = b'{"is_retry":true}'

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=2.0),
        http2=False,
    ) as client:
        attempt = 0
        while clock.synced_now() < deadline and not stop.is_set():
            attempt += 1
            fire = clock.synced_now()

            if on_attempt:
                on_attempt(wid, attempt, fire)

            try:
                resp = await client.post(APPLY_URL, headers=headers, content=body)
                data = resp.json()
            except Exception:
                await clock.sleep(0.05)
                continue

            try:
                msg, approved = parse_apply_response(data)
            except ValueError:
                # 100001 rejected — retry immediately
                await clock.sleep(0.01)
                continue

            if approved is True:
                stop.set()
                return ApplyResult(worker_id=wid, approved=True, attempts=attempt, message=msg, raw=data)
            if approved is False:
                return ApplyResult(worker_id=wid, approved=False, attempts=attempt, message=msg, raw=data)
            # approved is None (100003 — maybe approved): verify status then retry (OG behavior)
            try:
                s = await client.get(STATUS_URL, headers={"Cookie": build_cookie(token, dev)})
                sdata = s.json()
                st = parse_status_response(sdata)
            except Exception:
                await clock.sleep(0.05)
                continue
            if not st.eligible:
                # Already approved or token expired
                approved_final = "Already approved" in st.message
                if approved_final:
                    stop.set()
                return ApplyResult(
                    worker_id=wid,
                    approved=True if approved_final else None,
                    attempts=attempt,
                    message=f"{msg} → {st.message}",
                    raw=data,
                )
            await clock.sleep(0.01)

    if stop.is_set():
        return ApplyResult(worker_id=wid, approved=None, attempts=attempt, message="Stopped (another worker succeeded)")

    return ApplyResult(
        worker_id=wid,
        approved=False,
        attempts=attempt,
        message=f"Timed out after {MAX_RETRY_SECS}s",
    )


async def run_workers(
    tokens: tuple[str, str],
    clock: Clock,
    offsets: list[float] = TIME_OFFSETS_MS,
    dry_run: bool = False,
    on_attempt: Callable[[int, int, datetime], None] | None = None,
) -> list[ApplyResult]:
    """Run 4 parallel AQLR workers with alternating tokens.

    Args:
        tokens: (firefox_token, chrome_token)
        clock: Injectable clock
        offsets: List of ms offsets before midnight (default: 4 workers)
        dry_run: Skip actual HTTP POSTs
        on_attempt: Optional callback(wid, attempt, fire_time) for UI updates
    """
    firefox, chrome = tokens
    token_list = [firefox, chrome, firefox, chrome]
    # One stable device_id per token, reused across workers (matches OG behavior)
    dev_firefox, dev_chrome = gen_device_id(), gen_device_id()
    dev_ids = [dev_firefox, dev_chrome, dev_firefox, dev_chrome]
    stop = asyncio.Event()

    tasks = [
        run_worker(i + 1, tok, off, clock, stop, dry_run, on_attempt, dev_id=dev)
        for i, (tok, off, dev) in enumerate(zip(token_list, offsets, dev_ids))
    ]
    results = await asyncio.gather(*tasks)
    return list(results)
