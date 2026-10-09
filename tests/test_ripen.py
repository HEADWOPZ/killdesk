import asyncio
import json
from datetime import timedelta
from pathlib import Path

from killdesk.book import Book
from killdesk.config import Settings
from killdesk.cycle import _scan, run_cycle
from killdesk.judge import MockJev
from killdesk.sources import FixtureSources
from killdesk.thresholds import DEFAULTS

from .test_cycle import FIXTURES, NOW

EARLY = "EarlyMint111111111111111111111111111111111"


def _pool(payload: dict, address: str) -> dict:
    return next(row for row in payload["data"] if address in json.dumps(row))


def test_too_early_pool_ripens_and_is_refetched_by_address() -> None:
    listing = json.loads((FIXTURES / "gecko_solana_p1.json").read_text())
    early_pool = _pool(listing, EARLY)
    pool_address = early_pool["attributes"]["address"]
    calls: list[list[str]] = []

    class Ripening(FixtureSources):
        async def gecko_pools_multi(self, chain, pool_addresses):
            calls.append(list(pool_addresses))
            if chain != "solana":
                return {"data": [], "included": []}
            return {"data": [early_pool], "included": listing.get("included") or []}

    book = Book(":memory:")
    try:
        sources = Ripening(FIXTURES)
        asyncio.run(run_cycle(Settings(etherscan_api_key="k"), book, sources, MockJev(), NOW))
        due = book.due_watch("solana", NOW + timedelta(minutes=29))
        assert [row["pool_address"] for row in due] == [pool_address]
        # Not due yet: nothing re-fetched.
        found, _errors, counts = asyncio.run(_scan(sources, 1, NOW + timedelta(minutes=10), book, DEFAULTS))
        assert calls == []
        # Once old enough the pool is fetched by address, tagged ripe, and leaves the watchlist.
        later = NOW + timedelta(minutes=29)
        found, _errors, counts = asyncio.run(_scan(sources, 1, later, book, DEFAULTS))
        assert calls == [[pool_address]]
        ripe = [c for c in found if c.token_address == EARLY]
        assert ripe and ripe[0].source == "ripe"
        assert counts.get("ripe") == 1
        assert ripe[0].age_minutes >= DEFAULTS.min_age_minutes
        assert not book.is_benched(f"solana:{EARLY}", later)
        assert book.due_watch("solana", later) == []
    finally:
        book.close()


def test_trending_pools_feed_the_funnel(tmp_path: Path) -> None:
    listing = json.loads((FIXTURES / "gecko_solana_p1.json").read_text())
    good = _pool(listing, "GoodMint")

    class Trending(FixtureSources):
        async def gecko_new_pools(self, chain, page):
            return {"data": [], "included": []}

        async def gecko_trending_pools(self, chain):
            self.gecko_calls += 1
            if chain != "solana":
                return {"data": [], "included": []}
            return {"data": [good], "included": listing.get("included") or []}

    book = Book(":memory:")
    try:
        result = asyncio.run(
            run_cycle(Settings(etherscan_api_key="k"), book, Trending(FIXTURES), MockJev(), NOW)
        )
        assert result.scanned == 1
        assert result.opened is True
        row = book.conn.execute("SELECT summary_json FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
        summary = json.loads(row[0])
        assert summary["sources"] == {"trending": 1}
        assert summary["funnel"]["free_pass"] == 1
        assert summary["funnel"]["pick"] == 1
        assert summary["jev"]["choice_symbol"] == "GOOD"
        assert "GOOD" in summary["jev"]["reasoning"]
        stages = [
            (r["stage"], r["outcome"], r["source"])
            for r in book.conn.execute("SELECT stage, outcome, source FROM stage_log ORDER BY id")
        ]
        assert stages == [
            ("free_kill", "PASS", "trending"),
            ("trade_kill", "PASS", "trending"),
            ("chain_kill", "PASS", "trending"),
            ("soft_kill", "PASS", "trending"),
            ("pick", "PASS", "trending"),
        ]
    finally:
        book.close()


def test_trending_can_be_turned_off() -> None:
    from dataclasses import replace

    settings = Settings(etherscan_api_key="k")
    settings.thresholds = replace(DEFAULTS, use_trending=False)

    class Boom(FixtureSources):
        async def gecko_trending_pools(self, chain):
            raise AssertionError("trending should not be called")

    book = Book(":memory:")
    try:
        asyncio.run(run_cycle(settings, book, Boom(FIXTURES), MockJev(), NOW))
    finally:
        book.close()
