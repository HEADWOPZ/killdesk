"""Robinhood Chain (id 4663) via its public RPC.

The RPC returns 403 unless the request carries a browser-like User-Agent.
That header is set by the live source, not by this parser.
"""

from __future__ import annotations

import logging
from typing import Any

from killdesk.chains.evm import (
    OWNER_SELECTOR,
    ZERO_ADDRESS,
    code_is_empty,
    code_size,
    decode_address,
)

log = logging.getLogger("killdesk.robinhood")

CHAIN_ID = 4663


async def load(sources: Any, address: str) -> dict:
    notes: list[str] = []
    out: dict = {"chain_ok": False, "etherscan_skipped": True, "notes": notes}
    try:
        chain_id = await sources.evm_rpc("robinhood", "eth_chainId", [])
        if isinstance(chain_id, str) and int(chain_id, 16) != CHAIN_ID:
            notes.append(f"unexpected chain id {chain_id}")
    except Exception as exc:
        notes.append(f"eth_chainId: {exc}")
        log.info("robinhood chain id failed: %s", exc)
    try:
        code = await sources.evm_rpc("robinhood", "eth_getCode", [address, "latest"])
        out["no_contract"] = code_is_empty(code)
        out["code_bytes"] = code_size(code)
        out["chain_ok"] = True
    except Exception as exc:
        notes.append(f"eth_getCode: {exc}")
        log.info("robinhood code failed: %s", exc)
    owner = await _owner(sources, address, notes)
    if owner is not None:
        out["owner_open"] = owner != ZERO_ADDRESS
        out["chain_ok"] = True
    if not out["chain_ok"]:
        notes.append("robinhood rpc unavailable")
    return out


async def _owner(sources: Any, address: str, notes: list[str]) -> str | None:
    try:
        result = await sources.evm_rpc(
            "robinhood",
            "eth_call",
            [{"to": address, "data": OWNER_SELECTOR}, "latest"],
        )
    except Exception as exc:
        notes.append(f"owner: {exc}")
        return None
    return decode_address(result)
