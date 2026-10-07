"""Jev client.

The official Python SDK (`typesafe-sdk`) is used when a key is configured.
A thin httpx client speaks the same `POST /v1/systemone` shape and is the
fallback, including the OpenRouter base URL `https://openrouter.ai/api`.
`MockJev` is deterministic so the desk and the tests run with no key.

Validation, applied to every backend:

- a choice is one of the options that were asked
- probabilities sum to about 1
- `model` is a version string, not an alias such as `jev-latest`
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from killdesk.thresholds import DEFAULTS, Thresholds

log = logging.getLogger("killdesk.judge")

_VERSION = re.compile(r"\d+\.\d+")
_ALIASES = frozenset(
    {
        "jev",
        "jev-latest",
        "typesafe/jev",
        "typesafe/jev-latest",
        "~typesafe/jev-latest",
        "typesafe-ai/jev",
    }
)
_PROB_TOLERANCE = 0.02
MOCK_MODEL = "jev-mock-1.0.0"


class JudgeError(Exception):
    pass


class JudgeValidationError(JudgeError):
    pass


class JudgeTransportError(JudgeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


@dataclass(frozen=True)
class JudgeCall:
    model: str
    answers: dict[str, dict[str, Any]]
    usage: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "answers": self.answers, "usage": self.usage}


class Judge(Protocol):
    async def ask(self, state: dict[str, Any], questions: dict[str, dict]) -> JudgeCall: ...

    async def aclose(self) -> None: ...


def is_versioned_model(model: str) -> bool:
    text = model.strip()
    if not text or text.lower() in _ALIASES:
        return False
    return _VERSION.search(text) is not None


def validate_response(payload: dict[str, Any], questions: dict[str, dict]) -> JudgeCall:
    if not isinstance(payload, dict):
        raise JudgeValidationError("response is not an object")
    model = payload.get("model")
    if not isinstance(model, str) or not is_versioned_model(model):
        raise JudgeValidationError(f"model must be a version string, got {model!r}")
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise JudgeValidationError("answers missing")
    missing = [name for name in questions if name not in answers]
    if missing:
        raise JudgeValidationError(f"missing answers: {missing}")
    cleaned: dict[str, dict[str, Any]] = {}
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict):
            raise JudgeValidationError(f"{name} answer is not an object")
        kind = question.get("type")
        if answer.get("type") != kind:
            raise JudgeValidationError(f"{name} type {answer.get('type')!r} != {kind!r}")
        cleaned[name] = _validate_answer(name, kind, question, answer)
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return JudgeCall(model=model, answers=cleaned, usage=dict(usage))


def _validate_answer(name: str, kind: str, question: dict, answer: dict) -> dict[str, Any]:
    if kind == "noul":
        try:
            probability = float(answer["noul"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JudgeValidationError(f"{name} noul is not a float") from exc
        if not 0.0 <= probability <= 1.0:
            raise JudgeValidationError(f"{name} noul out of range: {probability}")
        return {"type": "noul", "noul": probability}
    if kind == "choice":
        options = set(question.get("criteria") or {})
        choice = answer.get("choice")
        if choice not in options:
            raise JudgeValidationError(f"{name} choice {choice!r} not in {sorted(options)}")
        probs = answer.get("probabilities")
        if not isinstance(probs, dict) or set(probs) != options:
            raise JudgeValidationError(f"{name} probabilities must cover every option")
        _probability_sum(name, probs)
        confidence = _optional_unit(name, answer.get("confidence"))
        if confidence is None:
            confidence = float(probs[choice])
        return {
            "type": "choice",
            "choice": choice,
            "probabilities": {str(key): float(value) for key, value in probs.items()},
            "confidence": confidence,
        }
    if kind == "score":
        levels = question.get("criteria") or []
        try:
            score = float(answer["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JudgeValidationError(f"{name} score is not a float") from exc
        if levels and not 0.0 <= score <= (len(levels) - 1) + 1e-6:
            raise JudgeValidationError(f"{name} score {score} outside rubric")
        probs = answer.get("probabilities")
        if not isinstance(probs, dict) or not probs:
            raise JudgeValidationError(f"{name} probabilities missing")
        _probability_sum(name, probs)
        confidence = _optional_unit(name, answer.get("confidence"))
        legend = answer.get("legend") if isinstance(answer.get("legend"), dict) else {}
        return {
            "type": "score",
            "score": score,
            "probabilities": {str(k): float(v) for k, v in probs.items()},
            "legend": {str(k): v for k, v in legend.items()},
            "confidence": confidence,
        }
    raise JudgeValidationError(f"{name} has unknown type {kind}")


def _probability_sum(name: str, probs: dict) -> float:
    try:
        values = [float(v) for v in probs.values()]
    except (TypeError, ValueError) as exc:
        raise JudgeValidationError(f"{name} probabilities are not floats") from exc
    if any(v < -1e-6 for v in values):
        raise JudgeValidationError(f"{name} has a negative probability")
    total = sum(values)
    if abs(total - 1.0) > _PROB_TOLERANCE:
        raise JudgeValidationError(f"{name} probabilities sum to {total:.4f}, not 1")
    return total


def _optional_unit(name: str, value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise JudgeValidationError(f"{name} confidence is not a float") from exc
    if not 0.0 <= number <= 1.0:
        raise JudgeValidationError(f"{name} confidence out of range")
    return number


def _exact_probs(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        share = 1.0 / len(weights)
        weights = {key: share for key in weights}
        total = 1.0
    keys = list(weights)
    probs: dict[str, float] = {}
    acc = 0.0
    for key in keys[:-1]:
        probs[key] = round(weights[key] / total, 8)
        acc += probs[key]
    probs[keys[-1]] = round(1.0 - acc, 8)
    return probs


def score_payload(value: float, levels: list[str]) -> dict[str, Any]:
    count = len(levels)
    if count < 2:
        raise JudgeValidationError("score rubric needs at least two levels")
    max_index = count - 1
    clamped = max(0.0, min(float(max_index), float(value)))
    lo = int(math.floor(clamped + 1e-12))
    hi = min(max_index, lo + 1)
    frac = 0.0 if lo == hi else clamped - lo
    raw = {str(i): 0.0 for i in range(count)}
    if lo == hi:
        raw[str(lo)] = 1.0
    else:
        raw[str(lo)] = 1.0 - frac
        raw[str(hi)] = frac
    probs = _exact_probs(raw)
    return {
        "type": "score",
        "score": round(clamped, 6),
        "confidence": max(probs.values()),
        "legend": {str(i): levels[i] for i in range(count)},
        "probabilities": probs,
    }


def choice_payload(options: list[str], selected: str, peak: float = 0.72) -> dict[str, Any]:
    if selected not in options:
        raise JudgeValidationError(f"mock selected {selected!r} outside options")
    peak = min(0.99, max(0.5, peak))
    others = [option for option in options if option != selected]
    raw = {selected: peak}
    share = (1.0 - peak) / len(others) if others else 0.0
    for option in others:
        raw[option] = share
    if not others:
        raw[selected] = 1.0
    probs = _exact_probs(raw)
    return {
        "type": "choice",
        "choice": selected,
        "confidence": probs[selected],
        "probabilities": probs,
    }


def _usage(state: dict, questions: dict) -> dict[str, int]:
    raw = json.dumps({"state": state, "questions": questions}, default=str)
    return {"input_tokens": max(1, len(raw) // 4), "output_tokens": 0}


class MockJev:
    """Deterministic heuristics over numbers the desk already computed.

    The shape matches a real Jev response, including a versioned model id,
    so validation and the book see the same fields they would live.
    """

    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.thresholds = thresholds or DEFAULTS

    async def aclose(self) -> None:
        return None

    async def ask(self, state: dict[str, Any], questions: dict[str, dict]) -> JudgeCall:
        if "selection" in questions:
            answers = {"selection": self._pick(state, questions["selection"])}
        else:
            answers = {}
            if "rug_risk" in questions:
                answers["rug_risk"] = {"type": "noul", "noul": round(self._rug(state), 6)}
            if "holder_concentration_danger" in questions:
                levels = list(questions["holder_concentration_danger"]["criteria"])
                answers["holder_concentration_danger"] = score_payload(self._holder(state), levels)
            if "momentum_quality" in questions:
                levels = list(questions["momentum_quality"]["criteria"])
                answers["momentum_quality"] = score_payload(self._momentum(state), levels)
            if "recycled_account" in questions:
                answers["recycled_account"] = {
                    "type": "noul",
                    "noul": round(self._recycled(state), 6),
                }
        payload = {"model": MOCK_MODEL, "answers": answers, "usage": _usage(state, questions)}
        return validate_response(payload, questions)

    def _rug(self, state: dict[str, Any]) -> float:
        risk = 0.15
        if state.get("mint_authority_open"):
            risk += 0.45
        if state.get("freeze_authority_open"):
            risk += 0.25
        if state.get("honeypot"):
            risk += 0.70
        if state.get("owner_open"):
            risk += 0.20
        conc = state.get("top1_ex_largest_pct")
        if isinstance(conc, (int, float)):
            risk += min(0.40, float(conc))
        ratio = state.get("liquidity_to_mcap")
        if isinstance(ratio, (int, float)) and ratio < 0.02:
            risk += 0.15
        return max(0.0, min(1.0, risk))

    def _holder(self, state: dict[str, Any]) -> float:
        conc = state.get("top1_ex_largest_pct")
        if not isinstance(conc, (int, float)):
            return 1.0
        conc = float(conc)
        if conc < 0.05:
            return 0.2
        if conc < 0.12:
            return 1.0
        if conc < 0.25:
            return 2.0
        return 2.8

    def _momentum(self, state: dict[str, Any]) -> float:
        ratio = state.get("buy_ratio_h1")
        ratio = float(ratio) if isinstance(ratio, (int, float)) else 0.0
        change = state.get("price_change_h1_pct")
        score = 1.5 if ratio >= 0.55 else 1.0 if ratio >= 0.45 else 0.3
        if isinstance(change, (int, float)):
            change = float(change)
            if -10 <= change <= 80:
                score += 1.2
            elif change > 80:
                score += 0.6
            else:
                score += 0.2
        return max(0.0, min(3.0, score))

    def _recycled(self, state: dict[str, Any]) -> float:
        handle = str(state.get("twitter_handle") or "").lower()
        symbol = str(state.get("symbol") or "").lower()
        if re.search(r"status/\d+", handle):
            return 0.72
        if symbol and symbol in handle:
            return 0.25
        return 0.40

    def _pick(self, state: dict[str, Any], question: dict) -> dict[str, Any]:
        options = list(question["criteria"])
        scored: list[tuple[float, str]] = []
        for candidate in state.get("candidates") or []:
            oid = str(candidate.get("option_id"))
            if oid not in options:
                continue
            rug = float(candidate.get("rug_risk") or 0)
            momentum = float(candidate.get("momentum_quality") or 0)
            holder = float(candidate.get("holder_concentration_danger") or 0)
            scored.append((momentum - 2 * rug - 0.4 * holder, oid))
        scored.sort(reverse=True)
        best_edge, best_id = scored[0] if scored else (-1.0, "no_trade")
        if best_edge < self.thresholds.mock_pick_min_edge or best_id not in options:
            selected = "no_trade"
        else:
            selected = best_id
        return choice_payload(options, selected)


class HttpxJev:
    """Thin client for `POST {base}/v1/systemone`.

    `base_url` is `https://api.typesafe.ai` or `https://openrouter.ai/api`.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "jev-latest",
        *,
        client: httpx.AsyncClient | None = None,
        sleeper: Any = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30)
        self._sleeper = sleeper

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def ask(self, state: dict[str, Any], questions: dict[str, dict]) -> JudgeCall:
        url = f"{self.base_url}/v1/systemone"
        body = {"model": self.model, "state": state, "questions": questions}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        delay = 0.4
        last_error: JudgeError | None = None
        sleep = self._sleeper
        for attempt in range(3):
            response = await self._client.post(url, json=body, headers=headers)
            if response.status_code in {429, 500, 502, 503, 529} and attempt < 2:
                last_error = JudgeTransportError(response.status_code, response.text[:200])
                wait = delay
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = min(float(retry_after), 20.0)
                    except ValueError:
                        wait = delay
                log.info("jev retry status=%s wait=%.2f", response.status_code, wait)
                if sleep is None:
                    import asyncio

                    await asyncio.sleep(wait)
                else:
                    await sleep(wait)
                delay = min(delay * 2, 8)
                continue
            if response.status_code >= 400:
                raise JudgeTransportError(response.status_code, response.text[:300])
            try:
                payload = response.json()
            except ValueError as exc:
                raise JudgeTransportError(response.status_code, "invalid json") from exc
            return validate_response(payload, questions)
        raise last_error or JudgeTransportError(0, "no response")


class SdkJev:
    """Official `typesafe_sdk.AsyncTypeSafeClient` plus the same validator.

    OpenRouter is the same client with `base_url="https://openrouter.ai/api"`.
    """

    def __init__(self, api_key: str, base_url: str, model: str = "jev-latest") -> None:
        from typesafe_sdk import AsyncTypeSafeClient

        self._client = AsyncTypeSafeClient(api_key=api_key, base_url=base_url, model=model)
        self.model = model

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ask(self, state: dict[str, Any], questions: dict[str, dict]) -> JudgeCall:
        response = await self._client.system_one(state, questions, model=self.model)
        payload = _from_sdk(response)
        return validate_response(payload, questions)


def _from_sdk(response: Any) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for name, answer in response.answers.items():
        kind = answer.type
        if kind == "noul":
            answers[name] = {"type": "noul", "noul": answer.noul}
        elif kind == "choice":
            answers[name] = {
                "type": "choice",
                "choice": answer.choice,
                "confidence": answer.confidence,
                "probabilities": dict(answer.probabilities),
            }
        elif kind == "score":
            answers[name] = {
                "type": "score",
                "score": answer.score,
                "confidence": answer.confidence,
                "legend": {str(k): v for k, v in answer.legend.items()},
                "probabilities": {str(k): v for k, v in answer.probabilities.items()},
            }
    usage = {}
    if response.usage is not None:
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
    return {"model": response.model, "answers": answers, "usage": usage}


def build_judge(*, mock: bool, backend: str, api_key: str | None, base_url: str, model: str) -> Judge:
    if mock or backend == "mock":
        return MockJev()
    if not api_key:
        raise SystemExit(
            "No judge API key. Set TYPESAFE_API_KEY or OPENROUTER_API_KEY, or pass --mock."
        )
    try:
        import typesafe_sdk  # noqa: F401
    except ImportError:
        log.warning("typesafe-sdk is not installed; using the httpx Jev client")
        return HttpxJev(api_key, base_url, model)
    return SdkJev(api_key, base_url, model)
