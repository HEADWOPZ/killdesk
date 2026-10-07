"""Order path. This MVP does not implement it and cannot be turned on.

Shadow fills are rows in the book. Nothing here talks to a wallet.
"""

from __future__ import annotations

from pydantic import BaseModel


class ShadowOrder(BaseModel):
    chain: str
    symbol: str
    token_address: str
    side: str
    price_usd: float | None = None
    paper_size_usd: float | None = None


class Executor:
    def __init__(self, shadow: bool = True) -> None:
        if not shadow:
            raise NotImplementedError(
                "KillDesk MVP is shadow-only. Refusing to construct a live executor."
            )
        self.shadow = True

    def submit(self, order: ShadowOrder) -> None:
        raise NotImplementedError(
            "No order execution in this MVP. Shadow fills are written to desk.db only."
        )
