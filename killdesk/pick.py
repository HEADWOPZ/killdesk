"""The only call that sees more than one token.

The shortlist is capped before the request. `no_trade` is a result.
A code-side baseline (no model) is recorded next to the pick so shadow
rows can show where the two disagree.
"""

from __future__ import annotations

from killdesk.questions import pick_questions
from killdesk.state import public_candidate


def shortlist(states: list[dict], limit: int) -> list[dict]:
    ordered = sorted(states, key=lambda item: item.get("baseline_score") or 0, reverse=True)
    return ordered[:limit]


def baseline_choice(states: list[dict], min_score: float) -> str:
    if not states:
        return "no_trade"
    best = max(states, key=lambda item: item.get("baseline_score") or 0)
    if (best.get("baseline_score") or 0) < min_score:
        return "no_trade"
    return str(best["option_id"])


def build_pick(states: list[dict], limit: int) -> tuple[dict, dict]:
    chosen = shortlist(states, limit)
    payload = {"candidates": [public_candidate(item) for item in chosen]}
    return payload, pick_questions(payload["candidates"])
