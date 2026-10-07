"""Holder concentration from raw balances.

The single largest balance is usually the pool vault, so the hard kill uses
the next account. A lone balance is not excluded: there is no second account
to call the pool.
"""

from __future__ import annotations


def concentration(amounts: list[int], supply: int) -> dict[str, float]:
    if supply <= 0 or not amounts:
        return {}
    ordered = sorted((amount for amount in amounts if amount > 0), reverse=True)
    if not ordered:
        return {}

    def share(rows: list[int]) -> float:
        return sum(rows) / supply

    excluded = ordered[1:] if len(ordered) > 1 else ordered
    return {
        "top1_pct": share(ordered[:1]),
        "top5_pct": share(ordered[:5]),
        "top10_pct": share(ordered[:10]),
        "top1_ex_largest_pct": share(excluded[:1]),
        "top10_ex_largest_pct": share(excluded[:10]),
    }
