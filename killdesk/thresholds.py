"""Every tunable number on the desk.

Edit this file to retune. Filters, the bench, the exit rule, and the
GeckoTerminal budget all read `Thresholds` and nothing else. These defaults
are a starting shape, not a strategy. Run shadow mode and move the numbers
where you disagree with the log.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# None means the bench entry never expires.
_REJECTION_TTL_SECONDS: dict[str, int | None] = {
    "honeypot": None,
    "mint_authority_open": None,
    "freeze_authority_open": None,
    "no_contract": None,
    "owner_not_renounced": 12 * 3600,
    "too_early": 3600,
    "missing_age": 3600,
    "too_old": 24 * 3600,
    "low_liquidity": 6 * 3600,
    "missing_mcap": 6 * 3600,
    "mcap_out_of_band": 6 * 3600,
    "low_volume": 2 * 3600,
    "low_tx_count": 2 * 3600,
    "no_dex_pair": 3600,
    "dex_unavailable": 15 * 60,
    "sell_pressure": 2 * 3600,
    "no_buyer_return": 3600,
    "price_dump": 3 * 3600,
    "already_extended": 2 * 3600,
    "holder_concentration": 6 * 3600,
    "chain_data_unavailable": 15 * 60,
    "rug_risk": 3 * 3600,
    "holder_concentration_danger": 6 * 3600,
    "weak_momentum": 3600,
    "recycled_account": 12 * 3600,
    "judge_unavailable": 15 * 60,
}


@dataclass(frozen=True)
class Thresholds:
    # free_kill — listing fields only, no network.
    min_age_minutes: float = 30
    max_age_minutes: float = 48 * 60
    min_liquidity_usd: float = 8_000
    min_mcap_usd: float = 20_000
    max_mcap_usd: float = 8_000_000
    min_volume_h1_usd: float = 1_500
    min_tx_h1: int = 25

    # trade_kill — one DexScreener batch per chain.
    min_dex_tx_h1: int = 25
    min_buy_ratio_h1: float = 0.45
    min_m5_tx_for_return: int = 4
    min_m5_buy_ratio: float = 0.35
    max_dump_h1_pct: float = -40
    max_extension_h1_pct: float = 250

    # chain_kill — plain facts. Comparisons, not model questions.
    # Solana mint/freeze stay hard kills. EVM owner is on by default;
    # loosen kill_open_owner first if you trade launches that still have an owner.
    kill_open_owner: bool = True
    max_top1_ex_largest_pct: float = 0.25
    max_dossiers: int = 3

    # soft_kill — thresholds on Jev's typed answers.
    rug_risk_max: float = 0.70
    holder_danger_max: float = 2.6
    momentum_min: float = 1.2
    recycled_account_max: float = 0.70

    # pick — code still decides after the choice comes back.
    max_shortlist: int = 10
    min_pick_confidence: float = 0.35
    baseline_min_score: float = 1.0
    mock_pick_min_edge: float = 0.8

    # shadow exit. Percents are fractions: 0.25 means +25%.
    take_profit_pct: float = 0.25
    stop_loss_pct: float = 0.15
    max_hold_minutes: float = 180
    paper_size_usd: float = 100

    # Public-API politeness. GeckoTerminal's published cap is 30/min;
    # the desk budgets 10 so listing calls leave room for dossiers.
    gecko_calls_per_minute: float = 10
    dex_calls_per_minute: float = 60
    rpc_calls_per_minute: float = 30

    default_rejection_ttl_seconds: int = 3600
    rejection_ttl_seconds: dict[str, int | None] = field(
        default_factory=lambda: dict(_REJECTION_TTL_SECONDS)
    )

    def ttl(self, reason: str) -> int | None:
        if reason in self.rejection_ttl_seconds:
            return self.rejection_ttl_seconds[reason]
        return self.default_rejection_ttl_seconds


DEFAULTS = Thresholds()
