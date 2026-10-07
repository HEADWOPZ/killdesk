import asyncio

from killdesk.judge import (
    MOCK_MODEL,
    HttpxJev,
    JudgeValidationError,
    MockJev,
    choice_payload,
    is_versioned_model,
    score_payload,
    validate_response,
)
from killdesk.questions import HOLDER_LEVELS, questions_for


def _questions() -> dict:
    return questions_for(
        "solana",
        {"social_present": False, "symbol": "GOOD"},
    )


def _valid_answers() -> dict:
    questions = _questions()
    return {
        "rug_risk": {"type": "noul", "noul": 0.2},
        "holder_concentration_danger": score_payload(1.0, list(questions["holder_concentration_danger"]["criteria"])),
        "momentum_quality": score_payload(2.0, list(questions["momentum_quality"]["criteria"])),
    }


def test_model_must_be_a_version_string() -> None:
    assert is_versioned_model("jev-1.13.0")
    assert is_versioned_model("typesafe/jev-1.13-20260917")
    assert is_versioned_model(MOCK_MODEL)
    assert not is_versioned_model("jev-latest")
    assert not is_versioned_model("jev")
    assert not is_versioned_model("~typesafe/jev-latest")
    questions = _questions()
    try:
        validate_response({"model": "jev-latest", "answers": _valid_answers()}, questions)
    except JudgeValidationError as exc:
        assert "version" in str(exc)
    else:
        raise AssertionError("alias model should fail")


def test_choice_must_be_an_option_and_probabilities_sum() -> None:
    question = {
        "pick": {
            "type": "choice",
            "criteria": {"no_trade": "stand down", "solana:abc": "take it"},
        }
    }
    bad_choice = {
        "model": "jev-1.13.0",
        "answers": {
            "pick": {
                "type": "choice",
                "choice": "other",
                "probabilities": {"no_trade": 0.5, "solana:abc": 0.5},
                "confidence": 0.5,
            }
        },
    }
    try:
        validate_response(bad_choice, question)
    except JudgeValidationError as exc:
        assert "not in" in str(exc)
    else:
        raise AssertionError("unexpected choice should fail")

    bad_sum = {
        "model": "jev-1.13.0",
        "answers": {
            "pick": {
                "type": "choice",
                "choice": "no_trade",
                "probabilities": {"no_trade": 0.5, "solana:abc": 0.5, "extra": 0.5},
                "confidence": 0.5,
            }
        },
    }
    try:
        validate_response(bad_sum, question)
    except JudgeValidationError:
        pass
    else:
        raise AssertionError("bad probability keys should fail")

    skewed = {
        "model": "jev-1.13.0",
        "answers": {
            "pick": {
                "type": "choice",
                "choice": "no_trade",
                "probabilities": {"no_trade": 0.9, "solana:abc": 0.9},
                "confidence": 0.9,
            }
        },
    }
    try:
        validate_response(skewed, question)
    except JudgeValidationError as exc:
        assert "sum" in str(exc)
    else:
        raise AssertionError("probabilities must sum to about 1")

    ok = choice_payload(["no_trade", "solana:abc"], "no_trade")
    validated = validate_response(
        {"model": "jev-1.13.0", "answers": {"pick": ok}, "usage": {"input_tokens": 3}},
        question,
    )
    assert validated.answers["pick"]["choice"] == "no_trade"
    total = sum(validated.answers["pick"]["probabilities"].values())
    assert abs(total - 1) < 0.02


def test_score_probabilities_sum_to_one() -> None:
    payload = score_payload(2.7, HOLDER_LEVELS)
    assert abs(sum(payload["probabilities"].values()) - 1) < 1e-6
    assert 0 <= payload["score"] <= 3


def test_mock_jev_is_deterministic_and_validated() -> None:
    state = {
        "symbol": "GOOD",
        "twitter_handle": "goodcoin",
        "social_present": True,
        "buy_ratio_h1": 0.62,
        "price_change_h1_pct": 8,
        "top1_ex_largest_pct": 0.04,
        "liquidity_to_mcap": 0.12,
        "honeypot": False,
        "mint_authority_open": False,
        "freeze_authority_open": False,
    }
    questions = questions_for("solana", state)

    async def run():
        judge = MockJev()
        first = await judge.ask(state, questions)
        second = await judge.ask(state, questions)
        return first, second

    first, second = asyncio.run(run())
    assert first.model == MOCK_MODEL
    assert first.answers == second.answers
    assert "recycled_account" in first.answers
    assert first.answers["recycled_account"]["noul"] == 0.25
    assert first.usage["input_tokens"] > 0


def test_mock_flags_a_status_link_as_recycled() -> None:
    state = {
        "symbol": "HIGGS",
        "twitter_handle": "lebombjam/status/2107889648903065874",
        "social_present": True,
        "buy_ratio_h1": 0.6,
        "price_change_h1_pct": 4,
    }
    questions = questions_for("solana", state)

    async def run():
        return await MockJev().ask(state, questions)

    call = asyncio.run(run())
    assert call.answers["recycled_account"]["noul"] == 0.72


def test_httpx_client_posts_systemone_shape() -> None:
    import httpx

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": _valid_answers(),
                "usage": {"input_tokens": 12, "output_tokens": 4},
            },
        )

    questions = _questions()

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            judge = HttpxJev(
                "test-key",
                "https://api.typesafe.ai",
                client=client,
            )
            return await judge.ask({"price_usd": 1}, questions)

    call = asyncio.run(run())
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "jev-latest"
    assert "rug_risk" in seen["body"]["questions"]
    assert seen["body"]["state"]["price_usd"] == 1
    assert call.model == "jev-1.13.0"
    assert call.answers["rug_risk"]["noul"] == 0.2


def test_openrouter_base_url() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://openrouter.ai/api/v1/systemone"
        return httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.13-20260917",
                "answers": _valid_answers(),
                "usage": {"input_tokens": 4, "output_tokens": 0, "cost": 0.00001},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            judge = HttpxJev("or-key", "https://openrouter.ai/api", client=client)
            return await judge.ask({"n": 1}, _questions())

    call = asyncio.run(run())
    assert call.model == "typesafe/jev-1.13-20260917"


def test_sdk_package_imports() -> None:
    from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

    assert AsyncTypeSafeClient is not None
    assert Noul(instructions="Is it a rug?").type == "noul"
    assert Choice(instructions="Which?", criteria={"no_trade": None}).type == "choice"
    assert Score(instructions="How?", criteria=["low", "high"]).type == "score"
