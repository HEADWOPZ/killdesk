"""Route a survivor to the chain module and merge GeckoTerminal token info."""

from __future__ import annotations

import logging
import re
from typing import Any

from killdesk.chains import bsc, robinhood, solana
from killdesk.models import Candidate, ChainFacts
from killdesk.numbers import fraction, inum

log = logging.getLogger("killdesk.chains")


def normalize_handle(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text.lower() in {"null", "none"}:
        return None
    text = re.sub(r"^https?://(www\.)?(twitter\.com|x\.com)/", "", text, flags=re.I)
    text = text.strip().strip("/")
    return text or None


def merge_gecko_token(facts: ChainFacts, payload: dict) -> None:
    data = payload.get("data") if isinstance(payload, dict) else None
    attrs = {}
    if isinstance(data, dict):
        attrs = data.get("attributes") or {}
    elif isinstance(payload, dict):
        attrs = payload.get("attributes") or {}
    if not isinstance(attrs, dict):
        return
    honey = attrs.get("is_honeypot")
    if honey == "yes":
        facts.honeypot = True
    elif honey == "no":
        facts.honeypot = False
    _yes_no(facts, "mint_authority_open", attrs.get("mint_authority"))
    _yes_no(facts, "freeze_authority_open", attrs.get("freeze_authority"))
    handle = normalize_handle(attrs.get("twitter_handle"))
    if handle:
        facts.twitter_handle = handle
    holders = attrs.get("holders") if isinstance(attrs.get("holders"), dict) else {}
    count = inum(holders.get("count")) if isinstance(holders, dict) else None
    if count is not None:
        facts.holder_count = count
    dist = holders.get("distribution_percentage") if isinstance(holders, dict) else None
    if isinstance(dist, dict) and dist.get("top_10") is not None:
        facts.gecko_top10_pct = fraction(dist.get("top_10"))
    if attrs.get("developer_holding_percentage") is not None:
        facts.developer_holding_pct = fraction(attrs.get("developer_holding_percentage"))


def _yes_no(facts: ChainFacts, field: str, raw: object) -> None:
    if raw == "yes":
        setattr(facts, field, True)
    elif raw == "no":
        setattr(facts, field, False)


def merge_updates(facts: ChainFacts, updates: dict) -> None:
    for note in updates.get("notes") or []:
        facts.notes.append(str(note))
    for key, value in updates.items():
        if key == "notes" or value is None:
            continue
        setattr(facts, key, value)


async def load_chain_facts(candidate: Candidate, sources: Any, etherscan_api_key: str | None) -> ChainFacts:
    facts = ChainFacts(source=candidate.chain)
    try:
        info = await sources.gecko_token_info(candidate.chain, candidate.token_address)
        if isinstance(info, dict):
            merge_gecko_token(facts, info)
    except Exception as exc:
        facts.notes.append(f"gecko token info: {exc}")
        log.info("token info %s failed: %s", candidate.option_id, exc)
    try:
        if candidate.chain == "solana":
            updates = await solana.load(sources, candidate.token_address)
        elif candidate.chain == "bsc":
            updates = await bsc.load(sources, candidate.token_address, etherscan_api_key)
        elif candidate.chain == "robinhood":
            updates = await robinhood.load(sources, candidate.token_address)
        else:
            updates = {"chain_ok": False, "notes": [f"unknown chain {candidate.chain}"]}
    except Exception as exc:
        updates = {"chain_ok": False, "notes": [f"chain load: {exc}"]}
        log.warning("chain load %s failed: %s", candidate.option_id, exc)
    merge_updates(facts, updates)
    return facts
