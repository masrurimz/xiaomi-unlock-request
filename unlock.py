#!/usr/bin/env python3
"""
Xiaomi Bootloader Unlock Request (AQLR method)

Sends 4 parallel HTTP requests to Xiaomi's unlock API at exactly 00:00 Beijing
time with staggered timing offsets to beat the daily quota reset.

Usage:
  1. Put your tokens in token.txt (2 lines — see README.md)
  2. Run: uv run python unlock.py
"""

import asyncio
import hashlib
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import ntplib

# ── Config ──────────────────────────────────────────────────────────
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

NTP_SERVERS = [
    "time.apple.com",
    "time.google.com",
    "ntp.aliyun.com",
    "pool.ntp.org",
    "ntp0.ntp-servers.net",
    "ntp1.ntp-servers.net",
]

TIME_OFFSETS_MS = [1400, 900, 400, 100]

STATUS_URL = "https://sgp-api.buy.mi.com/bbs/api/global/user/bl-switch/state"
APPLY_URL = "https://sgp-api.buy.mi.com/bbs/api/global/apply/bl-auth"

APP_VER = "500411"
APP_VER_NAME = "5.4.11"
MAX_RETRY_SECS = 30

# ── ANSI ────────────────────────────────────────────────────────────
G = "\033[32m"
BG = "\033[1;32m"
Y = "\033[33m"
BY = "\033[1;33m"
R = "\033[31m"
BR = "\033[1;31m"
RST = "\033[0m"


def gen_device_id() -> str:
    return hashlib.sha1(f"{random.random()}-{time.time()}".encode()).hexdigest().upper()


def build_cookie(token: str, dev_id: str) -> str:
    return (
        f"new_bbs_serviceToken={token};"
        f"versionCode={APP_VER};"
        f"versionName={APP_VER_NAME};"
        f"deviceId={dev_id};"
    )


# ── NTP ─────────────────────────────────────────────────────────────
def sync_ntp() -> datetime | None:
    client = ntplib.NTPClient()
    for server in NTP_SERVERS:
        try:
            print(f"  {Y}Trying {server}...{RST}", end=" ", flush=True)
            resp = client.request(server, version=3)
            t = datetime.fromtimestamp(resp.tx_time, tz=timezone.utc).astimezone(BEIJING_TZ)
            print(f"{G}✓ {t.strftime('%Y-%m-%d %H:%M:%S.%f')}{RST}")
            return t
        except Exception as e:
            print(f"{R}✗ {e}{RST}")
    return None


def synced_now(start_beijing: datetime, start_mono: float) -> datetime:
    return start_beijing + timedelta(seconds=time.monotonic() - start_mono)


# ── Status Check ────────────────────────────────────────────────────
async def check_status(token: str) -> bool:
    dev = gen_device_id()
    headers = {"Cookie": build_cookie(token, dev)}
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=15.0)) as c:
        try:
            r = await c.get(STATUS_URL, headers=headers)
            data = r.json()
        except Exception as e:
            print(f"  {R}✗ Status check failed: {e}{RST}")
            return False

    code = data.get("code")
    if code == 100004:
        print(f"  {BR}✗ Token expired! Get a fresh one.{RST}")
        return False

    info = data.get("data", {})
    is_pass = info.get("is_pass")
    btn = info.get("button_state")
    deadline = info.get("deadline_format", "")

    if is_pass == 1:
        print(f"  {BG}★ ALREADY APPROVED — unlock available until {deadline}{RST}")
        return False
    if is_pass == 4:
        if btn == 1:
            print(f"  {G}✓ Eligible to send unlock request{RST}")
            return True
        if btn == 2:
            print(f"  {Y}⚠ Blocked until {deadline}. Continuing anyway...{RST}")
            return True
        if btn == 3:
            print(f"  {Y}⚠ Account < 30 days old. Continuing anyway...{RST}")
            return True

    print(f"  {R}✗ Unknown status: is_pass={is_pass}, button_state={btn}{RST}")
    print(f"    {json.dumps(data)}")
    return False


# ── Worker ──────────────────────────────────────────────────────────
async def worker(
    wid: int,
    token: str,
    offset_ms: float,
    start_beijing: datetime,
    start_mono: float,
    stop: asyncio.Event,
) -> dict | None:
    dev = gen_device_id()

    cur = synced_now(start_beijing, start_mono)
    tomorrow = cur + timedelta(days=1)
    midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    target = midnight - timedelta(milliseconds=offset_ms)
    deadline = midnight + timedelta(seconds=MAX_RETRY_SECS)

    wait = (target - cur).total_seconds()
    print(f"  {G}W{wid}: offset={offset_ms}ms → {target.strftime('%H:%M:%S.%f')}, wait {wait:.0f}s{RST}")

    # Coarse sleep (wake early to spin-wait)
    if wait > 2:
        await asyncio.sleep(wait - 1)
    # Spin wait for precision
    while synced_now(start_beijing, start_mono) < target:
        await asyncio.sleep(0.0001)

    headers = {
        "Cookie": build_cookie(token, dev),
        "Content-Type": "application/json; charset=utf-8",
        "Accept-Encoding": "gzip, deflate, br",
        "User-Agent": "okhttp/4.12.0",
        "Connection": "keep-alive",
    }
    body = b'{"is_retry":true}'

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=2.0, read=15.0),
        http2=False,
    ) as client:
        attempt = 0
        while synced_now(start_beijing, start_mono) < deadline and not stop.is_set():
            attempt += 1
            fire = synced_now(start_beijing, start_mono)
            try:
                resp = await client.post(APPLY_URL, headers=headers, content=body)
                data = resp.json()
            except Exception as e:
                print(f"  {R}W{wid} #{attempt}: network error: {e}{RST}")
                await asyncio.sleep(0.05)
                continue

            code = data.get("code")
            info = data.get("data", {})
            result = info.get("apply_result")

            if code == 0:
                if result == 1:
                    recv = synced_now(start_beijing, start_mono)
                    print(f"\n  {BG}★ W{wid} APPROVED at {recv.strftime('%H:%M:%S.%f')} (attempt #{attempt}) ★{RST}")
                    stop.set()
                    return {"worker": wid, "approved": True, "data": data}
                if result in (3, 4):
                    reason = "quota reached" if result == 3 else "blocked"
                    dl = info.get("deadline_format", "?")
                    print(f"  {R}W{wid}: {reason} until {dl}{RST}")
                    return {"worker": wid, "approved": False, "data": data}
            elif code == 100001:
                # Rejected — retry immediately
                print(f"  W{wid}: attempt #{attempt} at {fire.strftime('%H:%M:%S.%f')} → rejected, retrying...", end="\r", flush=True)
                await asyncio.sleep(0.01)
                continue
            elif code == 100003:
                print(f"  {Y}W{wid}: possibly approved (100003){RST}")
                return {"worker": wid, "approved": None, "data": data}

            await asyncio.sleep(0.01)

    if stop.is_set():
        return None
    print(f"  {R}W{wid}: timed out after {MAX_RETRY_SECS}s{RST}")
    return {"worker": wid, "approved": False, "data": {}}


# ── Main ────────────────────────────────────────────────────────────
async def main():
    print(f"\n{BY}═══ Xiaomi Bootloader Unlock (AQLR) ═══{RST}\n")

    # Load tokens
    token_path = Path("token.txt")
    if not token_path.exists():
        print(f"  {R}✗ token.txt not found{RST}")
        print(f"\n  Create token.txt with 2 lines:")
        print(f"    Line 1: new_bbs_serviceToken (from Firefox cookies)")
        print(f"    Line 2: popRunToken (from Chrome cookies)")
        print(f"\n  How to get them:")
        print(f"    1. Firefox → https://c.mi.com/global → Login")
        print(f"       F12 → Storage → Cookies → copy 'new_bbs_serviceToken'")
        print(f"    2. Chrome → https://c.mi.com/global → Login")
        print(f"       F12 → Application → Cookies → copy 'popRunToken'")
        sys.exit(1)

    tokens = [l.strip() for l in token_path.read_text().splitlines() if l.strip()]
    if len(tokens) < 2:
        print(f"  {R}✗ Need at least 2 tokens in token.txt{RST}")
        sys.exit(1)

    print(f"  {G}✓ Loaded {len(tokens)} tokens → 4 workers{RST}")

    # Account status
    print(f"\n{BY}── Account Status ──{RST}")
    if not await check_status(tokens[0]):
        sys.exit(1)

    # NTP sync
    print(f"\n{BY}── NTP Sync ──{RST}")
    beijing = sync_ntp()
    if not beijing:
        print(f"  {R}✗ All NTP servers failed{RST}")
        sys.exit(1)
    start_mono = time.monotonic()

    # Countdown info
    tomorrow = beijing + timedelta(days=1)
    midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    secs = (midnight - beijing).total_seconds()
    h, m = int(secs // 3600), int((secs % 3600) // 60)

    print(f"\n{BY}── Countdown ──{RST}")
    print(f"  Target:  {midnight.strftime('%Y-%m-%d %H:%M:%S')} Beijing (UTC+8)")
    print(f"  Wait:    ~{h}h {m}m")
    print(f"  Workers: 4 × offsets {TIME_OFFSETS_MS}ms")
    print(f"\n  {Y}Keep this terminal open!{RST}\n")

    # 4 workers: alternate firefox/chrome tokens
    token_list = [tokens[0], tokens[1], tokens[0], tokens[1]]
    stop = asyncio.Event()

    tasks = [
        worker(i + 1, tok, off, beijing, start_mono, stop)
        for i, (tok, off) in enumerate(zip(token_list, TIME_OFFSETS_MS))
    ]
    results = await asyncio.gather(*tasks)

    # Summary
    print(f"\n\n{BY}═══ Results ═══{RST}\n")
    approved = False
    for r in results:
        if not r:
            continue
        if r.get("approved"):
            approved = True
            print(f"  {BG}W{r['worker']}: APPROVED{RST}")
        elif r.get("approved") is None:
            print(f"  {Y}W{r['worker']}: MAYBE (check status manually){RST}")
        else:
            print(f"  {R}W{r['worker']}: FAILED{RST}")
        print(f"    {json.dumps(r.get('data', {}), indent=2)}")

    if approved:
        print(f"\n  {BG}★★★ UNLOCK REQUEST APPROVED! ★★★{RST}")
        print(f"\n  Next steps on your Xiaomi 15:")
        print(f"    1. Sign out of Mi Account")
        print(f"    2. Restart phone")
        print(f"    3. Sign in to Mi Account")
        print(f"    4. Developer Options → Mi Unlock Status → Link Account")
        print(f"    5. Use Mi Unlock Tool on PC (72h wait)")
    else:
        print(f"\n  {Y}Not approved. Try again tomorrow at midnight Beijing time.{RST}")


if __name__ == "__main__":
    asyncio.run(main())
