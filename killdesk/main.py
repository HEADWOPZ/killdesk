"""CLI. `killdesk run`, `killdesk report`, and `killdesk dashboard`.

Shadow mode is the only mode. `--no-shadow` exits without scanning.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import typer

from killdesk import __version__
from killdesk.book import Book, format_report, iso, parse_iso, utcnow
from killdesk.config import Settings
from killdesk.cycle import run_cycle
from killdesk.executor import Executor
from killdesk.judge import build_judge
from killdesk.sources import FixtureSources, LiveSources

app = typer.Typer(no_args_is_help=True, help="Shadow-mode memecoin desk. No orders, no wallet keys.")
log = logging.getLogger("killdesk")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@app.callback()
def _main() -> None:
    """KillDesk shadow desk."""


@app.command()
def run(
    once: bool = typer.Option(False, "--once", help="Run a single cycle and exit."),
    mock: bool = typer.Option(False, "--mock", help="Use the deterministic MockJev judge."),
    shadow: bool = typer.Option(
        True,
        "--shadow/--no-shadow",
        help="Shadow mode. On by default and cannot be disabled in this MVP.",
    ),
    fixtures: Path | None = typer.Option(
        None,
        "--fixtures",
        help="Directory of recorded JSON. Skips the network.",
    ),
    db: Path | None = typer.Option(None, "--db", help="SQLite path. Default desk.db or KILLDESK_DB."),
    pages: int = typer.Option(1, "--pages", help="GeckoTerminal new-pool pages per chain."),
    interval_minutes: int = typer.Option(15, "--interval-minutes", help="Daemon sleep between cycles."),
    dashboard: bool = typer.Option(
        False, "--dashboard", help="Also serve the live dashboard (same process, background thread)."
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Dashboard bind address (with --dashboard)."),
    port: int = typer.Option(8787, "--port", help="Dashboard port (with --dashboard)."),
    now: str | None = typer.Option(
        None, "--now", hidden=True, help="Pin the clock (ISO time) for replaying fixtures with --once."
    ),
) -> None:
    """Scan, kill, judge, and record a shadow position. Or just watch the open one."""
    _configure_logging()
    if not shadow:
        typer.echo(
            "KillDesk MVP is shadow-only. --no-shadow is refused. No order path is implemented.",
            err=True,
        )
        raise typer.Exit(code=2)
    if interval_minutes <= 0:
        raise typer.BadParameter("--interval-minutes must be positive")
    if pages <= 0:
        raise typer.BadParameter("--pages must be positive")
    Executor(shadow=True)
    settings = Settings.from_env()
    settings.shadow = True
    settings.pages = pages
    settings.interval_minutes = interval_minutes
    if db is not None:
        settings.db_path = db
    if fixtures is not None:
        settings.fixtures = fixtures
    if mock:
        settings.judge_backend = "mock"
    pinned = parse_iso(now) if now else None
    if dashboard:
        from killdesk.dashboard.server import start_in_thread

        server = start_in_thread(settings.db_path, host=host, port=port)
        typer.echo(f"KillDesk dashboard on http://{host}:{server.server_address[1]}  (shadow mode)")
    asyncio.run(_serve(settings, once=once, pinned_now=pinned))


@app.command("dashboard")
def dashboard_cmd(
    db: Path | None = typer.Option(None, "--db", help="SQLite path. Default desk.db or KILLDESK_DB."),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address. Use 0.0.0.0 to reach it from your LAN."),
    port: int = typer.Option(8787, "--port", help="Port."),
    live_marks: bool = typer.Option(
        True,
        "--live-marks/--no-live-marks",
        help="Fetch live DexScreener marks and GeckoTerminal candles for the focus token.",
    ),
) -> None:
    """Serve the animated live dashboard over desk.db. Read-only; run the desk separately."""
    from killdesk.dashboard.server import serve

    _configure_logging()
    settings = Settings.from_env()
    path = db or settings.db_path
    typer.echo(f"KillDesk dashboard on http://{host}:{port}  reading {path}  (shadow mode, read-only)")
    serve(path, host=host, port=port, live_marks=live_marks)


@app.command()
def report(
    db: Path | None = typer.Option(None, "--db", help="SQLite path. Default desk.db or KILLDESK_DB."),
) -> None:
    """Show shadow P&L, hit rate, and rejection stats from desk.db."""
    settings = Settings.from_env()
    path = db or settings.db_path
    if not path.exists():
        typer.echo(f"No book at {path}. Run `killdesk run --once` first.")
        raise typer.Exit(code=1)
    book = Book(path)
    try:
        typer.echo(format_report(book))
    finally:
        book.close()


def version() -> None:
    typer.echo(__version__)


app.command("version")(version)


async def _serve(settings: Settings, *, once: bool, pinned_now: datetime | None = None) -> None:
    book = Book(settings.db_path)
    model_label = "jev-mock" if settings.judge_backend == "mock" else settings.judge_model
    book.set_meta(
        mode="shadow",
        judge=model_label,
        interval_minutes=settings.interval_minutes,
        daemon_started_at=iso(utcnow()),
        status="starting",
        pid=os.getpid(),
    )
    key, base = settings.judge_key_and_url()
    judge = build_judge(
        mock=settings.judge_backend == "mock",
        backend=settings.judge_backend,
        api_key=key,
        base_url=base,
        model=settings.judge_model,
    )
    client: httpx.AsyncClient | None = None
    try:
        if settings.fixtures is not None:
            sources: Any = FixtureSources(settings.fixtures)
        else:
            client = httpx.AsyncClient(timeout=20)
            sources = LiveSources(client, settings)
        while True:
            started = pinned_now or utcnow()
            book.set_meta(status="scanning", cycle_started_at=iso(utcnow()), next_cycle_at=None)
            try:
                result = await run_cycle(settings, book, sources, judge, started)
                typer.echo(result.text)
            except Exception:
                log.exception("cycle crashed; continuing" if not once else "cycle crashed")
                book.set_meta(status="error")
                if once:
                    raise
            if once:
                book.set_meta(status="idle", next_cycle_at=None)
                return
            wake = utcnow() + timedelta(minutes=settings.interval_minutes)
            book.set_meta(status="sleeping", next_cycle_at=iso(wake), last_cycle_at=iso(utcnow()))
            await asyncio.sleep(settings.interval_minutes * 60)
    finally:
        await judge.aclose()
        book.close()
        if client is not None:
            await client.aclose()
