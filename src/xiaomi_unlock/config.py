"""Token management: load, save, validate, and interactive setup wizard."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import NamedTuple

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()

DEFAULT_TOKEN_FILE = Path("tokens.json")
LEGACY_TOKEN_FILE = Path("token.txt")


class Tokens(NamedTuple):
    firefox: str   # new_bbs_serviceToken
    chrome: str    # popRunToken


class TokenError(Exception):
    pass


def validate_token(token: str) -> bool:
    """Return True if token looks plausibly valid."""
    return bool(token) and len(token) >= 20


def load_tokens(token_file: Path | None = None) -> Tokens:
    """Load tokens from tokens.json (preferred) or token.txt (legacy).

    Raises TokenError if tokens cannot be loaded or are invalid.
    """
    path = token_file or DEFAULT_TOKEN_FILE

    # New format: tokens.json
    if path.exists() and path.suffix != ".txt":
        try:
            data = json.loads(path.read_text())
            firefox = data.get("firefox", "").strip()
            chrome = data.get("chrome", "").strip()
        except (json.JSONDecodeError, OSError) as e:
            raise TokenError(f"Cannot read {path}: {e}") from e
        if not validate_token(firefox) or not validate_token(chrome):
            raise TokenError(
                f"Tokens in {path} look invalid. Run `mi-unlock setup` to reconfigure."
            )
        return Tokens(firefox=firefox, chrome=chrome)

    # Legacy format: token.txt (2 lines)
    legacy = path if path.suffix == ".txt" else (LEGACY_TOKEN_FILE if token_file is None else token_file)
    if legacy.suffix == ".txt" and legacy.exists():
        lines = [l.strip() for l in legacy.read_text().splitlines() if l.strip()]
        if len(lines) < 2:
            raise TokenError(
                f"{legacy} must have 2 lines (Firefox token, Chrome token)."
            )
        return Tokens(firefox=lines[0], chrome=lines[1])

    raise TokenError(
        "No tokens found. Run `mi-unlock setup` to configure your tokens."
    )


def save_tokens(tokens: Tokens, path: Path = DEFAULT_TOKEN_FILE) -> None:
    """Write tokens to tokens.json with restrictive permissions (600)."""
    data = {"firefox": tokens.firefox, "chrome": tokens.chrome}
    path.write_text(json.dumps(data, indent=2))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def setup_wizard(token_file: Path = DEFAULT_TOKEN_FILE) -> Tokens:
    """Interactive Rich prompt wizard to collect Firefox + Chrome tokens."""
    console.print(
        Panel.fit(
            "[bold cyan]Xiaomi Bootloader Unlock — Token Setup[/bold cyan]\n\n"
            "You need [bold]2 session tokens[/bold] from [bold]2 different browsers[/bold],\n"
            "both logged into the [bold]same Mi Account[/bold] on "
            "[link=https://c.mi.com/global]c.mi.com/global[/link].",
            border_style="cyan",
        )
    )

    console.print(
        "\n[bold yellow]Token 1 — Firefox[/bold yellow] ([cyan]new_bbs_serviceToken[/cyan])\n"
        "  1. Open Firefox → [link=https://c.mi.com/global]https://c.mi.com/global[/link] → Login\n"
        "  2. Press [bold]F12[/bold] → [bold]Storage[/bold] → [bold]Cookies[/bold] → c.mi.com\n"
        "  3. Find [cyan]new_bbs_serviceToken[/cyan] → copy the [bold]Value[/bold]"
    )
    firefox = Prompt.ask("\n  [bold]Paste Firefox token[/bold]").strip()
    if not validate_token(firefox):
        console.print("[red]Token looks too short — double-check and try again.[/red]")
        raise click.Abort()

    console.print(
        "\n[bold yellow]Token 2 — Chrome[/bold yellow] ([cyan]popRunToken[/cyan])\n"
        "  1. Open Chrome → [link=https://c.mi.com/global]https://c.mi.com/global[/link] → Login\n"
        "  2. Press [bold]F12[/bold] → [bold]Application[/bold] → [bold]Cookies[/bold] → https://c.mi.com\n"
        "  3. Find [cyan]popRunToken[/cyan] → copy the [bold]Value[/bold]"
    )
    chrome = Prompt.ask("\n  [bold]Paste Chrome token[/bold]").strip()
    if not validate_token(chrome):
        console.print("[red]Token looks too short — double-check and try again.[/red]")
        raise click.Abort()

    tokens = Tokens(firefox=firefox, chrome=chrome)
    save_tokens(tokens, token_file)
    console.print(f"\n[green]✓ Tokens saved to [bold]{token_file}[/bold][/green]")
    console.print(
        "[yellow]⚠ Tokens expire — get fresh ones each night before running.[/yellow]"
    )
    return tokens
