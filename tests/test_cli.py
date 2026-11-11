"""Offset configuration and countdown/worker orchestration regressions; no network."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

import xiaomi_unlock.cli as cli_module
from xiaomi_unlock.core import BEIJING_TZ, ApplyResult, NtpResult, StatusResult


@pytest.mark.parametrize("value", ["", "100,", "one", "nan", "inf", "-30000", "86400000"])
def test_invalid_offsets_fail_before_reading_tokens(monkeypatch, value):
    def unexpected_load(*args):
        pytest.fail("Invalid offsets must be rejected before reading credentials")

    monkeypatch.setattr(cli_module, "load_tokens", unexpected_load)
    result = CliRunner().invoke(cli_module.cli, ["run", f"--offsets-ms={value}"])
    assert result.exit_code == 2
    assert "Invalid value for '--offsets-ms'" in result.output


@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        ([], {}, [1400, 900, 400, 100]),
        (["--offsets-ms=500"], {}, [500]),
        (["--offsets-ms=1400,900,400,100,0,-500"], {}, [1400, 900, 400, 100, 0, -500]),
        (["--offsets-ms=-500"], {}, [-500]),
        ([], {"MI_UNLOCK_OFFSETS_MS": "1000,500"}, [1000, 500]),
        (["--offsets-ms=500"], {"MI_UNLOCK_OFFSETS_MS": "1000,100"}, [500]),
    ],
)
def test_cli_passes_offsets_and_shared_midnight(monkeypatch, args, env, expected):
    monkeypatch.delenv("MI_UNLOCK_OFFSETS_MS", raising=False)
    monkeypatch.setattr(cli_module, "load_tokens", lambda _: SimpleNamespace(firefox="fake-ff", chrome="fake-ch"))
    monkeypatch.setattr(cli_module, "check_status", AsyncMock(return_value=StatusResult(True, "Eligible")))
    beijing = datetime(2026, 2, 21, 23, 50, tzinfo=BEIJING_TZ)
    monkeypatch.setattr(cli_module, "sync_ntp", lambda: NtpResult(True, beijing, "fake"))
    scheduled = AsyncMock(return_value=[ApplyResult(1, True, 1, "Approved")])
    monkeypatch.setattr(cli_module, "run_scheduled", scheduled)

    result = CliRunner().invoke(cli_module.cli, ["run", "--plain", *args], env=env)
    assert result.exit_code == 0, result.output
    call = scheduled.call_args
    assert call.args[2] == datetime(2026, 2, 22, tzinfo=BEIJING_TZ)
    assert call.args[3] == expected
    assert f"Workers: {len(expected)}" in result.output
    assert "Positive = before midnight; negative = after midnight" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_workers", [False, True])
async def test_countdown_never_blocks_workers_and_is_cleaned_up(monkeypatch, fail_workers):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    midnight = datetime(2026, 2, 22, tzinfo=BEIJING_TZ)
    clock = SimpleNamespace(synced_now=lambda: datetime(2026, 2, 21, 23, 50, tzinfo=BEIJING_TZ))
    results = [ApplyResult(1, True, 1, "Approved")]

    async def countdown(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def workers(*args, **kwargs):
        await started.wait()
        assert kwargs["midnight"] == midnight
        if fail_workers:
            raise RuntimeError("test failure")
        return results

    monkeypatch.setattr(cli_module, "countdown_display", countdown)
    monkeypatch.setattr(cli_module, "run_workers", workers)
    run = cli_module.run_scheduled(("fake-ff", "fake-ch"), clock, midnight, [500])
    if fail_workers:
        with pytest.raises(RuntimeError, match="test failure"):
            await asyncio.wait_for(run, timeout=1)
    else:
        assert await asyncio.wait_for(run, timeout=1) == results
    assert cancelled.is_set()
