"""Kill funnel. Cheapest check first. Every rejection names the check.

free_kill reads the listing. trade_kill reads DexScreener. chain_kill reads
plain facts (open authority, honeypot). soft_kill reads Jev's numbers.
Code does the arithmetic. Jev does the judgements.
"""

from __future__ import annotations

from killdesk.models import Candidate
from killdesk.thresholds import Thresholds


def free_kill(candidate: Candidate, thresholds: Thresholds) -> str | None:
    if candidate.age_minutes is None:
        return "missing_age"
    if candidate.age_minutes < thresholds.min_age_minutes:
        return "too_early"
    if candidate.age_minutes > thresholds.max_age_minutes:
        return "too_old"
    if candidate.reserve_usd is None or candidate.reserve_usd < thresholds.min_liquidity_usd:
        return "low_liquidity"
    mcap = (
        candidate.market_cap_usd
        if candidate.market_cap_usd is not None
        else candidate.fdv_usd
    )
    if mcap is None:
        return "missing_mcap"
    if mcap < thresholds.min_mcap_usd or mcap > thresholds.max_mcap_usd:
        return "mcap_out_of_band"
    if candidate.volume_h1 is None or candidate.volume_h1 < thresholds.min_volume_h1_usd:
        return "low_volume"
    if candidate.buys_h1 + candidate.sells_h1 < thresholds.min_tx_h1:
        return "low_tx_count"
    return None


def trade_kill(candidate: Candidate, thresholds: Thresholds) -> str | None:
    trade = candidate.trade
    if trade is None:
        return "no_dex_pair"
    total = trade.buys_h1 + trade.sells_h1
    if total < thresholds.min_dex_tx_h1:
        return "low_tx_count"
    if (trade.buys_h1 / total) < thresholds.min_buy_ratio_h1:
        return "sell_pressure"
    m5 = trade.buys_m5 + trade.sells_m5
    if m5 >= thresholds.min_m5_tx_for_return:
        if (trade.buys_m5 / m5) < thresholds.min_m5_buy_ratio:
            return "no_buyer_return"
    change = trade.price_change_h1
    if change is not None and change <= thresholds.max_dump_h1_pct:
        return "price_dump"
    if change is not None and change >= thresholds.max_extension_h1_pct:
        return "already_extended"
    return None


def chain_kill(candidate: Candidate, thresholds: Thresholds) -> str | None:
    facts = candidate.chain_facts
    if facts is None:
        return "chain_data_unavailable"
    if facts.honeypot is True:
        return "honeypot"
    if facts.no_contract:
        return "no_contract"
    if facts.mint_authority_open is True:
        return "mint_authority_open"
    if facts.freeze_authority_open is True:
        return "freeze_authority_open"
    if thresholds.kill_open_owner and facts.owner_open is True:
        return "owner_not_renounced"
    conc = facts.top1_ex_largest_pct
    if conc is not None and conc > thresholds.max_top1_ex_largest_pct:
        return "holder_concentration"
    if not facts.chain_ok:
        return "chain_data_unavailable"
    return None


def soft_kill(answers: dict, thresholds: Thresholds) -> str | None:
    rug = _noul(answers, "rug_risk")
    if rug is not None and rug >= thresholds.rug_risk_max:
        return "rug_risk"
    holder = _score(answers, "holder_concentration_danger")
    if holder is not None and holder >= thresholds.holder_danger_max:
        return "holder_concentration_danger"
    momentum = _score(answers, "momentum_quality")
    if momentum is not None and momentum < thresholds.momentum_min:
        return "weak_momentum"
    recycled = _noul(answers, "recycled_account")
    if recycled is not None and recycled >= thresholds.recycled_account_max:
        return "recycled_account"
    return None


def _noul(answers: dict, name: str) -> float | None:
    row = answers.get(name)
    if not isinstance(row, dict) or "noul" not in row:
        return None
    return float(row["noul"])


def _score(answers: dict, name: str) -> float | None:
    row = answers.get(name)
    if not isinstance(row, dict) or "score" not in row:
        return None
    return float(row["score"])
