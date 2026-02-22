"""Tests for core.py: device_id, cookie, timing, status/apply parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from xiaomi_unlock.core import (
    BEIJING_TZ,
    MAX_RETRY_SECS,
    build_cookie,
    calc_target_time,
    check_status,
    gen_device_id,
    parse_apply_response,
    parse_status_response,
    sync_ntp,
)


# ── Fake clock ────────────────────────────────────────────────────────
class FakeClock:
    def __init__(self, current: datetime):
        self._current = current

    def monotonic(self) -> float:
        return 0.0

    def synced_now(self) -> datetime:
        return self._current

    async def sleep(self, seconds: float) -> None:
        pass  # No actual sleep in tests


def make_clock(hour: int = 23, minute: int = 50) -> FakeClock:
    """Create a fake clock set to given time in Beijing timezone."""
    dt = datetime(2026, 2, 22, hour, minute, 0, tzinfo=BEIJING_TZ)
    return FakeClock(dt)


# ── gen_device_id ─────────────────────────────────────────────────────
def test_gen_device_id_format():
    dev_id = gen_device_id()
    assert len(dev_id) == 40  # SHA1 hex = 40 chars
    assert dev_id == dev_id.upper()
    assert all(c in "0123456789ABCDEF" for c in dev_id)


def test_gen_device_id_unique():
    ids = {gen_device_id() for _ in range(10)}
    assert len(ids) == 10  # all unique


# ── build_cookie ──────────────────────────────────────────────────────
def test_build_cookie_format():
    cookie = build_cookie("mytoken", "DEVID123")
    assert "new_bbs_serviceToken=mytoken" in cookie
    assert "versionCode=500411" in cookie
    assert "versionName=5.4.11" in cookie
    assert "deviceId=DEVID123" in cookie


# ── calc_target_time ──────────────────────────────────────────────────
def test_calc_target_time_offset():
    clock = make_clock(hour=23, minute=50)
    target, deadline = calc_target_time(clock, offset_ms=1000)

    # Target should be 1 second before midnight
    assert target.hour == 23
    assert target.minute == 59
    assert target.second == 59
    assert target.tzinfo == BEIJING_TZ


def test_calc_target_time_deadline():
    from xiaomi_unlock.core import MAX_RETRY_SECS

    clock = make_clock(hour=23, minute=50)
    _, deadline = calc_target_time(clock, offset_ms=0)

    # Deadline = midnight + 30s
    assert deadline.hour == 0
    assert deadline.second == MAX_RETRY_SECS


def test_calc_target_time_just_after_midnight():
    """Workers started seconds after midnight must target THIS midnight, not tomorrow's.

    Regression for: countdown ends at 00:00:00, workers call calc_target_time at
    00:00:02 — previously returned tomorrow's midnight (24h away), causing all
    workers to sit in 'waiting' forever.
    """
    from xiaomi_unlock.core import MAX_RETRY_SECS

    # Simulate: workers start 2 seconds after midnight
    dt = datetime(2026, 2, 23, 0, 0, 2, tzinfo=BEIJING_TZ)
    clock = FakeClock(dt)
    target, deadline = calc_target_time(clock, offset_ms=0)

    # Must target Feb 23 00:00:00 — NOT Feb 24 00:00:00
    assert target.day == 23
    assert target.hour == 0
    assert target.minute == 0
    assert target.second == 0
    # Deadline is only 30s after midnight — workers should fire immediately
    assert (deadline - target).total_seconds() == MAX_RETRY_SECS


def test_calc_target_time_well_after_midnight_targets_tomorrow():
    """During the day (e.g. 12:00), target should be next midnight."""
    dt = datetime(2026, 2, 23, 12, 0, 0, tzinfo=BEIJING_TZ)
    clock = FakeClock(dt)
    target, _ = calc_target_time(clock, offset_ms=0)

    # Next midnight = Feb 24 00:00:00
    assert target.day == 24
    assert target.hour == 0


# ── parse_status_response ─────────────────────────────────────────────
def test_parse_status_token_expired():
    result = parse_status_response({"code": 100004})
    assert result.eligible is False
    assert "expired" in result.message.lower()


def test_parse_status_already_approved():
    data = {"code": 0, "data": {"is_pass": 1, "deadline_format": "2026-03-01"}}
    result = parse_status_response(data)
    assert result.eligible is False
    assert "approved" in result.message.lower()


def test_parse_status_eligible():
    data = {"code": 0, "data": {"is_pass": 4, "button_state": 1}}
    result = parse_status_response(data)
    assert result.eligible is True
    assert "eligible" in result.message.lower()


def test_parse_status_blocked_eligible():
    data = {"code": 0, "data": {"is_pass": 4, "button_state": 2, "deadline_format": "tomorrow"}}
    result = parse_status_response(data)
    assert result.eligible is True  # try anyway


def test_parse_status_unknown():
    data = {"code": 0, "data": {"is_pass": 99, "button_state": 0}}
    result = parse_status_response(data)
    assert result.eligible is False
    assert "unknown" in result.message.lower()


# ── parse_apply_response ──────────────────────────────────────────────
def test_parse_apply_approved():
    data = {"code": 0, "data": {"apply_result": 1}}
    msg, approved = parse_apply_response(data)
    assert approved is True


def test_parse_apply_quota():
    data = {"code": 0, "data": {"apply_result": 3, "deadline_format": "tomorrow"}}
    msg, approved = parse_apply_response(data)
    assert approved is False
    assert "quota" in msg.lower()


def test_parse_apply_blocked():
    data = {"code": 0, "data": {"apply_result": 4, "deadline_format": "tomorrow"}}
    msg, approved = parse_apply_response(data)
    assert approved is False


def test_parse_apply_rejected_raises():
    data = {"code": 100001}
    with pytest.raises(ValueError, match="rejected"):
        parse_apply_response(data)


def test_parse_apply_maybe():
    data = {"code": 100003}
    msg, approved = parse_apply_response(data)
    assert approved is None


# ── [448] parse_apply_response: data=None / missing ───────────────────
def test_parse_apply_data_none():
    """data=None must not crash; approved=False, code=0 falls through."""
    data: dict = {"code": 0, "data": None}
    msg, approved = parse_apply_response(data)
    # apply_result is None → falls through to "Unexpected" branch
    assert approved is False


def test_parse_apply_data_missing():
    """Missing 'data' key must not crash."""
    data: dict = {"code": 0}
    msg, approved = parse_apply_response(data)
    assert approved is False


def test_parse_apply_unexpected_code():
    """Unknown code must be reflected in message, approved=False."""
    data: dict = {"code": 123456}
    msg, approved = parse_apply_response(data)
    assert approved is False
    assert "123456" in msg


def test_parse_apply_missing_code():
    """Missing 'code' key (None) with valid apply_result→approved=False (no code==0 branch)."""
    data: dict = {"data": {"apply_result": 1}}
    msg, approved = parse_apply_response(data)
    # code is None → not 0/100001/100003 → "Unexpected" branch → approved=False
    assert approved is False


# ── [iy6] parse_status_response: data=None / missing ─────────────────
def test_parse_status_data_none():
    """data=None must not crash; eligible=False."""
    data: dict = {"code": 0, "data": None}
    result = parse_status_response(data)
    assert result.eligible is False


def test_parse_status_data_missing():
    """Missing 'data' key must not crash."""
    data: dict = {"code": 0}
    result = parse_status_response(data)
    assert result.eligible is False
    assert "unknown" in result.message.lower()


def test_parse_status_no_deadline_format():
    """is_pass=1 with no deadline_format must not crash."""
    data: dict = {"code": 0, "data": {"is_pass": 1}}
    result = parse_status_response(data)
    assert result.eligible is False


# ── [e5l] Unexpected codes matrix ─────────────────────────────────────
def test_parse_status_btn3_30_days():
    """button_state=3 → eligible, message mentions 30 days."""
    data: dict = {"code": 0, "data": {"is_pass": 4, "button_state": 3}}
    result = parse_status_response(data)
    assert result.eligible is True
    assert "30 days" in result.message


def test_parse_status_btn99_unknown():
    """button_state=99 with is_pass=4 → eligible=False, 'Unknown' in message."""
    data: dict = {"code": 0, "data": {"is_pass": 4, "button_state": 99}}
    result = parse_status_response(data)
    assert result.eligible is False
    assert "unknown" in result.message.lower()


def test_parse_status_is_pass2():
    """is_pass=2 → eligible=False."""
    data: dict = {"code": 0, "data": {"is_pass": 2, "button_state": 1}}
    result = parse_status_response(data)
    assert result.eligible is False


# ── [7pm] calc_target_time boundaries ────────────────────────────────
def test_calc_target_time_exactly_midnight():
    """At 00:00:00.000000 → target is today's midnight, deadline = midnight+30s."""
    dt = datetime(2026, 2, 23, 0, 0, 0, tzinfo=BEIJING_TZ)
    clock = FakeClock(dt)
    target, deadline = calc_target_time(clock, offset_ms=0)
    assert target == dt  # today's midnight
    assert (deadline - target).total_seconds() == MAX_RETRY_SECS


def test_calc_target_time_at_30s_targets_tomorrow():
    """At exactly 00:00:30 (== MAX_RETRY_SECS) → target is TOMORROW's midnight."""
    dt = datetime(2026, 2, 23, 0, 0, 30, tzinfo=BEIJING_TZ)
    clock = FakeClock(dt)
    target, _ = calc_target_time(clock, offset_ms=0)
    # seconds_since_midnight == 30 is NOT < 30, so tomorrow
    assert target.day == 24


def test_calc_target_time_late_night_with_offset():
    """23:59:59.5 with offset_ms=1000 → target is 23:59:58.5, which is in the past."""
    dt = datetime(2026, 2, 22, 23, 59, 59, 500000, tzinfo=BEIJING_TZ)
    clock = FakeClock(dt)
    target, _ = calc_target_time(clock, offset_ms=1000)
    # target = midnight - 1000ms = 23:59:59.000 of same day → before 23:59:59.5
    assert target < dt


# ── [mvz] check_status: data=None payload ────────────────────────────
@pytest.mark.asyncio
async def test_check_status_data_none_no_crash():
    """check_status with data=None in response must not raise."""
    import httpx
    import respx

    with respx.mock(base_url="https://sgp-api.buy.mi.com") as m:
        m.get("/bbs/api/global/user/bl-switch/state").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": None})
        )
        result = await check_status("token_abc")

    assert result.eligible is False


# ── [m1z] sync_ntp failures ───────────────────────────────────────────
def test_sync_ntp_first_fails_second_succeeds():
    """First server raises, second returns valid time → success=True."""
    mock_resp = MagicMock()
    mock_resp.tx_time = datetime(2026, 2, 22, 12, 0, 0, tzinfo=timezone.utc).timestamp()

    call_count = 0

    def fake_request(server: str, version: int) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("timeout")
        return mock_resp

    with patch("ntplib.NTPClient.request", side_effect=fake_request):
        result = sync_ntp(["bad.server", "good.server"])

    assert result.success is True
    assert result.server == "good.server"


def test_sync_ntp_all_fail():
    """All servers raise → success=False, error non-empty."""
    with patch("ntplib.NTPClient.request", side_effect=Exception("connection refused")):
        result = sync_ntp(["s1", "s2", "s3"])

    assert result.success is False
    assert result.error != ""


def test_sync_ntp_empty_servers():
    """Empty server list → success=False, error='No NTP servers configured'."""
    result = sync_ntp([])
    assert result.success is False
    assert result.error == "No NTP servers configured"
