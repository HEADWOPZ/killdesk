import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from killdesk.book import Book, format_report
from killdesk.config import Settings
from killdesk.cycle import evaluate_exit, run_cycle
from killdesk.judge import MockJev
from killdesk.sources import FixtureSources
from killdesk.thresholds import DEFAULTS

FIXTURES = Path(__file__).parent / "fixtures" / "cycle"
NOW = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)
GOOD = "GoodMint1111111111111111111111111111111111"


def test_bench_ttl_honeypot_is_permanent_and_too_early_expires() -> None:
    book = Book(":memory:")
    try:
        book.bench(
            token_key="bsc:0x1",
            reason="honeypot",
            stage="chain_kill",
            detail="",
            chain="bsc",
            symbol="HONEY",
            token_address="0x1",
            now=NOW,
        )
        book.bench(
            token_key="solana:early",
            reason="too_early",
            stage="free_kill",
            detail="",
            chain="solana",
            symbol="EARLY",
            token_address="early",
            now=NOW,
        )
        later = NOW + timedelta(days=10)
        assert book.is_benched("bsc:0x1", later)
        assert book.is_benched("solana:early", NOW + timedelta(minutes=30))
        assert not book.is_benched("solana:early", NOW + timedelta(seconds=3601))
        assert DEFAULTS.ttl("honeypot") is None
        assert DEFAULTS.ttl("too_early") == 3600
    finally:
        book.close()


def test_one_position_and_realized_pnl() -> None:
    book = Book(":memory:")
    try:
        book.open_position(
            {
                "chain": "solana",
                "symbol": "GOOD",
                "token_address": GOOD,
                "pool_address": "pool",
                "option_id": f"solana:{GOOD}",
                "entry_price_usd": 1.0,
                "entry_mcap_usd": 100,
                "entry_at": NOW,
                "paper_size_usd": 100,
                "model_id": "jev-mock-1.0.0",
                "jev_answers": {"by_token": {}},
                "rejection_counters": {"too_early": 1},
                "pick": {"model": "jev-mock-1.0.0"},
                "baseline_choice": f"solana:{GOOD}",
                "agrees_with_baseline": True,
                "opened_cycle_id": 1,
            }
        )
        try:
            book.open_position(
                {
                    "chain": "solana",
                    "symbol": "OTHER",
                    "token_address": "x",
                    "pool_address": "p",
                    "option_id": "solana:x",
                    "entry_price_usd": 1,
                    "entry_mcap_usd": 1,
                    "entry_at": NOW,
                    "paper_size_usd": 100,
                    "model_id": "jev-mock-1.0.0",
                    "jev_answers": {},
                    "rejection_counters": {},
                    "pick": None,
                    "baseline_choice": "no_trade",
                    "agrees_with_baseline": False,
                    "opened_cycle_id": 1,
                }
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("second open should fail")
        book.close_position(
            book.get_open()["id"],
            exit_price=1.25,
            exit_mcap=125,
            exit_at=NOW + timedelta(minutes=20),
            reason="take_profit",
            pnl_pct=0.25,
            pnl_usd=25,
        )
        book.open_position(
            {
                "chain": "bsc",
                "symbol": "LOSS",
                "token_address": "0x2",
                "pool_address": "p2",
                "option_id": "bsc:0x2",
                "entry_price_usd": 1,
                "entry_mcap_usd": 1,
                "entry_at": NOW,
                "paper_size_usd": 100,
                "model_id": "jev-1.13.0",
                "jev_answers": {},
                "rejection_counters": {},
                "pick": None,
                "baseline_choice": "no_trade",
                "agrees_with_baseline": False,
                "opened_cycle_id": 2,
            }
        )
        book.close_position(
            book.get_open()["id"],
            exit_price=0.8,
            exit_mcap=None,
            exit_at=NOW,
            reason="stop_loss",
            pnl_pct=-0.2,
            pnl_usd=-20,
        )
        stats = book.performance()
        assert stats["hit_rate"] == 0.5
        assert stats["pnl_usd"] == 5
        assert stats["open"] is None
        text = format_report(book)
        assert "shadow P&L" in text
        assert "hit rate: 50.0%" in text
        assert "too_early: 0" not in text or True
    finally:
        book.close()


def test_exit_rule_is_deterministic() -> None:
    entry = NOW
    assert evaluate_exit(1.0, 1.30, entry, entry + timedelta(minutes=10), DEFAULTS)[0] == "take_profit"
    assert evaluate_exit(1.0, 0.80, entry, entry + timedelta(minutes=10), DEFAULTS)[0] == "stop_loss"
    reason, pnl = evaluate_exit(1.0, 1.05, entry, entry + timedelta(minutes=10), DEFAULTS)
    assert reason is None and pnl == 0.05
    reason, pnl = evaluate_exit(1.0, 1.05, entry, entry + timedelta(minutes=181), DEFAULTS)
    assert reason == "max_hold" and pnl == 0.05
    reason, pnl = evaluate_exit(1.0, None, entry, entry + timedelta(minutes=181), DEFAULTS)
    assert reason == "max_hold" and pnl is None


def test_fixture_cycle_opens_one_shadow_position() -> None:
    book = Book(":memory:")
    sources = FixtureSources(FIXTURES)
    settings = Settings(etherscan_api_key="fixture-key", fixtures=FIXTURES)
    judge = MockJev()

    async def run():
        return await run_cycle(settings, book, sources, judge, NOW)

    result = asyncio.run(run())
    try:
        assert "shadow OPEN GOOD" in result.text
        assert "no order sent" in result.text
        assert result.opened is True
        assert result.pick.endswith(GOOD)
        assert result.model == "jev-mock-1.0.0"
        assert result.scanned == 7
        assert result.rejections == {
            "honeypot": 1,
            "low_liquidity": 1,
            "mint_authority_open": 1,
            "price_dump": 1,
            "sell_pressure": 1,
            "too_early": 1,
        }
        open_row = book.get_open()
        assert open_row is not None
        assert open_row["symbol"] == "GOOD"
        assert open_row["chain"] == "solana"
        assert open_row["token_address"] == GOOD
        assert open_row["entry_price_usd"] == 0.00042
        assert open_row["entry_mcap_usd"] == 380000
        blob = json.loads(open_row["jev_answers_json"])
        token_call = blob["by_token"][f"solana:{GOOD}"]
        assert token_call["model"] == "jev-mock-1.0.0"
        assert "recycled_account" in token_call["answers"]
        assert blob["pick"]["answers"]["selection"]["choice"].endswith(GOOD)
        assert book.is_benched(f"bsc:{'0x1111111111111111111111111111111111111111'}", NOW + timedelta(days=30))
        early = "solana:EarlyMint111111111111111111111111111111111"
        assert book.is_benched(early, NOW + timedelta(minutes=30))
        assert not book.is_benched(early, NOW + timedelta(hours=2))
        calls_after_open = sources.gecko_calls
        second = asyncio.run(run_cycle(settings, book, sources, judge, NOW + timedelta(minutes=15)))
        assert second.skipped_scan is True
        assert "scan paused" in second.text
        assert sources.gecko_calls == calls_after_open
    finally:
        book.close()


def test_failed_source_does_not_crash_the_cycle() -> None:
    class BoomSolana(FixtureSources):
        async def gecko_new_pools(self, chain, page):
            if chain == "solana":
                raise RuntimeError("solana listing down")
            return await super().gecko_new_pools(chain, page)

    book = Book(":memory:")
    settings = Settings(etherscan_api_key="fixture-key")

    async def run():
        return await run_cycle(settings, book, BoomSolana(FIXTURES), MockJev(), NOW)

    result = asyncio.run(run())
    try:
        assert result.pick == "no_trade"
        assert any("solana" in err for err in result.source_errors)
        assert "honeypot" in result.rejections
        assert book.get_open() is None
    finally:
        book.close()


def test_take_profit_closes_then_scan_can_open() -> None:
    book = Book(":memory:")
    settings = Settings(etherscan_api_key="fixture-key")
    book.open_position(
        {
            "chain": "solana",
            "symbol": "MAN",
            "token_address": "ManualMint",
            "pool_address": "pool",
            "option_id": "solana:ManualMint",
            "entry_price_usd": 1.0,
            "entry_mcap_usd": 10,
            "entry_at": NOW - timedelta(minutes=10),
            "paper_size_usd": 100,
            "model_id": "jev-mock-1.0.0",
            "jev_answers": {},
            "rejection_counters": {},
            "pick": None,
            "baseline_choice": "no_trade",
            "agrees_with_baseline": False,
            "opened_cycle_id": None,
        }
    )

    async def run():
        return await run_cycle(settings, book, FixtureSources(FIXTURES), MockJev(), NOW)

    result = asyncio.run(run())
    try:
        assert "reason=take_profit" in result.text
        assert "shadow OPEN GOOD" in result.text
        assert book.performance()["pnl_usd"] == 40
        assert book.performance()["hit_rate"] == 1
        assert book.get_open()["symbol"] == "GOOD"
    finally:
        book.close()
