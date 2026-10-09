"""Build the JSON the dashboard renders. Read-only queries over desk.db."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from killdesk.thresholds import DEFAULTS

PAPER_BANK_USD = 1_000.0
STAGES = ("free_kill", "trade_kill", "chain_kill", "soft_kill", "pick")
_STAGE_RANK = {stage: index for index, stage in enumerate(STAGES)}


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Plain connection pinned to query_only. The dashboard never writes the book."""
    conn = sqlite3.connect(Path(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _loads(text: Any) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def build_snapshot(path: str | Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    path = Path(path)
    empty: dict[str, Any] = {
        "now": now.isoformat(),
        "ok": False,
        "db": str(path),
        "meta": {"mode": "shadow"},
        "cycles": [],
        "latest": None,
        "feed": [],
        "bench": [],
        "bench_total": 0,
        "bench_permanent": 0,
        "watchlist": 0,
        "reasons": [],
        "performance": _empty_perf(),
        "open": None,
        "focus": None,
        "totals": {},
        "thresholds": _threshold_view(),
    }
    if not path.exists():
        return empty
    conn = connect_ro(path)
    try:
        tables = _tables(conn)
        if "cycles" not in tables:
            return empty
        snap = dict(empty)
        snap["ok"] = True
        snap["meta"] = _meta(conn, tables)
        snap["cycles"] = _cycles(conn)
        finished = [row for row in snap["cycles"] if row.get("finished_at")]
        snap["latest"] = finished[-1] if finished else None
        last_scan = next((row for row in reversed(finished) if not row.get("skipped_scan")), None)
        snap["last_scan"] = last_scan
        snap["feed"] = _feed(conn, tables)
        snap["bench"], snap["bench_total"], snap["bench_permanent"] = _bench(conn, now)
        snap["watchlist"] = (
            int(conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]) if "watchlist" in tables else 0
        )
        snap["reasons"] = [
            {"reason": row["reason"], "stage": row["stage"], "n": row["n"]}
            for row in conn.execute(
                "SELECT reason, MAX(stage) AS stage, COUNT(*) AS n FROM rejection_log "
                "GROUP BY reason ORDER BY n DESC LIMIT 16"
            )
        ]
        snap["performance"] = _performance(conn)
        snap["open"] = snap["performance"].pop("open_row")
        snap["focus"] = _focus(conn, tables, snap["open"], last_scan)
        snap["totals"] = _totals(conn, tables, snap["cycles"])
        return snap
    finally:
        conn.close()


def _empty_perf() -> dict[str, Any]:
    return {
        "bank_usd": PAPER_BANK_USD,
        "equity_usd": PAPER_BANK_USD,
        "pnl_usd": 0.0,
        "wins": 0,
        "losses": 0,
        "closed": 0,
        "hit_rate": None,
        "curve": [],
        "closes": [],
    }


def _threshold_view() -> dict[str, Any]:
    t = DEFAULTS
    return {
        "min_age_minutes": t.min_age_minutes,
        "max_age_minutes": t.max_age_minutes,
        "min_liquidity_usd": t.min_liquidity_usd,
        "take_profit_pct": t.take_profit_pct,
        "stop_loss_pct": t.stop_loss_pct,
        "max_hold_minutes": t.max_hold_minutes,
        "paper_size_usd": t.paper_size_usd,
    }


def _meta(conn: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": "shadow"}
    if "meta" in tables:
        for row in conn.execute("SELECT key, value FROM meta"):
            value = _loads(row["value"])
            out[row["key"]] = value if value is not None else row["value"]
    out["mode"] = "shadow"
    return out


def _cycles(conn: sqlite3.Connection, limit: int = 96) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, started_at, finished_at, summary_json FROM cycles ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for row in reversed(rows):
        summary = _loads(row["summary_json"]) or {}
        rejections = summary.get("rejections") or {}
        funnel = summary.get("funnel") or {}
        if not funnel and summary:
            funnel = {"scanned": summary.get("scanned", 0)}
        out.append(
            {
                "id": row["id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "scanned": summary.get("scanned", 0),
                "rejections": rejections,
                "killed": sum(int(v) for v in rejections.values()) if isinstance(rejections, dict) else 0,
                "stage_counts": summary.get("stage_counts") or {},
                "funnel": funnel,
                "sources": summary.get("sources") or {},
                "benched_skip": summary.get("benched_skip", 0),
                "pick": summary.get("pick"),
                "model": summary.get("model"),
                "confidence": summary.get("confidence"),
                "opened": summary.get("opened", False),
                "closed": summary.get("closed", False),
                "skipped_scan": summary.get("skipped_scan", False),
                "source_errors": len(summary.get("source_errors") or []),
                "jev": summary.get("jev"),
                "text": summary.get("text"),
                "watchlist": summary.get("watchlist"),
            }
        )
    return out


def _feed(conn: sqlite3.Connection, tables: set[str], limit: int = 120) -> list[dict[str, Any]]:
    if "stage_log" in tables:
        rows = conn.execute(
            """
            SELECT id, cycle_id, created_at, chain, symbol, token_address, source, stage, outcome,
                   reason, mcap_usd, liquidity_usd, age_minutes
            FROM stage_log ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]
    rows = conn.execute(
        """
        SELECT id, cycle_id, created_at, chain, symbol, token_address, stage, reason
        FROM rejection_log ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) | {"outcome": "DROP", "source": None} for row in reversed(rows)]


def _bench(conn: sqlite3.Connection, now: datetime, limit: int = 40) -> tuple[list[dict], int, int]:
    stamp = now.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    rows = conn.execute(
        """
        SELECT token_key, reason, stage, chain, symbol, token_address, created_at, expires_at
        FROM bench WHERE expires_at IS NOT NULL AND expires_at > ?
        ORDER BY expires_at ASC LIMIT ?
        """,
        (stamp, limit),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM bench WHERE expires_at IS NULL OR expires_at > ?", (stamp,)
    ).fetchone()[0]
    permanent = conn.execute("SELECT COUNT(*) FROM bench WHERE expires_at IS NULL").fetchone()[0]
    return [dict(row) for row in rows], int(total), int(permanent)


def _performance(conn: sqlite3.Connection) -> dict[str, Any]:
    perf = _empty_perf()
    closed = conn.execute("SELECT * FROM positions WHERE status = 'closed' ORDER BY id").fetchall()
    equity = PAPER_BANK_USD
    curve = []
    closes = []
    for row in closed:
        pnl = row["pnl_usd"] or 0.0
        equity += pnl
        curve.append({"t": row["exit_at"], "equity": round(equity, 2)})
        closes.append(
            {
                "symbol": row["symbol"],
                "chain": row["chain"],
                "pnl_pct": row["pnl_pct"],
                "pnl_usd": row["pnl_usd"],
                "reason": row["exit_reason"],
                "exit_at": row["exit_at"],
            }
        )
    priced = [row for row in closed if row["pnl_pct"] is not None]
    wins = [row for row in priced if row["pnl_pct"] > 0]
    perf.update(
        {
            "equity_usd": round(equity, 2),
            "pnl_usd": round(equity - PAPER_BANK_USD, 2),
            "wins": len(wins),
            "losses": len(priced) - len(wins),
            "closed": len(closed),
            "hit_rate": (len(wins) / len(priced)) if priced else None,
            "curve": curve,
            "closes": closes[-12:],
        }
    )
    open_row = conn.execute("SELECT * FROM positions WHERE status = 'open' ORDER BY id DESC LIMIT 1").fetchone()
    perf["open_row"] = None
    if open_row is not None:
        row = dict(open_row)
        for key in ("jev_answers_json", "rejection_counters_json"):
            row.pop(key, None)
        row["pick"] = _loads(row.pop("pick_json", None))
        perf["open_row"] = row
    return perf


def _focus(
    conn: sqlite3.Connection, tables: set[str], open_row: dict | None, last_scan: dict | None
) -> dict[str, Any] | None:
    """The token the analysis panel charts: open position, else the deepest runner of the last scan."""
    if "stage_log" not in tables:
        if open_row:
            return {"kind": "position", **{k: open_row.get(k) for k in ("chain", "symbol", "token_address")},
                    "pool_address": open_row.get("pool_address"), "checks": []}
        return None
    if open_row:
        token_key = open_row["option_id"]
        kind = "position"
        cycle_id = open_row.get("opened_cycle_id")
    else:
        if not last_scan:
            return None
        cycle_id = last_scan["id"]
        rows = conn.execute(
            "SELECT token_key, stage, outcome, mcap_usd, liquidity_usd FROM stage_log WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchall()
        if not rows:
            return None
        best: dict[str, tuple[float, float]] = {}
        for row in rows:
            depth = _STAGE_RANK.get(row["stage"], 0) + (0.5 if row["outcome"] == "PASS" else 0)
            weight = row["liquidity_usd"] or 0.0
            current = best.get(row["token_key"], (-1.0, 0.0))
            best[row["token_key"]] = max(current, (depth, weight))
        token_key = max(best.items(), key=lambda item: item[1])[0]
        kind = "runner"
    checks = conn.execute(
        """
        SELECT stage, outcome, reason, chain, symbol, token_address, pool_address, source,
               mcap_usd, liquidity_usd, age_minutes, created_at
        FROM stage_log WHERE token_key = ? AND (? IS NULL OR cycle_id = ?) ORDER BY id
        """,
        (token_key, cycle_id, cycle_id),
    ).fetchall()
    if not checks:
        checks = conn.execute(
            "SELECT * FROM stage_log WHERE token_key = ? ORDER BY id DESC LIMIT 6", (token_key,)
        ).fetchall()[::-1]
    if not checks:
        if open_row:
            return {"kind": kind, "chain": open_row["chain"], "symbol": open_row["symbol"],
                    "token_address": open_row["token_address"], "pool_address": open_row["pool_address"],
                    "checks": []}
        return None
    last = dict(checks[-1])
    return {
        "kind": kind,
        "token_key": token_key,
        "chain": last["chain"],
        "symbol": last["symbol"],
        "token_address": last["token_address"],
        "pool_address": last.get("pool_address") or (open_row or {}).get("pool_address"),
        "source": last.get("source"),
        "mcap_usd": last.get("mcap_usd"),
        "liquidity_usd": last.get("liquidity_usd"),
        "age_minutes": last.get("age_minutes"),
        "checks": [
            {"stage": row["stage"], "outcome": row["outcome"], "reason": row["reason"]} for row in checks
        ],
    }


def _totals(conn: sqlite3.Connection, tables: set[str], cycles: list[dict]) -> dict[str, Any]:
    out = {
        "cycles": int(conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]),
        "rejections": int(conn.execute("SELECT COUNT(*) FROM rejection_log").fetchone()[0]),
        "scanned": sum(int(row.get("scanned") or 0) for row in cycles),
    }
    if "stage_log" in tables:
        out["passes"] = int(
            conn.execute("SELECT COUNT(*) FROM stage_log WHERE outcome = 'PASS'").fetchone()[0]
        )
        out["stage_passes"] = {
            row["stage"]: row["n"]
            for row in conn.execute(
                "SELECT stage, COUNT(*) AS n FROM stage_log WHERE outcome = 'PASS' GROUP BY stage"
            )
        }
    return out
