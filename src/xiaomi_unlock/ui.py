"""Rich UI components for the Xiaomi unlock CLI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .core import ApplyResult, StatusResult

console = Console()


# ── NTP Spinner ───────────────────────────────────────────────────────
@contextmanager
def ntp_spinner(server: str) -> Generator[None, None, None]:
    """Context manager showing a spinner while querying an NTP server."""
    with console.status(f"[yellow]Syncing NTP via {server}...[/yellow]", spinner="dots"):
        yield


# ── Status Panel ──────────────────────────────────────────────────────
def status_panel(result: StatusResult) -> Panel:
    """Render account status as a Rich Panel."""
    if result.eligible:
        icon = "✓"
        color = "green"
    else:
        icon = "✗"
        color = "red"

    return Panel(
        f"[{color}]{icon} {result.message}[/{color}]",
        title="[bold]Account Status[/bold]",
        border_style=color,
        padding=(0, 2),
    )


# ── Countdown Display ─────────────────────────────────────────────────
def _fmt_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


async def countdown_display(
    midnight: datetime,
    clock_synced_now: Callable[[], datetime],
    offsets_ms: list[float],
    plain: bool = False,
) -> None:
    """Show a live HH:MM:SS countdown until midnight Beijing.

    Args:
        midnight: Target datetime (midnight Beijing)
        clock_synced_now: Callable returning current synced datetime
        offsets_ms: Worker offsets to display
        plain: If True, print lines instead of Rich Live
    """
    if plain:
        remaining = (midnight - clock_synced_now()).total_seconds()
        while remaining > 1:
            remaining = (midnight - clock_synced_now()).total_seconds()
            print(f"  Waiting: {_fmt_countdown(remaining)}", end="\r", flush=True)
            await asyncio.sleep(1)
        print()
        return

    def make_panel() -> Panel:
        remaining = (midnight - clock_synced_now()).total_seconds()
        countdown_text = Text(_fmt_countdown(remaining), style="bold cyan", justify="center")
        body = (
            countdown_text.markup
            + f"\n\n[dim]Target:[/dim] {midnight.strftime('%Y-%m-%d %H:%M:%S')} Beijing (UTC+8)"
            + f"\n[dim]Workers:[/dim] {len(offsets_ms)} × offsets {offsets_ms} ms"
            + "\n\n[yellow]Keep this terminal open![/yellow]"
        )
        return Panel(
            body,
            title="[bold]⏱ Countdown to Midnight[/bold]",
            border_style="cyan",
            padding=(1, 4),
        )

    remaining = (midnight - clock_synced_now()).total_seconds()
    with Live(make_panel(), console=console, refresh_per_second=2) as live:
        while remaining > 0.5:
            remaining = (midnight - clock_synced_now()).total_seconds()
            live.update(make_panel())
            await asyncio.sleep(0.5)


# ── Worker Table ──────────────────────────────────────────────────────
def make_worker_table(
    num_workers: int,
    offsets_ms: list[float],
    statuses: dict[int, str] | None = None,
    attempts: dict[int, int] | None = None,
) -> Table:
    """Build a Rich Table showing per-worker status.

    Args:
        num_workers: Number of workers
        offsets_ms: List of ms offsets per worker
        statuses: {wid: status_str} e.g. "waiting", "firing", "approved", "failed"
        attempts: {wid: attempt_count}
    """
    statuses = statuses or {}
    attempts = attempts or {}

    table = Table(title="Workers", border_style="dim", show_lines=False)
    table.add_column("Worker", style="bold cyan", justify="center")
    table.add_column("Offset", justify="right")
    table.add_column("Attempts", justify="right")
    table.add_column("Status", justify="left")

    status_styles = {
        "waiting": "dim",
        "firing": "yellow",
        "approved": "bold green",
        "failed": "red",
        "quota": "red",
        "stopped": "dim",
        "maybe": "yellow",
    }

    for i in range(num_workers):
        wid = i + 1
        off = offsets_ms[i] if i < len(offsets_ms) else "?"
        st = statuses.get(wid, "waiting")
        att = str(attempts.get(wid, 0)) if attempts.get(wid) else "-"
        style = status_styles.get(st, "")
        table.add_row(
            f"W{wid}",
            f"{off}ms",
            att,
            f"[{style}]{st}[/{style}]" if style else st,
        )

    return table


# ── Results Panel ──────────────────────────────────────────────────────
def results_panel(results: list[ApplyResult]) -> Panel:
    """Render final results as a Rich Panel with next steps if approved."""
    approved = any(r.approved is True for r in results)
    maybe = any(r.approved is None and r.message != "Stopped (another worker succeeded)" for r in results)

    lines: list[str] = []
    for r in results:
        if r.approved is True:
            lines.append(f"[bold green]★ W{r.worker_id}: APPROVED ({r.attempts} attempts)[/bold green]")
        elif r.approved is None and "Stopped" not in r.message:
            lines.append(f"[yellow]? W{r.worker_id}: MAYBE — {r.message}[/yellow]")
        elif r.approved is False:
            lines.append(f"[red]✗ W{r.worker_id}: {r.message}[/red]")

    summary = "\n".join(lines) if lines else "[dim]No results[/dim]"

    if approved:
        next_steps = (
            "\n\n[bold]Next steps on your Xiaomi 15:[/bold]\n"
            "  1. Sign out of Mi Account\n"
            "  2. Restart phone\n"
            "  3. Sign in to Mi Account\n"
            "  4. Developer Options → Mi Unlock Status → Link Account\n"
            "  5. Use Mi Unlock Tool on PC [dim](72h waiting period)[/dim]"
        )
        body = summary + next_steps
        title = "[bold green]★★★ UNLOCK REQUEST APPROVED! ★★★[/bold green]"
        border = "green"
    elif maybe:
        body = summary + "\n\n[yellow]Check your Mi Account status manually.[/yellow]"
        title = "[bold yellow]Results — Uncertain[/bold yellow]"
        border = "yellow"
    else:
        body = summary + "\n\n[dim]Try again tomorrow at midnight Beijing time.[/dim]"
        title = "[bold red]Results — Not Approved[/bold red]"
        border = "red"

    return Panel(body, title=title, border_style=border, padding=(1, 2))


# ── Header ────────────────────────────────────────────────────────────
def print_header() -> None:
    console.print(
        Panel.fit(
            "[bold cyan]🔓 Xiaomi Bootloader Unlock[/bold cyan]\n"
            "[dim]AQLR method — NTP-synced to midnight Beijing time[/dim]",
            border_style="cyan",
        )
    )
