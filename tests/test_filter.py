from datetime import datetime, timezone

from killdesk.filter import chain_kill, free_kill, soft_kill, trade_kill
from killdesk.models import Candidate, ChainFacts, TradeStats
from killdesk.thresholds import DEFAULTS

NOW = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)


def candidate(**overrides: object) -> Candidate:
    payload: dict = {
        "chain": "solana",
        "pool_address": "pool",
        "token_address": "mint",
        "symbol": "T",
        "name": "Token",
        "reserve_usd": 50_000,
        "market_cap_usd": 100_000,
        "fdv_usd": 120_000,
        "volume_h1": 5_000,
        "buys_h1": 40,
        "sells_h1": 20,
        "buys_m5": 8,
        "sells_m5": 4,
        "age_minutes": 120,
    }
    payload.update(overrides)
    return Candidate(**payload)


def test_free_kill_reports_the_first_failing_check() -> None:
    young = candidate(age_minutes=5, reserve_usd=10)
    assert free_kill(young, DEFAULTS) == "too_early"
    assert free_kill(candidate(age_minutes=None), DEFAULTS) == "missing_age"
    assert free_kill(candidate(age_minutes=49 * 60), DEFAULTS) == "too_old"
    assert free_kill(candidate(reserve_usd=100), DEFAULTS) == "low_liquidity"
    assert free_kill(candidate(market_cap_usd=None, fdv_usd=None), DEFAULTS) == "missing_mcap"
    assert free_kill(candidate(market_cap_usd=10, fdv_usd=10), DEFAULTS) == "mcap_out_of_band"
    assert free_kill(candidate(market_cap_usd=None, fdv_usd=100_000), DEFAULTS) is None
    assert free_kill(candidate(volume_h1=10), DEFAULTS) == "low_volume"
    assert free_kill(candidate(buys_h1=1, sells_h1=1), DEFAULTS) == "low_tx_count"
    assert free_kill(candidate(), DEFAULTS) is None


def test_trade_kill_uses_dex_counts() -> None:
    assert trade_kill(candidate(trade=None), DEFAULTS) == "no_dex_pair"
    heavy = candidate(trade=TradeStats(buys_h1=10, sells_h1=90, buys_m5=1, sells_m5=1, price_change_h1=2))
    assert trade_kill(heavy, DEFAULTS) == "sell_pressure"
    returning = candidate(
        trade=TradeStats(buys_h1=40, sells_h1=20, buys_m5=1, sells_m5=10, price_change_h1=2)
    )
    assert trade_kill(returning, DEFAULTS) == "no_buyer_return"
    dumped = candidate(
        trade=TradeStats(buys_h1=40, sells_h1=20, buys_m5=8, sells_m5=4, price_change_h1=-55)
    )
    assert trade_kill(dumped, DEFAULTS) == "price_dump"
    extended = candidate(
        trade=TradeStats(buys_h1=40, sells_h1=20, buys_m5=8, sells_m5=4, price_change_h1=400)
    )
    assert trade_kill(extended, DEFAULTS) == "already_extended"
    healthy = candidate(
        trade=TradeStats(buys_h1=80, sells_h1=50, buys_m5=12, sells_m5=8, price_change_h1=8)
    )
    assert trade_kill(healthy, DEFAULTS) is None


def test_chain_kill_prefers_honeypot_over_authority() -> None:
    both = candidate(
        chain_facts=ChainFacts(
            source="bsc",
            chain_ok=True,
            honeypot=True,
            mint_authority_open=True,
            owner_open=True,
        )
    )
    assert chain_kill(both, DEFAULTS) == "honeypot"
    mint = candidate(
        chain_facts=ChainFacts(source="solana", chain_ok=True, mint_authority_open=True)
    )
    assert chain_kill(mint, DEFAULTS) == "mint_authority_open"
    freeze = candidate(
        chain_facts=ChainFacts(source="solana", chain_ok=True, freeze_authority_open=True)
    )
    assert chain_kill(freeze, DEFAULTS) == "freeze_authority_open"
    owner = candidate(chain_facts=ChainFacts(source="bsc", chain_ok=True, owner_open=True))
    assert chain_kill(owner, DEFAULTS) == "owner_not_renounced"
    concentrated = candidate(
        chain_facts=ChainFacts(source="solana", chain_ok=True, top1_ex_largest_pct=0.40)
    )
    assert chain_kill(concentrated, DEFAULTS) == "holder_concentration"
    missing = candidate(chain_facts=ChainFacts(source="solana", chain_ok=False))
    assert chain_kill(missing, DEFAULTS) == "chain_data_unavailable"
    clean = candidate(
        chain_facts=ChainFacts(
            source="solana",
            chain_ok=True,
            honeypot=False,
            mint_authority_open=False,
            freeze_authority_open=False,
            top1_ex_largest_pct=0.04,
        )
    )
    assert chain_kill(clean, DEFAULTS) is None


def test_soft_kill_thresholds() -> None:
    assert soft_kill({"rug_risk": {"noul": 0.71}}, DEFAULTS) == "rug_risk"
    assert (
        soft_kill(
            {
                "rug_risk": {"noul": 0.2},
                "holder_concentration_danger": {"score": 2.7},
            },
            DEFAULTS,
        )
        == "holder_concentration_danger"
    )
    assert (
        soft_kill(
            {
                "rug_risk": {"noul": 0.2},
                "holder_concentration_danger": {"score": 1.0},
                "momentum_quality": {"score": 0.4},
            },
            DEFAULTS,
        )
        == "weak_momentum"
    )
    assert (
        soft_kill(
            {
                "rug_risk": {"noul": 0.2},
                "holder_concentration_danger": {"score": 1.0},
                "momentum_quality": {"score": 2.0},
                "recycled_account": {"noul": 0.72},
            },
            DEFAULTS,
        )
        == "recycled_account"
    )
    assert (
        soft_kill(
            {
                "rug_risk": {"noul": 0.2},
                "holder_concentration_danger": {"score": 1.0},
                "momentum_quality": {"score": 2.0},
                "recycled_account": {"noul": 0.25},
            },
            DEFAULTS,
        )
        is None
    )
