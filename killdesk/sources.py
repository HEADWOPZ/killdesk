"""Live public APIs and a fixture source for tests.

GeckoTerminal calls share a token bucket (default 10/minute). DexScreener,
Solana, BSC, and Robinhood each have their own bucket and circuit breaker.
Robinhood requests send a browser User-Agent because the public RPC 403s
without one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from killdesk.chains.evm import pad_address
from killdesk.collect import DEX_BASE, GECKO_ACCEPT, GECKO_BASE
from killdesk.config import Settings
from killdesk.http import CircuitBreaker, SourceError, TokenBucket, request_json

log = logging.getLogger("killdesk.sources")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
DESK_UA = "KillDesk/0.1 (+https://github.com/HEADWOPZ/killdesk)"


class LiveSources:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        limits = settings.thresholds
        self.gecko_bucket = TokenBucket(limits.gecko_calls_per_minute)
        self.dex_bucket = TokenBucket(limits.dex_calls_per_minute)
        self.rpc_buckets = {
            name: TokenBucket(limits.rpc_calls_per_minute) for name in ("solana", "bsc", "robinhood")
        }
        self.breakers = {
            name: CircuitBreaker(name)
            for name in ("gecko", "dexscreener", "solana", "bsc", "robinhood", "etherscan")
        }

    async def gecko_new_pools(self, chain: str, page: int) -> dict:
        url = f"{GECKO_BASE}/networks/{chain}/new_pools"
        payload = await request_json(
            self.client,
            "GET",
            url,
            source="gecko",
            bucket=self.gecko_bucket,
            breaker=self.breakers["gecko"],
            headers={"Accept": GECKO_ACCEPT, "User-Agent": DESK_UA},
            params={"page": str(page), "include": "base_token,quote_token"},
        )
        if not isinstance(payload, dict):
            raise SourceError("gecko", "new_pools payload is not an object")
        return payload

    async def gecko_token_info(self, chain: str, address: str) -> dict:
        url = f"{GECKO_BASE}/networks/{chain}/tokens/{address}/info"
        payload = await request_json(
            self.client,
            "GET",
            url,
            source="gecko",
            bucket=self.gecko_bucket,
            breaker=self.breakers["gecko"],
            headers={"Accept": GECKO_ACCEPT, "User-Agent": DESK_UA},
        )
        if not isinstance(payload, dict):
            raise SourceError("gecko", "token info payload is not an object")
        return payload

    async def dexscreener_tokens(self, chain: str, addresses: list[str]) -> list:
        if not addresses:
            return []
        joined = ",".join(addresses[:30])
        url = f"{DEX_BASE}/tokens/v1/{chain}/{joined}"
        payload = await request_json(
            self.client,
            "GET",
            url,
            source="dexscreener",
            bucket=self.dex_bucket,
            breaker=self.breakers["dexscreener"],
            headers={"Accept": "application/json", "User-Agent": DESK_UA},
        )
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("pairs"), list):
            return payload["pairs"]
        raise SourceError("dexscreener", "unexpected payload")

    async def solana_rpc(self, method: str, params: list) -> Any:
        last: Exception | None = None
        for url in self.settings.solana_rpc_urls:
            try:
                return await self._rpc("solana", url, method, params, headers={"User-Agent": DESK_UA})
            except SourceError as exc:
                last = exc
                log.info("solana rpc %s via %s failed: %s", method, url, exc)
        raise last or SourceError("solana", "no rpc url")

    async def evm_rpc(self, chain: str, method: str, params: list) -> Any:
        if chain == "robinhood":
            url = self.settings.robinhood_rpc_url
            headers = {"User-Agent": BROWSER_UA, "Accept": "application/json", "Content-Type": "application/json"}
        elif chain == "bsc":
            url = self.settings.bsc_rpc_url
            headers = {"User-Agent": DESK_UA, "Content-Type": "application/json"}
        else:
            raise SourceError(chain, "no evm rpc")
        return await self._rpc(chain, url, method, params, headers=headers)

    async def etherscan(self, chain: str, params: dict[str, str]) -> dict:
        if chain != "bsc":
            raise SourceError("etherscan", f"unsupported chain {chain}")
        key = self.settings.etherscan_api_key
        if not key:
            raise SourceError("etherscan", "no API key")
        query = {"chainid": "56", "apikey": key, **params}
        payload = await request_json(
            self.client,
            "GET",
            "https://api.etherscan.io/v2/api",
            source="etherscan",
            bucket=self.rpc_buckets["bsc"],
            breaker=self.breakers["etherscan"],
            headers={"User-Agent": DESK_UA},
            params=query,
        )
        if not isinstance(payload, dict):
            raise SourceError("etherscan", "payload is not an object")
        message = f"{payload.get('message', '')} {payload.get('result', '')}"
        if "rate limit" in message.lower():
            raise SourceError("etherscan", "rate limit")
        return payload

    async def mark_price(self, chain: str, address: str) -> float | None:
        rows = await self.dexscreener_tokens(chain, [address])
        from killdesk.collect import index_pairs

        stats = index_pairs(chain, rows).get(address if chain == "solana" else address.lower())
        if stats is None:
            return None
        return stats.price_usd

    async def _rpc(
        self,
        source: str,
        url: str,
        method: str,
        params: list,
        *,
        headers: dict[str, str],
    ) -> Any:
        payload = await request_json(
            self.client,
            "POST",
            url,
            source=source,
            bucket=self.rpc_buckets[source],
            breaker=self.breakers[source],
            headers=headers,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if not isinstance(payload, dict):
            raise SourceError(source, "rpc payload is not an object")
        if payload.get("error"):
            err = payload["error"]
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise SourceError(source, message)
        return payload.get("result")


class FixtureSources:
    """Recorded JSON. Tests and `killdesk run --fixtures` never touch the network."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.gecko_calls = 0
        self._solana = self._optional("solana_rpc.json") or {}
        self._evm = {
            "bsc": self._optional("evm_bsc.json") or {},
            "robinhood": self._optional("evm_robinhood.json") or {},
        }
        self._prices = self._optional("prices.json") or {}
        self._etherscan: dict[str, dict] = {}
        for path in sorted(self.root.glob("etherscan_*.json")):
            address = path.stem.removeprefix("etherscan_").lower()
            self._etherscan[address] = json.loads(path.read_text(encoding="utf-8"))

    def _optional(self, name: str) -> Any:
        path = self.root / name
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _read(self, name: str, source: str) -> Any:
        path = self.root / name
        if not path.exists():
            raise SourceError(source, f"missing fixture {name}")
        return json.loads(path.read_text(encoding="utf-8"))

    async def gecko_new_pools(self, chain: str, page: int) -> dict:
        self.gecko_calls += 1
        name = f"gecko_{chain}_p{page}.json"
        path = self.root / name
        if not path.exists():
            if page > 1:
                return {"data": [], "included": []}
            raise SourceError("gecko", f"missing fixture {name}")
        return json.loads(path.read_text(encoding="utf-8"))

    async def gecko_token_info(self, chain: str, address: str) -> dict:
        self.gecko_calls += 1
        return self._read(f"token_{chain}_{address}.json", "gecko")

    async def dexscreener_tokens(self, chain: str, addresses: list[str]) -> list:
        rows = self._read(f"dex_{chain}.json", "dexscreener")
        if not isinstance(rows, list):
            raise SourceError("dexscreener", "fixture is not a list")
        wanted = {item if chain == "solana" else item.lower() for item in addresses}
        kept = []
        for row in rows:
            base = ((row.get("baseToken") or {}).get("address")) if isinstance(row, dict) else None
            if not base:
                continue
            key = base if chain == "solana" else str(base).lower()
            if key in wanted:
                kept.append(row)
        return kept

    async def solana_rpc(self, method: str, params: list) -> dict:
        mint = params[0]
        row = self._solana.get(mint)
        if not isinstance(row, dict):
            raise SourceError("solana", f"no fixture for {mint}")
        if method == "getAccountInfo":
            return {
                "context": {"slot": 1},
                "value": {
                    "data": {
                        "parsed": {"type": "mint", "info": row["account"]},
                        "program": "spl-token",
                    }
                },
            }
        if method == "getTokenSupply":
            return {"context": {"slot": 1}, "value": row["supply"]}
        if method == "getTokenLargestAccounts":
            return {"context": {"slot": 1}, "value": row["largest"]}
        raise SourceError("solana", method)

    async def evm_rpc(self, chain: str, method: str, params: list) -> Any:
        if method == "eth_chainId":
            return "0x1237" if chain == "robinhood" else "0x38"
        table = self._evm.get(chain) or {}
        if method == "eth_getCode":
            address = str(params[0]).lower()
            row = table.get(address)
            if not isinstance(row, dict):
                raise SourceError(chain, f"no fixture for {address}")
            return row.get("code", "0x")
        if method == "eth_call":
            call = params[0]
            address = str(call["to"]).lower()
            row = table.get(address)
            if not isinstance(row, dict):
                raise SourceError(chain, f"no fixture for {address}")
            data = str(call.get("data") or "")
            if data.startswith("0x8da5cb5b"):
                return pad_address(str(row.get("owner") or ("0x" + "0" * 40)))
            if data.startswith("0x18160ddd"):
                return row.get("total_supply") or "0x0"
            raise SourceError(chain, f"unknown call {data}")
        raise SourceError(chain, method)

    async def etherscan(self, chain: str, params: dict[str, str]) -> dict:
        address = str(params.get("address") or "").lower()
        body = self._etherscan.get(address)
        if body is None:
            raise SourceError("etherscan", f"no fixture for {address}")
        if params.get("action") == "tokenholderlist" and "holders" in body:
            return body["holders"]
        if params.get("action") == "getsourcecode" and "source" in body:
            return body["source"]
        return body

    async def mark_price(self, chain: str, address: str) -> float | None:
        key = f"{chain}:{address if chain == 'solana' else address.lower()}"
        raw = self._prices.get(key)
        if raw is None:
            return None
        return float(raw)
