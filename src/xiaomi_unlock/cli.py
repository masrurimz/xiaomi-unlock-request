"""Xiaomi Bootloader Unlock CLI — Click-based command interface."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import timedelta
from pathlib import Path

import click
from rich.live import Live

from .config import TokenError, load_tokens, setup_wizard
from .core import (
    TIME_OFFSETS_MS,
    RealClock,
    StatusResult,
    check_status,
    run_workers,
    sync_ntp,
)
from .ui import (
    console,
    countdown_display,
    make_worker_table,
    print_header,
    results_panel,
    status_panel,
)


# ── CLI Group ─────────────────────────────────────────────────────────
@click.group(invoke_without_command=True)
@click.version_option("2.0.0", prog_name="mi-unlock")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """🔓 Xiaomi Bootloader Unlock — AQLR method.

    Sends 4 parallel HTTP requests to Xiaomi's unlock API at exactly
    00:00 Beijing time (UTC+8) with staggered timing offsets.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ── setup ─────────────────────────────────────────────────────────────
@cli.command()
@click.option(
    "--token-file",
    default="tokens.json",
    type=click.Path(path_type=Path),
    help="Where to save tokens (default: tokens.json)",
    show_default=True,
)
def setup(token_file: Path) -> None:
    """Interactive wizard to save your Firefox + Chrome tokens."""
    print_header()
    try:
        setup_wizard(token_file)
    except (KeyboardInterrupt, click.Abort):
        console.print("\n[yellow]Setup cancelled.[/yellow]")
        sys.exit(0)


# ── status ────────────────────────────────────────────────────────────
@cli.command()
@click.option(
    "--token-file",
    default=None,
    type=click.Path(path_type=Path, exists=False),
    help="Path to tokens file (default: tokens.json or token.txt)",
)
def status(token_file: Path | None) -> None:
    """Check your Mi Account unlock eligibility."""
    print_header()

    try:
        tokens = load_tokens(token_file)
    except TokenError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)

    console.print("\n[bold]── Account Status ──[/bold]")
    with console.status("[yellow]Checking eligibility...[/yellow]", spinner="dots"):
        result: StatusResult = asyncio.run(check_status(tokens.firefox))

    console.print(status_panel(result))

    if not result.eligible:
        sys.exit(1)


# ── run ───────────────────────────────────────────────────────────────
@cli.command()
@click.option(
    "--token-file",
    default=None,
    type=click.Path(path_type=Path, exists=False),
    help="Path to tokens file (default: tokens.json or token.txt)",
)
@click.option("--dry-run", is_flag=True, help="Skip actual HTTP POST (for testing)")
@click.option(
    "--plain",
    is_flag=True,
    help="Line-based output (no Rich Live countdown or worker table)",
)
def run(token_file: Path | None, dry_run: bool, plain: bool) -> None:
    """Run the AQLR unlock attempt at next Beijing midnight."""
    print_header()

    if dry_run:
        console.print("[yellow]⚠ DRY-RUN mode — no real HTTP requests will be sent[/yellow]")

    # 1. Load tokens
    try:
        tokens = load_tokens(token_file)
    except TokenError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)

    console.print("\n[green]✓ Loaded tokens (Firefox + Chrome)[/green]")

    # 2. Account status
    console.print("\n[bold]── Account Status ──[/bold]")
    with console.status("[yellow]Checking eligibility...[/yellow]", spinner="dots"):
        result: StatusResult = asyncio.run(check_status(tokens.firefox))

    console.print(status_panel(result))
    if not result.eligible:
        sys.exit(1)

    # 3. NTP sync
    console.print("\n[bold]── NTP Sync ──[/bold]")
    ntp_result = sync_ntp()
    if not ntp_result.success:
        console.print(f"[red]✗ All NTP servers failed: {ntp_result.error}[/red]")
        sys.exit(1)

    beijing = ntp_result.beijing_time
    console.print(f"  [green]✓ {ntp_result.server}: {beijing.strftime('%Y-%m-%d %H:%M:%S.%f')}[/green]")

    clock = RealClock(beijing, time.monotonic())

    cur = clock.synced_now()
    tomorrow = cur + timedelta(days=1)
    midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    secs = (midnight - cur).total_seconds()
    h, m = int(secs // 3600), int((secs % 3600) // 60)

    console.print("\n[bold]── Countdown ──[/bold]")
    console.print(f"  Target:  {midnight.strftime('%Y-%m-%d %H:%M:%S')} Beijing (UTC+8)")
    console.print(f"  Wait:    ~{h}h {m}m")
    console.print(f"  Workers: 4 × offsets {TIME_OFFSETS_MS} ms")

    if secs > 5:
        asyncio.run(countdown_display(midnight, clock.synced_now, TIME_OFFSETS_MS, plain=plain))

    # 5. Workers
    console.print("\n[bold]── Firing Workers ──[/bold]")

    worker_statuses: dict[int, str] = {i: "waiting" for i in range(1, 5)}
    worker_attempts: dict[int, int] = {}

    def on_attempt(wid: int, attempt: int, fire_time) -> None:
        worker_statuses[wid] = "firing"
        worker_attempts[wid] = attempt

    if not plain:
        with Live(
            make_worker_table(4, TIME_OFFSETS_MS, worker_statuses, worker_attempts),
            console=console,
            refresh_per_second=4,
        ) as live:

            async def _run_with_live():
                def _on_attempt(wid, attempt, fire_time):
                    on_attempt(wid, attempt, fire_time)
                    live.update(make_worker_table(4, TIME_OFFSETS_MS, worker_statuses, worker_attempts))

                return await run_workers(
                    (tokens.firefox, tokens.chrome),
                    clock,
                    TIME_OFFSETS_MS,
                    dry_run=dry_run,
                    on_attempt=_on_attempt,
                )

            results = asyncio.run(_run_with_live())

        # Update final statuses
        for r in results:
            if r.approved is True:
                worker_statuses[r.worker_id] = "approved"
            elif r.approved is False:
                worker_statuses[r.worker_id] = "failed"
            else:
                worker_statuses[r.worker_id] = "maybe"
        console.print(make_worker_table(4, TIME_OFFSETS_MS, worker_statuses, worker_attempts))
    else:
        results = asyncio.run(
            run_workers(
                (tokens.firefox, tokens.chrome),
                clock,
                TIME_OFFSETS_MS,
                dry_run=dry_run,
                on_attempt=on_attempt,
            )
        )

    # 6. Results
    console.print()
    console.print(results_panel(results))

    approved = any(r.approved is True for r in results)
    sys.exit(0 if approved else 1)


def main() -> None:
    """Entry point for mi-unlock command."""
    cli()


if __name__ == "__main__":
    main()
