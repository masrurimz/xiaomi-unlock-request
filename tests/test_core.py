"""Tests for core.py: device_id, cookie, timing, status/apply parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from xiaomi_unlock.core import (
    BEIJING_TZ,
    build_cookie,
    calc_target_time,
    gen_device_id,
    parse_apply_response,
    parse_status_response,
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
