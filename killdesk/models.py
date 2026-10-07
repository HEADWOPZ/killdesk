"""Desk records. Code fills every numeric field before a judge call."""

from __future__ import annotations

from pydantic import BaseModel, Field

CHAINS: tuple[str, ...] = ("solana", "bsc", "robinhood")

# Quote assets we do not treat as the thing being launched.
MAJORS: dict[str, frozenset[str]] = {
    "solana": frozenset(
        {
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        }
    ),
    "bsc": frozenset(
        {
            "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
            "0x55d398326f99059ff775485246999027b3197955",
            "0xe9e7cea3dedca5984780bafc599bd69add087d56",
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
        }
    ),
    "robinhood": frozenset(),
}


def canonical_address(chain: str, address: str) -> str:
    text = address.strip()
    if chain == "solana":
        return text
    return text.lower()


def option_id(chain: str, address: str) -> str:
    return f"{chain}:{canonical_address(chain, address)}"


def is_major(chain: str, address: str | None) -> bool:
    if not address:
        return False
    return canonical_address(chain, address) in MAJORS.get(chain, frozenset())


class TradeStats(BaseModel):
    price_usd: float | None = None
    liquidity_usd: float | None = None
    volume_h1: float | None = None
    market_cap_usd: float | None = None
    buys_m5: int = 0
    sells_m5: int = 0
    buys_h1: int = 0
    sells_h1: int = 0
    price_change_m5: float | None = None
    price_change_h1: float | None = None
    pair_address: str | None = None
    dex: str | None = None


class ChainFacts(BaseModel):
    source: str
    chain_ok: bool = False
    mint_authority_open: bool | None = None
    freeze_authority_open: bool | None = None
    owner_open: bool | None = None
    honeypot: bool | None = None
    no_contract: bool = False
    source_verified: bool | None = None
    etherscan_skipped: bool = False
    supply_raw: str | None = None
    top1_pct: float | None = None
    top5_pct: float | None = None
    top10_pct: float | None = None
    top1_ex_largest_pct: float | None = None
    top10_ex_largest_pct: float | None = None
    holder_count: int | None = None
    gecko_top10_pct: float | None = None
    developer_holding_pct: float | None = None
    twitter_handle: str | None = None
    code_bytes: int | None = None
    notes: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    chain: str
    pool_address: str
    token_address: str
    symbol: str
    name: str
    price_usd: float | None = None
    fdv_usd: float | None = None
    market_cap_usd: float | None = None
    reserve_usd: float | None = None
    volume_h1: float | None = None
    price_change_m5: float | None = None
    price_change_h1: float | None = None
    buys_m5: int = 0
    sells_m5: int = 0
    buys_h1: int = 0
    sells_h1: int = 0
    buyers_h1: int = 0
    sellers_h1: int = 0
    age_minutes: float | None = None
    quote_symbol: str | None = None
    dex: str | None = None
    trade: TradeStats | None = None
    chain_facts: ChainFacts | None = None

    @property
    def option_id(self) -> str:
        return option_id(self.chain, self.token_address)
