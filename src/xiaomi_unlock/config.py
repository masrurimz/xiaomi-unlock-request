"""Token management: load, save, validate, and interactive setup wizard."""

from __future__ import annotations

import asyncio
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


# Browser DevTools path info — cookie name is shown separately per cookie type.
BROWSER_UI = {
    "firefox": {
        "label": "Firefox",
        "devtools": "[bold]F12[/bold] → [bold]Storage[/bold] → [bold]Cookies[/bold] → c.mi.com",
        "open": "Open [bold]Firefox[/bold]",
    },
    "chrome": {
        "label": "Chrome",
        "devtools": "[bold]F12[/bold] → [bold]Application[/bold] → [bold]Cookies[/bold] → https://c.mi.com",
        "open": "Open [bold]Chrome[/bold]",
    },
    "edge": {
        "label": "Edge",
        "devtools": "[bold]F12[/bold] → [bold]Application[/bold] → [bold]Cookies[/bold] → https://c.mi.com",
        "open": "Open [bold]Edge[/bold]",
    },
    "safari": {
        "label": "Safari",
        "devtools": "[bold]⌥⌘I[/bold] → [bold]Storage[/bold] → [bold]Cookies[/bold] → c.mi.com",
        "open": "Open [bold]Safari[/bold] (enable DevTools: Settings → Advanced → Show Develop menu)",
    },
}

BROWSER_CHOICES = ["firefox", "chrome", "edge", "safari"]

# The two cookies the AQLR method requires.
COOKIE_A = "new_bbs_serviceToken"
COOKIE_B = "popRunToken"


def _prompt_browser(label: str, exclude_key: str | None = None) -> str:
    """Ask which browser to use; excluded browser shown grayed-out so numbering stays consistent."""
    console.print(f"\n[bold yellow]{label}[/bold yellow]")
    valid_indices: list[int] = []
    for i, key in enumerate(BROWSER_CHOICES, 1):
        if key == exclude_key:
            console.print(
                f"  [dim]{i}. {BROWSER_UI[key]['label']} (already used — pick a different browser)[/dim]"
            )
        else:
            console.print(f"  [bold]{i}[/bold]. {BROWSER_UI[key]['label']}")
            valid_indices.append(i)
    choice = Prompt.ask(
        "\n  [bold]Enter number[/bold]",
        choices=[str(i) for i in valid_indices],
        show_choices=False,
    )
    return BROWSER_CHOICES[int(choice) - 1]


def _collect_cookie(cookie_name: str, exclude_browser: str | None = None) -> tuple[str, str]:
    """Show browser selector + DevTools instructions for one cookie. Returns (browser_key, value)."""
    slot_label = "Cookie A" if cookie_name == COOKIE_A else "Cookie B"
    browser_key = _prompt_browser(
        label=f"{slot_label} ([cyan]{cookie_name}[/cyan]) — which browser will you use?",
        exclude_key=exclude_browser,
    )
    ui = BROWSER_UI[browser_key]
    console.print(
        f"\n[bold yellow]{slot_label} — {ui['label']}[/bold yellow]\n"
        f"  1. {ui['open']} → [link=https://c.mi.com/global]https://c.mi.com/global[/link] → Login\n"
        f"  2. Press {ui['devtools']}\n"
        f"  3. Find [cyan]{cookie_name}[/cyan] → copy the [bold]Value[/bold]"
    )
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            value = Prompt.ask(f"\n  [bold]Paste [cyan]{cookie_name}[/cyan] value[/bold]").strip()
        except (KeyboardInterrupt, click.exceptions.Abort):
            raise click.Abort()
        if validate_token(value):
            return browser_key, value
        remaining = max_attempts - attempt
        if remaining:
            console.print(
                f"[red]Value looks too short — make sure you copied the [bold]Value[/bold] "
                f"column for [bold]{cookie_name}[/bold], not the cookie name. "
                f"({remaining} attempt{'s' if remaining != 1 else ''} left)[/red]"
            )
    raise click.ClickException(
        f"Too many invalid attempts for {cookie_name}. Run `mi-unlock setup` to try again."
    )


def setup_wizard(token_file: Path = DEFAULT_TOKEN_FILE) -> Tokens:
    """Interactive Rich prompt wizard — collects 2 named cookies from 2 different browsers."""
    console.print(
        Panel.fit(
            "[bold cyan]Xiaomi Bootloader Unlock — Token Setup[/bold cyan]\n\n"
            "You need [bold]2 cookie values[/bold] from [bold]c.mi.com/global[/bold],\n"
            "both from the [bold]same Mi Account[/bold] but from [bold]2 different browsers[/bold].\n\n"
            f"  Cookie A: [cyan]{COOKIE_A}[/cyan]\n"
            f"  Cookie B: [cyan]{COOKIE_B}[/cyan]",
            border_style="cyan",
        )
    )

    browser1, token_a = _collect_cookie(COOKIE_A)
    _, token_b = _collect_cookie(COOKIE_B, exclude_browser=browser1)

    tokens = Tokens(firefox=token_a, chrome=token_b)
    save_tokens(tokens, token_file)
    console.print(f"\n[green]✓ Tokens saved to [bold]{token_file}[/bold][/green]")

    _verify_tokens(tokens)
    return tokens


def _verify_tokens(tokens: Tokens) -> None:
    """Ping the Xiaomi status API to verify new_bbs_serviceToken (Cookie A).

    Only Cookie A (new_bbs_serviceToken) can be verified via the status API —
    the API always interprets the cookie as new_bbs_serviceToken regardless of name.
    Cookie B (popRunToken) is used only at apply-time and cannot be pre-verified here.
    """
    from xiaomi_unlock.core import check_status

    console.print("\n[bold]Verifying Cookie A (new_bbs_serviceToken)…[/bold]")

    async def _run_checks() -> tuple:
        result_a = await check_status(tokens.firefox)
        return result_a

    with console.status(f"  Checking [cyan]{COOKIE_A}[/cyan]…"):
        result = asyncio.run(_run_checks())

    # Network errors are inconclusive — treat as warning, not success
    msg_lower = result.message.lower()
    network_error = msg_lower.startswith("network error")

    if network_error:
        console.print(
            f"  [yellow]? {COOKIE_A} — could not reach Xiaomi API ({result.message}). "
            "Token may still be valid.[/yellow]"
        )
    elif result.eligible or ("expired" not in msg_lower and "invalid" not in msg_lower and result.message):
        # Extract expiry if present in message (deadline_format is embedded by parse_status_response)
        console.print(f"  [green]✓ {COOKIE_A} — {result.message}[/green]")
    else:
        console.print(f"  [red]✗ {COOKIE_A} — {result.message or 'invalid/no response'}[/red]")
        console.print(
            "\n[yellow]Cookie A looks invalid. Re-run [bold]mi-unlock setup[/bold] "
            "with a fresh cookie from your browser.[/yellow]"
        )

    console.print(
        f"  [dim]ℹ {COOKIE_B} (Cookie B) is used at apply-time only — not verifiable here.[/dim]"
    )
