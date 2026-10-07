from killdesk.pick import baseline_choice, build_pick, shortlist
from killdesk.questions import SETS, pick_questions, questions_for


def test_question_sets_are_per_chain_and_skip_social_without_a_handle() -> None:
    quiet = {"social_present": False, "symbol": "GOOD"}
    social = {"social_present": True, "symbol": "GOOD", "twitter_handle": "goodcoin"}
    for chain in ("solana", "bsc", "robinhood"):
        bare = questions_for(chain, quiet)
        assert "recycled_account" not in bare
        assert set(bare) == {"rug_risk", "holder_concentration_danger", "momentum_quality"}
        with_social = questions_for(chain, social)
        assert "recycled_account" in with_social
        assert with_social["rug_risk"]["type"] == "noul"
        assert with_social["holder_concentration_danger"]["type"] == "score"
    assert "Solana" in questions_for("solana", quiet)["rug_risk"]["instructions"]
    assert "BSC" in questions_for("bsc", quiet)["rug_risk"]["instructions"]
    assert "4663" in questions_for("robinhood", quiet)["rug_risk"]["instructions"]
    assert set(SETS) == {"solana", "bsc", "robinhood"}


def test_pick_always_offers_no_trade_and_caps_the_shortlist() -> None:
    states = [
        {"option_id": f"solana:{index}", "symbol": "T", "chain": "solana", "baseline_score": index}
        for index in range(12)
    ]
    chosen = shortlist(states, 10)
    assert len(chosen) == 10
    assert chosen[0]["option_id"] == "solana:11"
    payload, questions = build_pick(states, 10)
    assert len(payload["candidates"]) == 10
    criteria = questions["selection"]["criteria"]
    assert "no_trade" in criteria
    assert len(criteria) == 11
    assert pick_questions(payload["candidates"])["selection"]["type"] == "choice"
    assert baseline_choice(chosen, min_score=1) == "solana:11"
    assert baseline_choice([{"option_id": "solana:1", "baseline_score": 0.1}], min_score=1) == "no_trade"
    assert baseline_choice([], min_score=1) == "no_trade"
