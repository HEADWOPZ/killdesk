"""BSC chain facts.

Public RPC covers code and `owner()`. An optional Etherscan V2 key
(`chainid=56`) adds a source-marker honeypot check and, when the plan
allows it, a holder list. With no key those steps are skipped and the
token is not killed for the missing key.
"""

from __future__ import annotations

import logging
from typing import Any

from killdesk.chains.evm import OWNER_SELECTOR, ZERO_ADDRESS, code_is_empty, decode_address
from killdesk.chains.holders import concentration

log = logging.getLogger("killdesk.bsc")

# High-confidence source markers. A comment that merely says "honeypot" is
# not enough; these are function or mapping names used to block sells.
HONEYPOT_MARKERS = (
    "isblacklisted",
    "addblacklist",
    "setblacklist",
    "_blacklist(",
    "blacklistaddress",
    "function addbot",
    "isbot[",
    "function setbots",
)


def source_is_honeypot(source: str) -> bool:
    lowered = source.lower()
    return any(marker in lowered for marker in HONEYPOT_MARKERS)


def extract_source(body: dict) -> tuple[str, bool]:
    result = body.get("result")
    row: dict = {}
    if isinstance(result, list) and result and isinstance(result[0], dict):
        row = result[0]
    elif isinstance(result, dict):
        row = result
    source = row.get("SourceCode") or ""
    abi = str(row.get("ABI") or "")
    verified = bool(source.strip()) and "not verified" not in abi.lower()
    if not verified:
        return "", False
    return str(source), True


async def load(sources: Any, address: str, api_key: str | None) -> dict:
    notes: list[str] = []
    out: dict = {"etherscan_skipped": not api_key, "chain_ok": False, "notes": notes}
    try:
        code = await sources.evm_rpc("bsc", "eth_getCode", [address, "latest"])
        out["no_contract"] = code_is_empty(code)
        out["chain_ok"] = True
    except Exception as exc:
        notes.append(f"bsc code: {exc}")
        log.info("bsc code failed: %s", exc)
    owner = await _owner(sources, address, notes)
    if owner is not None:
        out["owner_open"] = owner != ZERO_ADDRESS
        out["chain_ok"] = True
    if not api_key:
        notes.append("etherscan skipped: no ETHERSCAN_API_KEY")
        out["chain_ok"] = True
        return out
    try:
        body = await sources.etherscan(
            "bsc",
            {
                "module": "contract",
                "action": "getsourcecode",
                "address": address,
            },
        )
        source, verified = extract_source(body if isinstance(body, dict) else {})
        out["source_verified"] = verified
        if verified:
            out["honeypot"] = source_is_honeypot(source)
        out["chain_ok"] = True
    except Exception as exc:
        notes.append(f"etherscan source: {exc}")
        log.info("etherscan source failed: %s", exc)
    try:
        holders = await sources.etherscan(
            "bsc",
            {
                "module": "token",
                "action": "tokenholderlist",
                "address": address,
                "page": "1",
                "offset": "20",
            },
        )
        parsed = _holders(holders if isinstance(holders, dict) else {})
        if parsed:
            out.update(parsed)
            out["chain_ok"] = True
    except Exception as exc:
        notes.append(f"etherscan holders: {exc}")
        log.info("etherscan holders skipped: %s", exc)
    return out


async def _owner(sources: Any, address: str, notes: list[str]) -> str | None:
    try:
        result = await sources.evm_rpc(
            "bsc",
            "eth_call",
            [{"to": address, "data": OWNER_SELECTOR}, "latest"],
        )
    except Exception as exc:
        notes.append(f"owner: {exc}")
        return None
    return decode_address(result)


def _holders(body: dict) -> dict:
    if str(body.get("status")) != "1":
        return {}
    rows = body.get("result")
    if not isinstance(rows, list):
        return {}
    amounts: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("TokenHolderQuantity") or row.get("value")
        try:
            amounts.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not amounts:
        return {}
    supply = sum(amounts)
    facts = concentration(amounts, supply)
    facts["holder_count"] = len(rows)
    return facts
