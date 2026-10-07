"""CLI. `killdesk run` and `killdesk report`.

Shadow mode is the only mode. `--no-shadow` exits without scanning.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import typer

from killdesk import __version__
from killdesk.book import Book, format_report, utcnow
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
    asyncio.run(_serve(settings, once=once))


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


async def _serve(settings: Settings, *, once: bool) -> None:
    book = Book(settings.db_path)
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
            try:
                result = await run_cycle(settings, book, sources, judge, utcnow())
                typer.echo(result.text)
            except Exception:
                log.exception("cycle crashed; continuing" if not once else "cycle crashed")
                if once:
                    raise
            if once:
                return
            await asyncio.sleep(settings.interval_minutes * 60)
    finally:
        await judge.aclose()
        book.close()
        if client is not None:
            await client.aclose()
