"""Per-chain Jev question sets.

One call carries the whole set for a token. Splitting questions into separate
calls would resend the same state and multiply the input-token bill. The
recycled-account question is added only when a social handle is already on
the state. No handle is a gap, not a pass and not a search.
"""

from __future__ import annotations

HOLDER_LEVELS: list[str] = [
    "Dispersed. top1_ex_largest_pct is small. No non-LP wallet can empty the pool.",
    "Moderate. A few wallets are notable. top1_ex_largest_pct is under about 0.10.",
    "Elevated. One wallet can move price. top1_ex_largest_pct is roughly 0.10 to 0.25.",
    "Dangerous. A single non-LP wallet can dump through the pool.",
]

MOMENTUM_LEVELS: list[str] = [
    "Dead or one-sided selling. buy_ratio_h1 is weak and price_change_h1_pct is deeply negative.",
    "Fragile. Some buys, but buy_ratio_m5 does not show buyers returning.",
    "Decent. Buys roughly match sells and price_change_h1_pct is not a collapse.",
    "Strong. buy_ratio_h1 is healthy, the 5-minute window agrees, and price is holding.",
]


def _common(chain_note: str, social: bool) -> dict[str, dict]:
    questions: dict[str, dict] = {
        "rug_risk": {
            "type": "noul",
            "instructions": (
                "Is this token likely a rug, meaning the creator can mint, freeze, "
                "or dump into the pool? Every number is already a field on state. "
                "Do not recompute them and do not invent missing data. " + chain_note
            ),
            "criteria": {
                "true": "Open authority, honeypot true, or extreme non-LP concentration.",
                "false": "Authorities closed, honeypot is not true, concentration is not extreme.",
            },
        },
        "holder_concentration_danger": {
            "type": "score",
            "instructions": (
                "How dangerous is holder concentration? "
                "top1_ex_largest_pct and top10_ex_largest_pct are fractions of supply "
                "and already exclude the single largest account, which is usually the pool. "
                "0.10 means 10 percent. Score the fields. Do not recompute them. "
                "Null concentration is unknown, not proof of danger."
            ),
            "criteria": list(HOLDER_LEVELS),
        },
        "momentum_quality": {
            "type": "score",
            "instructions": (
                "How healthy is momentum? buy_ratio_h1, buy_ratio_m5, and "
                "price_change_h1_pct are already computed. price_change fields are "
                "percent points, so -40 means down 40 percent. Do not recompute them."
            ),
            "criteria": list(MOMENTUM_LEVELS),
        },
    }
    if social:
        questions["recycled_account"] = {
            "type": "noul",
            "instructions": (
                "Does twitter_handle look recycled relative to this token? "
                "The handle and age_minutes are the only social facts in state. "
                "There is no account timeline. A status URL, or a handle that is "
                "only a post id, leans yes. If the fields are not enough to tell, stay near 0.5."
            ),
            "criteria": {
                "true": "Handle looks reused or is only a status link, not the project's account.",
                "false": "Nothing in state says the handle predates or is unrelated to this token.",
            },
        }
    return questions


def solana_questions(state: dict) -> dict[str, dict]:
    return _common(
        "On Solana, mint_authority_open and freeze_authority_open are the authority facts.",
        bool(state.get("social_present")),
    )


def bsc_questions(state: dict) -> dict[str, dict]:
    return _common(
        "On BSC, honeypot and owner_open are the authority facts. "
        "etherscan_skipped means verified source was not fetched. "
        "Do not treat a skipped source as a honeypot.",
        bool(state.get("social_present")),
    )


def robinhood_questions(state: dict) -> dict[str, dict]:
    return _common(
        "On Robinhood Chain (chain id 4663), owner_open is the authority fact. "
        "Holder fields may be null. Null is unknown, not dangerous by itself.",
        bool(state.get("social_present")),
    )


SETS = {
    "solana": solana_questions,
    "bsc": bsc_questions,
    "robinhood": robinhood_questions,
}


def questions_for(chain: str, state: dict) -> dict[str, dict]:
    builder = SETS.get(chain)
    if builder is None:
        raise KeyError(f"no question set for {chain}")
    return builder(state)


def pick_questions(candidates: list[dict]) -> dict[str, dict]:
    """One choice over the shortlist. `no_trade` is always an option."""
    criteria: dict[str, str] = {
        "no_trade": (
            "Open no shadow position. Correct when no candidate is clearly worth "
            "holding, or when the edge is ambiguous."
        )
    }
    for item in candidates:
        criteria[str(item["option_id"])] = (
            f"{item.get('symbol')} on {item.get('chain')} "
            f"mcap_usd={item.get('mcap_usd')} age_minutes={item.get('age_minutes')} "
            f"liquidity_usd={item.get('liquidity_usd')} buy_ratio_h1={item.get('buy_ratio_h1')} "
            f"rug_risk={item.get('rug_risk')} "
            f"momentum_quality={item.get('momentum_quality')} "
            f"holder_concentration_danger={item.get('holder_concentration_danger')} "
            f"honeypot={item.get('honeypot')} mint_authority_open={item.get('mint_authority_open')}"
        )
    return {
        "selection": {
            "type": "choice",
            "instructions": (
                "Choose one option_id to shadow-trade, or no_trade. "
                "Every number you need is already on each candidate in state. "
                "Do not assume a trade is required. no_trade is a normal outcome."
            ),
            "criteria": criteria,
        }
    }
