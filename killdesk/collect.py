"""SCAN. Fresh pools from the public GeckoTerminal API, then DexScreener.

No FOMO session, no browser cookie, no wallet. Listing fields are parsed
here so free_kill can run without another request.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from killdesk.models import CHAINS, Candidate, TradeStats, canonical_address, is_major
from killdesk.numbers import fnum, inum

log = logging.getLogger("killdesk.collect")

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
DEX_BASE = "https://api.dexscreener.com"
GECKO_ACCEPT = "application/json;version=20230302"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _address_from_id(raw_id: str | None) -> str | None:
    if not raw_id or "_" not in raw_id:
        return None
    return raw_id.split("_", 1)[1]


def _symbol_from_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        return "?"
    return name.split("/")[0].strip() or "?"


def _tx(block: dict, window: str) -> dict:
    windows = block.get("transactions") or {}
    row = windows.get(window) or {}
    return row if isinstance(row, dict) else {}


def parse_new_pools(chain: str, payload: dict, now: datetime) -> list[Candidate]:
    if chain not in CHAINS:
        raise ValueError(f"unsupported chain {chain}")
    included: dict[str, dict] = {}
    for item in payload.get("included") or []:
        if isinstance(item, dict) and item.get("id"):
            included[str(item["id"])] = item.get("attributes") or {}
    found: list[Candidate] = []
    for pool in payload.get("data") or []:
        if not isinstance(pool, dict):
            continue
        candidate = _candidate_from_pool(chain, pool, included, now)
        if candidate is not None:
            found.append(candidate)
    return found


def _candidate_from_pool(
    chain: str, pool: dict, included: dict[str, dict], now: datetime
) -> Candidate | None:
    attrs = pool.get("attributes") or {}
    rels = pool.get("relationships") or {}

    def rel_id(name: str) -> str | None:
        data = (rels.get(name) or {}).get("data") or {}
        raw = data.get("id")
        return str(raw) if raw else None

    base_id = rel_id("base_token")
    quote_id = rel_id("quote_token")
    base_attr = dict(included.get(base_id or "", {}))
    quote_attr = dict(included.get(quote_id or "", {}))
    base_addr = base_attr.get("address") or _address_from_id(base_id)
    quote_addr = quote_attr.get("address") or _address_from_id(quote_id)
    if is_major(chain, base_addr) and not is_major(chain, quote_addr):
        base_addr, quote_addr = quote_addr, base_addr
        base_attr, quote_attr = quote_attr, base_attr
    if not base_addr or is_major(chain, base_addr):
        return None
    pool_address = attrs.get("address") or _address_from_id(pool.get("id"))
    if not pool_address:
        return None
    created = _parse_time(attrs.get("pool_created_at"))
    age = None
    if created is not None:
        age = (now - created).total_seconds() / 60.0
    h1 = _tx(attrs, "h1")
    m5 = _tx(attrs, "m5")
    volume = attrs.get("volume_usd") or {}
    change = attrs.get("price_change_percentage") or {}
    symbol = base_attr.get("symbol") or _symbol_from_name(attrs.get("name"))
    name = base_attr.get("name") or symbol
    dex = rel_id("dex")
    return Candidate(
        chain=chain,
        pool_address=str(pool_address),
        token_address=canonical_address(chain, str(base_addr)),
        symbol=str(symbol),
        name=str(name),
        price_usd=fnum(attrs.get("base_token_price_usd")),
        fdv_usd=fnum(attrs.get("fdv_usd")),
        market_cap_usd=fnum(attrs.get("market_cap_usd")),
        reserve_usd=fnum(attrs.get("reserve_in_usd")),
        volume_h1=fnum(volume.get("h1") if isinstance(volume, dict) else None),
        price_change_m5=fnum(change.get("m5") if isinstance(change, dict) else None),
        price_change_h1=fnum(change.get("h1") if isinstance(change, dict) else None),
        buys_m5=inum(m5.get("buys")) or 0,
        sells_m5=inum(m5.get("sells")) or 0,
        buys_h1=inum(h1.get("buys")) or 0,
        sells_h1=inum(h1.get("sells")) or 0,
        buyers_h1=inum(h1.get("buyers")) or 0,
        sellers_h1=inum(h1.get("sellers")) or 0,
        age_minutes=age,
        quote_symbol=quote_attr.get("symbol"),
        dex=dex,
    )


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        current = best.get(candidate.option_id)
        rank = candidate.reserve_usd if candidate.reserve_usd is not None else -1.0
        current_rank = -1.0
        if current is not None and current.reserve_usd is not None:
            current_rank = current.reserve_usd
        if current is None or rank > current_rank:
            best[candidate.option_id] = candidate
    return sorted(best.values(), key=lambda item: item.option_id)


def index_pairs(chain: str, pairs: list) -> dict[str, TradeStats]:
    """Best (highest-liquidity) pair per base-token address."""
    best: dict[str, tuple[float, TradeStats]] = {}
    for raw in pairs or []:
        if not isinstance(raw, dict):
            continue
        base = (raw.get("baseToken") or {}).get("address")
        if not base:
            continue
        key = canonical_address(chain, str(base))
        stats = trade_from_pair(raw)
        liquidity = stats.liquidity_usd if stats.liquidity_usd is not None else -1.0
        current = best.get(key)
        if current is None or liquidity > current[0]:
            best[key] = (liquidity, stats)
    return {key: value[1] for key, value in best.items()}


def trade_from_pair(raw: dict) -> TradeStats:
    txns = raw.get("txns") or {}
    h1 = txns.get("h1") or {}
    m5 = txns.get("m5") or {}
    volume = raw.get("volume") or {}
    change = raw.get("priceChange") or {}
    liquidity = raw.get("liquidity") or {}
    return TradeStats(
        price_usd=fnum(raw.get("priceUsd")),
        liquidity_usd=fnum(liquidity.get("usd") if isinstance(liquidity, dict) else None),
        volume_h1=fnum(volume.get("h1") if isinstance(volume, dict) else None),
        market_cap_usd=fnum(raw.get("marketCap")),
        buys_m5=inum(m5.get("buys")) or 0,
        sells_m5=inum(m5.get("sells")) or 0,
        buys_h1=inum(h1.get("buys")) or 0,
        sells_h1=inum(h1.get("sells")) or 0,
        price_change_m5=fnum(change.get("m5") if isinstance(change, dict) else None),
        price_change_h1=fnum(change.get("h1") if isinstance(change, dict) else None),
        pair_address=raw.get("pairAddress"),
        dex=raw.get("dexId"),
    )
