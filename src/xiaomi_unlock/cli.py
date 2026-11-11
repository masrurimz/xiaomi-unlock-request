"""Xiaomi Bootloader Unlock CLI — Click-based command interface."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

import click
from rich.live import Live

from .config import TokenError, load_tokens, setup_wizard
from .core import (
    TIME_OFFSETS_MS,
    ApplyResult,
    Clock,
    RealClock,
    StatusResult,
    check_status,
    run_workers,
    sync_ntp,
    validate_offsets,
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

    Sends parallel HTTP requests to Xiaomi's unlock API around
    00:00 Beijing time (UTC+8) with configurable timing offsets.
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
def parse_offsets_ms(ctx: click.Context, param: click.Parameter, value: str) -> list[float]:
    """Parse and validate before loading credentials or making network requests."""
    try:
        offsets = [float(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise click.BadParameter("Use comma-separated numbers, e.g. 1400,900,400,100", ctx, param) from exc
    try:
        validate_offsets(offsets)
    except ValueError as exc:
        raise click.BadParameter(str(exc), ctx, param) from exc
    return offsets


async def run_scheduled(
    tokens: tuple[str, str],
    clock: Clock,
    midnight: datetime,
    offsets: list[float],
    dry_run: bool = False,
    on_attempt: Callable[[int, int, datetime], None] | None = None,
) -> list[ApplyResult]:
    """Arm workers immediately; the countdown is only an independent display."""
    countdown = asyncio.create_task(countdown_display(midnight, clock.synced_now))
    try:
        return await run_workers(tokens, clock, offsets, dry_run, on_attempt, midnight=midnight)
    finally:
        countdown.cancel()
        with suppress(asyncio.CancelledError):
            await countdown


@cli.command()
@click.option(
    "--token-file",
    default=None,
    type=click.Path(path_type=Path, exists=False),
    help="Path to tokens file (default: tokens.json or token.txt)",
)
@click.option("--dry-run", is_flag=True, help="Skip actual HTTP POST (for testing)")
@click.option(
    "--offsets-ms",
    default=",".join(f"{offset:g}" for offset in TIME_OFFSETS_MS),
    envvar="MI_UNLOCK_OFFSETS_MS",
    callback=parse_offsets_ms,
    show_default=True,
    show_envvar=True,
    help=(
        "Comma-separated milliseconds BEFORE midnight: 500 = 500 ms before; "
        "-500 = 500 ms after; 0 = midnight. One worker per value. "
        "Negative offsets must be greater than -30000 (the retry deadline)."
    ),
)
@click.option(
    "--plain",
    is_flag=True,
    help="Disable the Rich Live worker table (countdown always logs new lines)",
)
def run(token_file: Path | None, dry_run: bool, offsets_ms: list[float], plain: bool) -> None:
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
    assert beijing is not None
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
    worker_count = len(offsets_ms)
    offsets_label = ", ".join(f"{offset:g}" for offset in offsets_ms)
    console.print(f"  Workers: {worker_count} × offsets [{offsets_label}] ms", markup=False)
    console.print("  Positive = before midnight; negative = after midnight")
    for wid, offset in enumerate(offsets_ms, 1):
        target = midnight - timedelta(milliseconds=offset)
        console.print(f"  W{wid}: scheduled {target.isoformat(timespec='milliseconds')} Beijing")

    # 5. Workers run alongside the countdown, using the same pinned midnight.
    console.print("\n[bold]── Arming Workers ──[/bold]")

    worker_statuses: dict[int, str] = {i: "waiting" for i in range(1, worker_count + 1)}
    worker_attempts: dict[int, int] = {}

    def on_attempt(wid: int, attempt: int, fire_time: datetime) -> None:
        worker_statuses[wid] = "firing"
        worker_attempts[wid] = attempt
        print(
            f"{fire_time.isoformat(timespec='milliseconds')} Beijing | W{wid}: dispatching attempt {attempt}",
            flush=True,
        )

    if not plain and console.is_terminal:
        with Live(
            make_worker_table(worker_count, offsets_ms, worker_statuses, worker_attempts),
            console=console,
            refresh_per_second=4,
        ) as live:

            async def _run_with_live() -> list[ApplyResult]:
                def _on_attempt(wid: int, attempt: int, fire_time: datetime) -> None:
                    on_attempt(wid, attempt, fire_time)
                    live.update(make_worker_table(worker_count, offsets_ms, worker_statuses, worker_attempts))

                return await run_scheduled(
                    (tokens.firefox, tokens.chrome),
                    clock,
                    midnight,
                    offsets_ms,
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
        console.print(make_worker_table(worker_count, offsets_ms, worker_statuses, worker_attempts))
    else:
        results = asyncio.run(
            run_scheduled(
                (tokens.firefox, tokens.chrome),
                clock,
                midnight,
                offsets_ms,
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
