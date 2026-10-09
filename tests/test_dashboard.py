import asyncio
import json
import threading
import urllib.request
from pathlib import Path

from typer.testing import CliRunner

from killdesk.book import Book
from killdesk.config import Settings
from killdesk.cycle import run_cycle
from killdesk.dashboard.server import build_server
from killdesk.dashboard.snapshot import build_snapshot
from killdesk.judge import MockJev
from killdesk.main import app
from killdesk.sources import FixtureSources

from .test_cycle import FIXTURES, NOW


def _seed(path: Path) -> None:
    book = Book(path)
    try:
        book.set_meta(status="sleeping", judge="jev-mock", next_cycle_at="2026-10-07T18:15:00+00:00")
        asyncio.run(run_cycle(Settings(etherscan_api_key="k"), book, FixtureSources(FIXTURES), MockJev(), NOW))
    finally:
        book.close()


def test_snapshot_has_every_panel(tmp_path: Path) -> None:
    db = tmp_path / "desk.db"
    _seed(db)
    snap = build_snapshot(db, NOW)
    assert snap["ok"] is True
    assert snap["meta"]["mode"] == "shadow"
    assert snap["meta"]["status"] == "sleeping"
    latest = snap["latest"]
    assert latest["funnel"]["scanned"] == 7
    assert latest["funnel"]["pick"] == 1
    assert latest["jev"]["choice_symbol"] == "GOOD"
    assert {row["outcome"] for row in snap["feed"]} == {"PASS", "DROP"}
    assert snap["open"]["symbol"] == "GOOD"
    assert snap["focus"]["kind"] == "position"
    assert [c["stage"] for c in snap["focus"]["checks"]][-1] == "pick"
    assert snap["bench_total"] >= 6 and snap["bench_permanent"] >= 1
    assert any(row["reason"] == "too_early" for row in snap["bench"])
    assert snap["watchlist"] == 1
    assert snap["performance"]["equity_usd"] == 1000.0


def test_snapshot_without_a_book(tmp_path: Path) -> None:
    snap = build_snapshot(tmp_path / "missing.db")
    assert snap["ok"] is False
    assert snap["cycles"] == []


def test_server_serves_page_snapshot_and_stream(tmp_path: Path) -> None:
    db = tmp_path / "desk.db"
    _seed(db)
    server = build_server(db, "127.0.0.1", 0, live_marks=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "KILLDESK" in page and "SHADOW MODE" in page
        assert "cdn" not in page.lower() and "https://" not in page
        snap = json.loads(urllib.request.urlopen(base + "/api/snapshot").read())
        assert snap["latest"]["funnel"]["scanned"] == 7
        assert snap["live_marks"] is False
        body = urllib.request.urlopen(base + "/api/stream?limit=1").read().decode()
        assert body.startswith("event: snapshot\ndata: ")
        assert json.loads(body.split("data: ", 1)[1])["ok"] is True
        assert urllib.request.urlopen(base + "/healthz").read() == b"ok"
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_command_is_registered() -> None:
    result = CliRunner().invoke(app, ["dashboard", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.stdout
    run_help = CliRunner().invoke(app, ["run", "--help"])
    assert "--dashboard" in run_help.stdout
