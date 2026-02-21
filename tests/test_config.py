"""Tests for config.py: token load/save/validate and setup wizard."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from xiaomi_unlock.config import (
    TokenError,
    Tokens,
    load_tokens,
    save_tokens,
    validate_token,
)


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
