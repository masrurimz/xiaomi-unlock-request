"""Tests for config.py: token load/save/validate and setup wizard."""

from __future__ import annotations

import asyncio as py_asyncio
import contextlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import click
import pytest

import xiaomi_unlock.config as cfg
import xiaomi_unlock.core as core
from xiaomi_unlock.config import (
    BROWSER_CHOICES,
    COOKIE_A,
    COOKIE_B,
    TokenError,
    Tokens,
    _collect_cookie,
    _prompt_browser,
    _verify_tokens,
    load_tokens,
    save_tokens,
    setup_wizard,
    validate_token,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_console(monkeypatch):
    """Replace module console with a fake that captures print calls."""
    class _FakeConsole:
        def __init__(self):
            self.print = MagicMock()

        def status(self, *args, **kwargs):
            return contextlib.nullcontext()

    fc = _FakeConsole()
    monkeypatch.setattr(cfg, "console", fc)
    return fc


@pytest.fixture
def valid_token():
    return "a" * 40


@pytest.fixture
def valid_token2():
    return "b" * 40


def test_validate_token_ok():
    assert validate_token("a" * 20) is True
    assert validate_token("x" * 100) is True


def test_validate_token_too_short():
    assert validate_token("") is False
    assert validate_token("short") is False
    assert validate_token("a" * 19) is False


def test_save_and_load_tokens(tmp_path: Path):
    path = tmp_path / "tokens.json"
    tokens = Tokens(firefox="f" * 40, chrome="c" * 40)
    save_tokens(tokens, path)

    assert path.exists()
    # File should have restrictive permissions
    mode = path.stat().st_mode & 0o777
    assert mode == stat.S_IRUSR | stat.S_IWUSR

    loaded = load_tokens(path)
    assert loaded.firefox == tokens.firefox
    assert loaded.chrome == tokens.chrome


def test_load_tokens_json_format(tmp_path: Path):
    path = tmp_path / "tokens.json"
    data = {"firefox": "f" * 40, "chrome": "c" * 40}
    path.write_text(json.dumps(data))

    tokens = load_tokens(path)
    assert tokens.firefox == data["firefox"]
    assert tokens.chrome == data["chrome"]


def test_load_tokens_legacy_txt(tmp_path: Path):
    path = tmp_path / "token.txt"
    firefox = "f" * 40
    chrome = "c" * 40
    path.write_text(f"{firefox}\n{chrome}\n")

    tokens = load_tokens(path)
    assert tokens.firefox == firefox
    assert tokens.chrome == chrome


def test_load_tokens_missing_raises(tmp_path: Path):
    with pytest.raises(TokenError, match="No tokens found"):
        load_tokens(tmp_path / "nonexistent.json")


def test_load_tokens_invalid_json(tmp_path: Path):
    path = tmp_path / "tokens.json"
    path.write_text("not valid json{")
    with pytest.raises(TokenError, match="Cannot read"):
        load_tokens(path)


def test_load_tokens_short_values(tmp_path: Path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"firefox": "short", "chrome": "short"}))
    with pytest.raises(TokenError, match="invalid"):
        load_tokens(path)


def test_load_tokens_legacy_needs_two_lines(tmp_path: Path):
    path = tmp_path / "token.txt"
    path.write_text("onlyoneline\n")
    with pytest.raises(TokenError, match="2 lines"):
        load_tokens(path)


# ── _prompt_browser ───────────────────────────────────────────────────────────

class TestPromptBrowser:
    def test_returns_correct_key_no_exclusion(self, monkeypatch, fake_console):
        """Choosing '2' with no exclusion returns 'chrome'."""
        ask = MagicMock(return_value="2")
        monkeypatch.setattr(cfg.Prompt, "ask", ask)

        result = _prompt_browser("Pick browser")

        assert result == "chrome"
        assert ask.call_args.kwargs["choices"] == ["1", "2", "3", "4"]
        assert ask.call_args.kwargs["show_choices"] is False

    def test_all_four_indices_available_when_no_exclusion(self, monkeypatch, fake_console):
        """All 4 browser choices offered when no exclusion."""
        ask = MagicMock(return_value="4")
        monkeypatch.setattr(cfg.Prompt, "ask", ask)

        result = _prompt_browser("Pick browser")

        assert result == "safari"
        assert ask.call_args.kwargs["choices"] == ["1", "2", "3", "4"]

    def test_excluded_browser_removed_from_choices(self, monkeypatch, fake_console):
        """Excluded browser index not in Prompt choices; choosing '3' maps to 'edge'."""
        ask = MagicMock(return_value="3")
        monkeypatch.setattr(cfg.Prompt, "ask", ask)

        result = _prompt_browser("Pick browser", exclude_key="chrome")

        assert result == "edge"
        assert "2" not in ask.call_args.kwargs["choices"]
        assert ask.call_args.kwargs["choices"] == ["1", "3", "4"]

    def test_excluded_browser_printed_dim_with_already_used(self, monkeypatch, fake_console):
        """Excluded browser is shown grayed-out with 'already used' label."""
        ask = MagicMock(return_value="1")
        monkeypatch.setattr(cfg.Prompt, "ask", ask)

        _prompt_browser("Pick browser", exclude_key="firefox")

        all_printed = " ".join(str(c.args[0]) for c in fake_console.print.call_args_list)
        assert "already used" in all_printed
        assert "[dim]" in all_printed


# ── _collect_cookie ───────────────────────────────────────────────────────────

class TestCollectCookie:
    def _patch_browser(self, monkeypatch, browser="firefox"):
        monkeypatch.setattr(cfg, "_prompt_browser", lambda *a, **k: browser)

    def test_valid_token_on_first_try(self, monkeypatch, fake_console, valid_token):
        self._patch_browser(monkeypatch)
        ask = MagicMock(return_value=valid_token)
        monkeypatch.setattr(cfg.Prompt, "ask", ask)

        browser_key, value = _collect_cookie(COOKIE_A)

        assert browser_key == "firefox"
        assert value == valid_token
        assert ask.call_count == 1

    def test_strips_whitespace_from_token(self, monkeypatch, fake_console, valid_token):
        self._patch_browser(monkeypatch)
        ask = MagicMock(return_value=f"  {valid_token}  ")
        monkeypatch.setattr(cfg.Prompt, "ask", ask)

        _, value = _collect_cookie(COOKIE_A)

        assert value == valid_token

    def test_retries_then_succeeds(self, monkeypatch, fake_console, valid_token):
        self._patch_browser(monkeypatch)
        ask = MagicMock(side_effect=["short", valid_token])
        monkeypatch.setattr(cfg.Prompt, "ask", ask)

        _, value = _collect_cookie(COOKIE_A)

        assert value == valid_token
        assert ask.call_count == 2

    def test_warning_shows_attempts_remaining(self, monkeypatch, fake_console, valid_token):
        self._patch_browser(monkeypatch)
        ask = MagicMock(side_effect=["short", valid_token])
        monkeypatch.setattr(cfg.Prompt, "ask", ask)

        _collect_cookie(COOKIE_A)

        all_printed = " ".join(str(c.args[0]) for c in fake_console.print.call_args_list)
        assert "2 attempts left" in all_printed

    def test_three_invalid_raises_click_exception(self, monkeypatch, fake_console):
        self._patch_browser(monkeypatch)
        ask = MagicMock(side_effect=["short", "short", "short"])
        monkeypatch.setattr(cfg.Prompt, "ask", ask)

        with pytest.raises(click.ClickException, match="Too many invalid attempts"):
            _collect_cookie(COOKIE_A)

        assert ask.call_count == 3

    def test_keyboard_interrupt_raises_click_abort(self, monkeypatch, fake_console):
        self._patch_browser(monkeypatch)
        monkeypatch.setattr(cfg.Prompt, "ask", MagicMock(side_effect=KeyboardInterrupt))

        with pytest.raises(click.Abort):
            _collect_cookie(COOKIE_A)

    def test_click_abort_exception_raises_click_abort(self, monkeypatch, fake_console):
        self._patch_browser(monkeypatch)
        monkeypatch.setattr(cfg.Prompt, "ask", MagicMock(side_effect=click.exceptions.Abort))

        with pytest.raises(click.Abort):
            _collect_cookie(COOKIE_A)

    def test_passes_exclude_browser_to_prompt_browser(self, monkeypatch, fake_console, valid_token):
        calls = []

        def fake_prompt(label, exclude_key=None):
            calls.append(exclude_key)
            return "chrome"

        monkeypatch.setattr(cfg, "_prompt_browser", fake_prompt)
        monkeypatch.setattr(cfg.Prompt, "ask", MagicMock(return_value=valid_token))

        _collect_cookie(COOKIE_B, exclude_browser="firefox")

        assert calls[0] == "firefox"

    def test_cookie_b_label_used_for_non_cookie_a(self, monkeypatch, fake_console, valid_token):
        self._patch_browser(monkeypatch, "chrome")
        monkeypatch.setattr(cfg.Prompt, "ask", MagicMock(return_value=valid_token))

        _collect_cookie(COOKIE_B)

        all_printed = " ".join(str(c.args[0]) for c in fake_console.print.call_args_list)
        assert "Cookie B" in all_printed


# ── _verify_tokens ────────────────────────────────────────────────────────────

class TestVerifyTokens:
    def _make_result(self, message, eligible):
        return SimpleNamespace(message=message, eligible=eligible)

    def _asyncio_spy(self, monkeypatch):
        """Wrap asyncio.run to count calls while still executing coroutines."""
        calls = {"n": 0}
        real_run = py_asyncio.run

        def run_spy(coro):
            calls["n"] += 1
            return real_run(coro)

        monkeypatch.setattr(cfg.asyncio, "run", run_spy)
        return calls

    def test_eligible_token_prints_green_check(self, monkeypatch, fake_console, valid_token, valid_token2):
        result = self._make_result("Eligible to send unlock request", eligible=True)
        monkeypatch.setattr(core, "check_status", AsyncMock(return_value=result))
        self._asyncio_spy(monkeypatch)

        _verify_tokens(Tokens(firefox=valid_token, chrome=valid_token2))

        all_printed = " ".join(str(c.args[0]) for c in fake_console.print.call_args_list)
        assert f"✓ {COOKIE_A}" in all_printed

    def test_expired_token_prints_red_cross_and_hint(self, monkeypatch, fake_console, valid_token, valid_token2):
        result = self._make_result("Token expired — get a fresh one.", eligible=False)
        monkeypatch.setattr(core, "check_status", AsyncMock(return_value=result))
        self._asyncio_spy(monkeypatch)

        _verify_tokens(Tokens(firefox=valid_token, chrome=valid_token2))

        all_printed = " ".join(str(c.args[0]) for c in fake_console.print.call_args_list)
        assert f"✗ {COOKIE_A}" in all_printed
        assert "Re-run" in all_printed or "mi-unlock setup" in all_printed

    def test_network_error_prints_yellow_warning_not_crash(self, monkeypatch, fake_console, valid_token, valid_token2):
        result = self._make_result("Network error: timeout", eligible=False)
        monkeypatch.setattr(core, "check_status", AsyncMock(return_value=result))
        self._asyncio_spy(monkeypatch)

        # Must not raise
        _verify_tokens(Tokens(firefox=valid_token, chrome=valid_token2))

        all_printed = " ".join(str(c.args[0]) for c in fake_console.print.call_args_list)
        assert f"? {COOKIE_A}" in all_printed or "could not reach" in all_printed
        # Must NOT show green ✓ for a network error
        assert f"✓ {COOKIE_A}" not in all_printed

    def test_asyncio_run_called_exactly_once(self, monkeypatch, fake_console, valid_token, valid_token2):
        result = self._make_result("Eligible to send unlock request", eligible=True)
        monkeypatch.setattr(core, "check_status", AsyncMock(return_value=result))
        spy = self._asyncio_spy(monkeypatch)

        _verify_tokens(Tokens(firefox=valid_token, chrome=valid_token2))

        assert spy["n"] == 1

    def test_check_status_called_with_cookie_a_token(self, monkeypatch, fake_console, valid_token, valid_token2):
        result = self._make_result("Eligible to send unlock request", eligible=True)
        mock_check = AsyncMock(return_value=result)
        monkeypatch.setattr(core, "check_status", mock_check)
        self._asyncio_spy(monkeypatch)

        _verify_tokens(Tokens(firefox=valid_token, chrome=valid_token2))

        mock_check.assert_awaited_once_with(valid_token)

    def test_cookie_b_always_shown_as_not_verifiable(self, monkeypatch, fake_console, valid_token, valid_token2):
        result = self._make_result("Eligible to send unlock request", eligible=True)
        monkeypatch.setattr(core, "check_status", AsyncMock(return_value=result))
        self._asyncio_spy(monkeypatch)

        _verify_tokens(Tokens(firefox=valid_token, chrome=valid_token2))

        all_printed = " ".join(str(c.args[0]) for c in fake_console.print.call_args_list)
        assert COOKIE_B in all_printed
        assert "not verifiable" in all_printed or "apply-time" in all_printed

    def test_expiry_message_shown_when_present(self, monkeypatch, fake_console, valid_token, valid_token2):
        result = self._make_result("Already approved — unlock available until 2026-03-01", eligible=False)
        monkeypatch.setattr(core, "check_status", AsyncMock(return_value=result))
        self._asyncio_spy(monkeypatch)

        _verify_tokens(Tokens(firefox=valid_token, chrome=valid_token2))

        all_printed = " ".join(str(c.args[0]) for c in fake_console.print.call_args_list)
        assert "2026-03-01" in all_printed


# ── setup_wizard (integration) ────────────────────────────────────────────────

class TestSetupWizard:
    def test_saves_tokens_calls_verify_returns_tokens(self, monkeypatch, tmp_path, fake_console, valid_token, valid_token2):
        token_file = tmp_path / "tokens.json"

        collect_calls = []
        def fake_collect(cookie_name, exclude_browser=None):
            collect_calls.append((cookie_name, exclude_browser))
            if cookie_name == COOKIE_A:
                return ("firefox", valid_token)
            return ("chrome", valid_token2)

        verify_calls = []
        def fake_verify(tokens):
            verify_calls.append(tokens)

        monkeypatch.setattr(cfg, "_collect_cookie", fake_collect)
        monkeypatch.setattr(cfg, "_verify_tokens", fake_verify)

        result = setup_wizard(token_file)

        assert result == Tokens(firefox=valid_token, chrome=valid_token2)
        assert token_file.exists()
        saved = json.loads(token_file.read_text())
        assert saved == {"firefox": valid_token, "chrome": valid_token2}
        assert len(verify_calls) == 1
        assert verify_calls[0] == result

    def test_collect_cookie_called_twice_with_correct_exclusion(self, monkeypatch, tmp_path, fake_console, valid_token, valid_token2):
        collect_calls = []
        def fake_collect(cookie_name, exclude_browser=None):
            collect_calls.append((cookie_name, exclude_browser))
            if cookie_name == COOKIE_A:
                return ("firefox", valid_token)
            return ("chrome", valid_token2)

        monkeypatch.setattr(cfg, "_collect_cookie", fake_collect)
        monkeypatch.setattr(cfg, "_verify_tokens", lambda t: None)

        setup_wizard(tmp_path / "tokens.json")

        assert collect_calls[0] == (COOKIE_A, None)
        assert collect_calls[1][0] == COOKIE_B
        assert collect_calls[1][1] == "firefox"  # exclude browser from slot 1
