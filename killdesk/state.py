"""Numeric state passed into Jev.

Every figure is computed here. Question text only points at field names.
"""

from __future__ import annotations

import math
from typing import Any

from killdesk.models import Candidate, ChainFacts


def build_state(candidate: Candidate) -> dict[str, Any]:
    trade = candidate.trade
    facts = candidate.chain_facts or ChainFacts(source="none")
    buys = trade.buys_h1 if trade else candidate.buys_h1
    sells = trade.sells_h1 if trade else candidate.sells_h1
    total = buys + sells
    buy_ratio = round(buys / total, 4) if total else None
    m5_buys = trade.buys_m5 if trade else candidate.buys_m5
    m5_sells = trade.sells_m5 if trade else candidate.sells_m5
    m5_total = m5_buys + m5_sells
    m5_ratio = round(m5_buys / m5_total, 4) if m5_total else None
    mcap, mcap_source = _mcap(candidate)
    liquidity = _first(
        trade.liquidity_usd if trade else None,
        candidate.reserve_usd,
    )
    price = _first(trade.price_usd if trade else None, candidate.price_usd)
    volume = _first(trade.volume_h1 if trade else None, candidate.volume_h1)
    change_h1 = _first(
        trade.price_change_h1 if trade else None,
        candidate.price_change_h1,
    )
    change_m5 = _first(
        trade.price_change_m5 if trade else None,
        candidate.price_change_m5,
    )
    handle = facts.twitter_handle
    state: dict[str, Any] = {
        "option_id": candidate.option_id,
        "chain": candidate.chain,
        "symbol": candidate.symbol,
        "name": candidate.name,
        "token_address": candidate.token_address,
        "pool_address": candidate.pool_address,
        "age_minutes": _round(candidate.age_minutes),
        "price_usd": price,
        "mcap_usd": _round(mcap),
        "mcap_source": mcap_source,
        "fdv_usd": _round(candidate.fdv_usd),
        "liquidity_usd": _round(liquidity),
        "liquidity_to_mcap": _round(liquidity / mcap) if liquidity and mcap else None,
        "volume_h1_usd": _round(volume),
        "volume_to_liquidity": _round(volume / liquidity) if volume is not None and liquidity else None,
        "buys_h1": buys,
        "sells_h1": sells,
        "buy_ratio_h1": buy_ratio,
        "buys_m5": m5_buys,
        "sells_m5": m5_sells,
        "buy_ratio_m5": m5_ratio,
        "listing_buyers_h1": candidate.buyers_h1,
        "listing_sellers_h1": candidate.sellers_h1,
        "price_change_h1_pct": _round(change_h1),
        "price_change_m5_pct": _round(change_m5),
        "mint_authority_open": facts.mint_authority_open,
        "freeze_authority_open": facts.freeze_authority_open,
        "owner_open": facts.owner_open,
        "honeypot": facts.honeypot,
        "source_verified": facts.source_verified,
        "etherscan_skipped": facts.etherscan_skipped,
        "no_contract": facts.no_contract,
        "top1_pct": _round(facts.top1_pct),
        "top10_pct": _round(facts.top10_pct),
        "top1_ex_largest_pct": _round(facts.top1_ex_largest_pct),
        "top10_ex_largest_pct": _round(facts.top10_ex_largest_pct),
        "holder_count": facts.holder_count,
        "gecko_top10_pct": _round(facts.gecko_top10_pct),
        "developer_holding_pct": _round(facts.developer_holding_pct),
        "twitter_handle": handle,
        "social_present": bool(handle),
        "code_bytes": facts.code_bytes,
        "dex": candidate.dex,
    }
    state["baseline_score"] = round(_baseline(state), 4)
    return state


def with_judgement(state: dict[str, Any], model: str, answers: dict) -> dict[str, Any]:
    updated = dict(state)
    updated["judge_model"] = model
    if "rug_risk" in answers:
        updated["rug_risk"] = answers["rug_risk"]["noul"]
    if "holder_concentration_danger" in answers:
        updated["holder_concentration_danger"] = answers["holder_concentration_danger"]["score"]
    if "momentum_quality" in answers:
        updated["momentum_quality"] = answers["momentum_quality"]["score"]
    if "recycled_account" in answers:
        updated["recycled_account"] = answers["recycled_account"]["noul"]
    return updated


PICK_FIELDS = (
    "option_id",
    "chain",
    "symbol",
    "token_address",
    "age_minutes",
    "price_usd",
    "mcap_usd",
    "mcap_source",
    "liquidity_usd",
    "liquidity_to_mcap",
    "volume_h1_usd",
    "buy_ratio_h1",
    "buy_ratio_m5",
    "price_change_h1_pct",
    "mint_authority_open",
    "freeze_authority_open",
    "owner_open",
    "honeypot",
    "top1_ex_largest_pct",
    "top10_ex_largest_pct",
    "holder_count",
    "social_present",
    "twitter_handle",
    "rug_risk",
    "holder_concentration_danger",
    "momentum_quality",
    "recycled_account",
    "baseline_score",
    "judge_model",
)


def public_candidate(state: dict[str, Any]) -> dict[str, Any]:
    return {key: state.get(key) for key in PICK_FIELDS}


def dossier_rank(candidate: Candidate) -> float:
    trade = candidate.trade
    liquidity = (trade.liquidity_usd if trade and trade.liquidity_usd else candidate.reserve_usd) or 0
    volume = (trade.volume_h1 if trade and trade.volume_h1 else candidate.volume_h1) or 0
    buys = trade.buys_h1 if trade else candidate.buys_h1
    sells = trade.sells_h1 if trade else candidate.sells_h1
    total = buys + sells
    ratio = buys / total if total else 0.0
    return ratio + math.log10(liquidity + 1) + 0.5 * math.log10(volume + 1)


def _baseline(state: dict[str, Any]) -> float:
    ratio = state.get("buy_ratio_h1") or 0.0
    liquidity = state.get("liquidity_usd") or 0.0
    volume = state.get("volume_h1_usd") or 0.0
    score = float(ratio)
    if liquidity > 0:
        score += 0.15 * math.log10(liquidity)
    if volume > 0:
        score += 0.10 * math.log10(volume)
    if state.get("honeypot"):
        score -= 5
    if state.get("mint_authority_open"):
        score -= 3
    if state.get("freeze_authority_open"):
        score -= 2
    return score


def _mcap(candidate: Candidate) -> tuple[float | None, str | None]:
    trade = candidate.trade
    if trade and trade.market_cap_usd:
        return trade.market_cap_usd, "dex_market_cap"
    if candidate.market_cap_usd is not None:
        return candidate.market_cap_usd, "market_cap"
    if candidate.fdv_usd is not None:
        return candidate.fdv_usd, "fdv"
    return None, None


def _first(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)
