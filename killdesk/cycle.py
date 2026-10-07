"""One desk cycle: scan, kill, judge, pick, or watch the open shadow position."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from killdesk.book import Book, iso
from killdesk.chains import load_chain_facts
from killdesk.collect import dedupe, index_pairs, parse_new_pools
from killdesk.config import Settings
from killdesk.filter import chain_kill, free_kill, soft_kill, trade_kill
from killdesk.judge import Judge
from killdesk.models import CHAINS, Candidate
from killdesk.notify import send_telegram
from killdesk.pick import baseline_choice, build_pick
from killdesk.questions import questions_for
from killdesk.state import build_state, dossier_rank, with_judgement
from killdesk.thresholds import Thresholds

log = logging.getLogger("killdesk.cycle")


@dataclass
class CycleResult:
    started_at: datetime
    text: str
    scanned: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    pick: str | None = None
    model: str | None = None
    opened: bool = False
    closed: bool = False
    skipped_scan: bool = False
    source_errors: list[str] = field(default_factory=list)


def evaluate_exit(
    entry_price: float | None,
    mark_price: float | None,
    entry_at: datetime,
    now: datetime,
    thresholds: Thresholds,
) -> tuple[str | None, float | None]:
    """Deterministic exit. Take-profit, stop-loss, or max hold. No model."""
    pnl = None
    if entry_price is not None and entry_price > 0 and mark_price is not None:
        pnl = round((mark_price - entry_price) / entry_price, 6)
        if pnl >= thresholds.take_profit_pct:
            return "take_profit", pnl
        if pnl <= -thresholds.stop_loss_pct:
            return "stop_loss", pnl
    held_minutes = (now - entry_at).total_seconds() / 60.0
    if held_minutes >= thresholds.max_hold_minutes:
        return "max_hold", pnl
    return None, pnl


async def run_cycle(
    settings: Settings,
    book: Book,
    sources: Any,
    judge: Judge,
    now: datetime,
    *,
    notify: Any = None,
) -> CycleResult:
    if not settings.shadow:
        raise NotImplementedError("KillDesk MVP is shadow-only. --shadow cannot be turned off.")
    thresholds = settings.thresholds
    cycle_id = book.start_cycle(now, mode="shadow")
    lines = [f"KillDesk shadow cycle {iso(now)}"]
    source_errors: list[str] = []
    rejections: list[tuple[str, str, str]] = []

    open_row = book.get_open()
    if open_row is not None:
        closed, monitor_lines = await _monitor(open_row, sources, book, now, thresholds, source_errors)
        lines.extend(monitor_lines)
        if not closed:
            lines.append("scan paused")
            return await _finish(
                book,
                settings,
                cycle_id,
                now,
                lines,
                scanned=0,
                rejections={},
                pick=None,
                model=None,
                opened=False,
                closed=False,
                skipped_scan=True,
                source_errors=source_errors,
                notify=notify,
            )
        lines.append("scan resumed")

    candidates, scan_errors = await _scan(sources, settings.pages, now)
    source_errors.extend(scan_errors)
    skipped_benched = 0
    free_pass: list[Candidate] = []
    for candidate in candidates:
        if book.is_benched(candidate.option_id, now):
            skipped_benched += 1
            log.info("skip benched %s %s", candidate.symbol, candidate.option_id)
            continue
        check = free_kill(candidate, thresholds)
        if check:
            _reject(book, cycle_id, candidate, "free_kill", check, "", now, thresholds, rejections)
            continue
        free_pass.append(candidate)

    trade_pass = await _trade_stage(
        free_pass, sources, book, cycle_id, now, thresholds, rejections, source_errors
    )
    trade_pass.sort(key=dossier_rank, reverse=True)
    dossiers = trade_pass[: thresholds.max_dossiers]

    judged: list[tuple[Candidate, dict, Any]] = []
    for candidate in dossiers:
        try:
            candidate.chain_facts = await load_chain_facts(
                candidate, sources, settings.etherscan_api_key
            )
        except Exception as exc:
            source_errors.append(f"chain:{candidate.option_id}:{exc}")
            log.warning("chain facts failed %s: %s", candidate.option_id, exc)
            _reject(
                book, cycle_id, candidate, "chain_kill", "chain_data_unavailable",
                str(exc), now, thresholds, rejections,
            )
            continue
        check = chain_kill(candidate, thresholds)
        if check:
            detail = "; ".join(candidate.chain_facts.notes) if candidate.chain_facts else ""
            _reject(book, cycle_id, candidate, "chain_kill", check, detail, now, thresholds, rejections)
            continue
        state = build_state(candidate)
        try:
            call = await judge.ask(state, questions_for(candidate.chain, state))
        except Exception as exc:
            source_errors.append(f"judge:{candidate.option_id}:{exc}")
            log.warning("judge failed %s: %s", candidate.option_id, exc)
            _reject(
                book, cycle_id, candidate, "soft_kill", "judge_unavailable",
                str(exc), now, thresholds, rejections,
            )
            continue
        check = soft_kill(call.answers, thresholds)
        if check:
            _reject(book, cycle_id, candidate, "soft_kill", check, "", now, thresholds, rejections)
            continue
        judged.append((candidate, with_judgement(state, call.model, call.answers), call))

    pick_label = "no_trade"
    model_id = None
    confidence = None
    pick_call = None
    chosen_states: list[dict] = []
    if judged:
        chosen_states = [item[1] for item in judged]
        pick_state, pick_questions_body = build_pick(chosen_states, thresholds.max_shortlist)
        chosen_states = [
            item[1] for item in judged if item[1]["option_id"] in {
                row["option_id"] for row in pick_state["candidates"]
            }
        ]
        try:
            pick_call = await judge.ask(pick_state, pick_questions_body)
            selection = pick_call.answers["selection"]
            pick_label = str(selection["choice"])
            confidence = selection.get("confidence")
            model_id = pick_call.model
            if (
                pick_label != "no_trade"
                and confidence is not None
                and float(confidence) < thresholds.min_pick_confidence
            ):
                log.info("pick confidence %.3f below floor; standing down", float(confidence))
                pick_label = "no_trade"
        except Exception as exc:
            source_errors.append(f"pick:{exc}")
            log.warning("pick failed: %s", exc)
            pick_label = "no_trade"

    baseline = baseline_choice(chosen_states, thresholds.baseline_min_score)
    opened = False
    if pick_label != "no_trade":
        match = next((item for item in judged if item[1]["option_id"] == pick_label), None)
        if match is None:
            log.warning("pick %s is not on the shortlist", pick_label)
            pick_label = "no_trade"
        else:
            candidate, state, _call = match
            price = state.get("price_usd")
            if price is None:
                log.info("pick %s has no price; standing down", pick_label)
                pick_label = "no_trade"
                lines.append("pick had no entry price; standing down")
            else:
                blob = {
                    "by_token": {
                        item[1]["option_id"]: item[2].as_dict() for item in judged
                    },
                    "pick": None if pick_call is None else pick_call.as_dict(),
                }
                counters = _counts(rejections)
                book.open_position(
                    {
                        "chain": candidate.chain,
                        "symbol": candidate.symbol,
                        "token_address": candidate.token_address,
                        "pool_address": candidate.pool_address,
                        "option_id": candidate.option_id,
                        "entry_price_usd": price,
                        "entry_mcap_usd": state.get("mcap_usd"),
                        "entry_at": now,
                        "paper_size_usd": thresholds.paper_size_usd,
                        "model_id": model_id,
                        "jev_answers": blob,
                        "rejection_counters": counters,
                        "pick": None if pick_call is None else pick_call.as_dict(),
                        "baseline_choice": baseline,
                        "agrees_with_baseline": baseline == pick_label,
                        "opened_cycle_id": cycle_id,
                    }
                )
                opened = True
                lines.append(
                    f"shadow OPEN {candidate.symbol} {candidate.chain} "
                    f"ca={candidate.token_address} entry_price={price} "
                    f"entry_mcap={state.get('mcap_usd')} model={model_id}"
                )
                lines.append("no order sent")

    if pick_label == "no_trade":
        lines.append(
            f"pick=no_trade model={model_id or '-'} confidence={_fmt(confidence)} "
            "(no trade is a result)"
        )
    else:
        lines.append(
            f"pick={pick_label} model={model_id or '-'} confidence={_fmt(confidence)} "
            f"baseline={baseline} agrees={str(baseline == pick_label).lower()}"
        )

    counts = _counts(rejections)
    stage_counts = _stage_counts(rejections)
    lines.insert(
        1,
        "scanned={scanned} benched_skip={benched} free_kill={free} trade_kill={trade} "
        "chain_kill={chain} soft_kill={soft} shortlist={short}".format(
            scanned=len(candidates),
            benched=skipped_benched,
            free=stage_counts.get("free_kill", 0),
            trade=stage_counts.get("trade_kill", 0),
            chain=stage_counts.get("chain_kill", 0),
            soft=stage_counts.get("soft_kill", 0),
            short=len(judged),
        ),
    )
    if counts:
        rendered = " ".join(f"{key}={counts[key]}" for key in sorted(counts))
        lines.append(f"rejections: {rendered}")
    else:
        lines.append("rejections: none")
    if source_errors:
        lines.append(f"source_errors: {len(source_errors)}")

    return await _finish(
        book,
        settings,
        cycle_id,
        now,
        lines,
        scanned=len(candidates),
        rejections=counts,
        pick=pick_label,
        model=model_id,
        opened=opened,
        closed=bool(open_row),
        skipped_scan=False,
        source_errors=source_errors,
        notify=notify,
        extra={"baseline": baseline},
    )


async def _monitor(open_row, sources, book, now, thresholds, source_errors):
    from killdesk.book import parse_iso

    entry_at = parse_iso(open_row["entry_at"])
    mark = None
    try:
        mark = await sources.mark_price(open_row["chain"], open_row["token_address"])
    except Exception as exc:
        source_errors.append(f"mark:{exc}")
        log.warning("mark price failed: %s", exc)
    reason, pnl = evaluate_exit(
        open_row["entry_price_usd"], mark, entry_at, now, thresholds
    )
    symbol = open_row["symbol"]
    if reason is None:
        pnl_text = "n/a" if pnl is None else f"{pnl:+.2%}"
        return False, [
            f"position open: {symbol} {open_row['chain']} ca={open_row['token_address']} "
            f"entry={open_row['entry_price_usd']} mark={mark} pnl={pnl_text}"
        ]
    pnl_usd = None if pnl is None else open_row["paper_size_usd"] * pnl
    book.close_position(
        open_row["id"],
        exit_price=mark,
        exit_mcap=None,
        exit_at=now,
        reason=reason,
        pnl_pct=pnl,
        pnl_usd=pnl_usd,
    )
    pnl_text = "n/a" if pnl is None else f"{pnl:+.2%}"
    usd_text = "n/a" if pnl_usd is None else f"${pnl_usd:,.2f}"
    return True, [
        f"shadow CLOSE {symbol} {open_row['chain']} reason={reason} "
        f"exit_price={mark} pnl={pnl_text} ({usd_text})"
    ]


async def _scan(sources, pages: int, now: datetime) -> tuple[list[Candidate], list[str]]:
    found: list[Candidate] = []
    errors: list[str] = []
    for chain in CHAINS:
        for page in range(1, pages + 1):
            try:
                payload = await sources.gecko_new_pools(chain, page)
            except Exception as exc:
                errors.append(f"gecko:{chain}:p{page}:{exc}")
                log.warning("scan %s page %s failed: %s", chain, page, exc)
                continue
            try:
                found.extend(parse_new_pools(chain, payload, now))
            except Exception as exc:
                errors.append(f"parse:{chain}:p{page}:{exc}")
                log.warning("parse %s page %s failed: %s", chain, page, exc)
    return dedupe(found), errors


async def _trade_stage(candidates, sources, book, cycle_id, now, thresholds, rejections, errors):
    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.chain].append(candidate)
    passed: list[Candidate] = []
    for chain, group in grouped.items():
        ordered = sorted(group, key=dossier_rank, reverse=True)
        for chunk in _chunks(ordered, 30):
            try:
                pairs = await sources.dexscreener_tokens(chain, [item.token_address for item in chunk])
            except Exception as exc:
                errors.append(f"dex:{chain}:{exc}")
                log.warning("dexscreener %s failed: %s", chain, exc)
                for candidate in chunk:
                    _reject(
                        book, cycle_id, candidate, "trade_kill", "dex_unavailable",
                        str(exc), now, thresholds, rejections,
                    )
                continue
            indexed = index_pairs(chain, pairs if isinstance(pairs, list) else [])
            for candidate in chunk:
                candidate.trade = indexed.get(candidate.token_address)
                check = trade_kill(candidate, thresholds)
                if check:
                    _reject(book, cycle_id, candidate, "trade_kill", check, "", now, thresholds, rejections)
                else:
                    passed.append(candidate)
    return passed


def _reject(book, cycle_id, candidate, stage, check, detail, now, thresholds, rejections):
    log.info(
        "reject stage=%s check=%s chain=%s symbol=%s ca=%s",
        stage,
        check,
        candidate.chain,
        candidate.symbol,
        candidate.token_address,
    )
    book.bench(
        token_key=candidate.option_id,
        reason=check,
        stage=stage,
        detail=detail,
        chain=candidate.chain,
        symbol=candidate.symbol,
        token_address=candidate.token_address,
        now=now,
        thresholds=thresholds,
        cycle_id=cycle_id,
    )
    rejections.append((stage, check, candidate.option_id))


def _counts(rejections: list[tuple[str, str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _stage, check, _key in rejections:
        counts[check] = counts.get(check, 0) + 1
    return counts


def _stage_counts(rejections: list[tuple[str, str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for stage, _check, _key in rejections:
        counts[stage] = counts.get(stage, 0) + 1
    return counts


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


async def _finish(
    book,
    settings,
    cycle_id,
    now,
    lines,
    *,
    scanned,
    rejections,
    pick,
    model,
    opened,
    closed,
    skipped_scan,
    source_errors,
    notify,
    extra: dict | None = None,
) -> CycleResult:
    text = "\n".join(lines)
    summary = {
        "text": text,
        "scanned": scanned,
        "rejections": rejections,
        "pick": pick,
        "model": model,
        "opened": opened,
        "closed": closed,
        "skipped_scan": skipped_scan,
        "source_errors": source_errors,
    }
    if extra:
        summary.update(extra)
    book.finish_cycle(cycle_id, now, summary)
    if notify is not None:
        await notify(text)
    else:
        await send_telegram(
            text,
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
    log.info("\n%s", text)
    return CycleResult(
        started_at=now,
        text=text,
        scanned=scanned,
        rejections=rejections,
        pick=pick,
        model=model,
        opened=opened,
        closed=closed,
        skipped_scan=skipped_scan,
        source_errors=source_errors,
    )
