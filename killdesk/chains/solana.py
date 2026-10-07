"""Solana chain_kill facts from the public JSON-RPC.

`getAccountInfo` (jsonParsed) supplies mint and freeze authority.
`getTokenSupply` and `getTokenLargestAccounts` supply concentration.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("killdesk.solana")


async def load(sources: Any, mint: str) -> dict:
    notes: list[str] = []
    account = await _call(sources, "getAccountInfo", [mint, {"encoding": "jsonParsed"}], notes)
    supply_res = await _call(sources, "getTokenSupply", [mint], notes)
    largest_res = await _call(sources, "getTokenLargestAccounts", [mint], notes)
    if account is None and supply_res is None and largest_res is None:
        return {"chain_ok": False, "notes": notes or ["solana rpc unavailable"]}

    out: dict = {"chain_ok": True, "notes": notes}
    info = _mint_info(account)
    if info is None and account is not None:
        value = account.get("value") if isinstance(account, dict) else None
        if value is None:
            out["no_contract"] = True
        else:
            out["no_contract"] = True
            notes.append("account is not an SPL mint")
    elif info is not None:
        out["no_contract"] = False
        out["mint_authority_open"] = bool(info.get("mintAuthority"))
        out["freeze_authority_open"] = bool(info.get("freezeAuthority"))
        if info.get("supply") is not None:
            out["supply_raw"] = str(info.get("supply"))

    supply = _supply_amount(supply_res)
    if supply is None and info is not None and info.get("supply") is not None:
        try:
            supply = int(info["supply"])
        except (TypeError, ValueError):
            supply = None
    if supply is not None:
        out["supply_raw"] = str(supply)
    amounts = _largest_amounts(largest_res)
    if supply and amounts:
        from killdesk.chains.holders import concentration

        out.update(concentration(amounts, supply))
    return out


async def _call(sources: Any, method: str, params: list, notes: list[str]) -> dict | None:
    try:
        result = await sources.solana_rpc(method, params)
    except Exception as exc:
        notes.append(f"{method}: {exc}")
        log.info("solana %s failed: %s", method, exc)
        return None
    if not isinstance(result, dict):
        notes.append(f"{method}: empty result")
        return None
    return result


def _mint_info(result: dict | None) -> dict | None:
    if not result:
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if not isinstance(data, dict):
        return None
    parsed = data.get("parsed") or {}
    if parsed.get("type") != "mint":
        return None
    info = parsed.get("info")
    return info if isinstance(info, dict) else None


def _supply_amount(result: dict | None) -> int | None:
    if not result:
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    raw = value.get("amount")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _largest_amounts(result: dict | None) -> list[int]:
    if not result:
        return []
    rows = result.get("value")
    if not isinstance(rows, list):
        return []
    amounts: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            amounts.append(int(row["amount"]))
        except (KeyError, TypeError, ValueError):
            continue
    return amounts
