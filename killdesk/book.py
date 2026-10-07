"""Shadow book and rejection bench. SQLite file `desk.db`.

One open position. A rejected token stays benched for a reason-specific TTL.
Honeypot does not expire. `too_early` lasts about an hour.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from killdesk.thresholds import DEFAULTS, Thresholds

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS rejection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER,
    created_at TEXT NOT NULL,
    token_key TEXT NOT NULL,
    chain TEXT NOT NULL,
    symbol TEXT NOT NULL,
    token_address TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS bench (
    token_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    stage TEXT NOT NULL,
    detail TEXT,
    chain TEXT NOT NULL,
    symbol TEXT NOT NULL,
    token_address TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (token_key, reason)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    chain TEXT NOT NULL,
    symbol TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    option_id TEXT NOT NULL,
    entry_price_usd REAL,
    entry_mcap_usd REAL,
    entry_at TEXT NOT NULL,
    exit_price_usd REAL,
    exit_mcap_usd REAL,
    exit_at TEXT,
    exit_reason TEXT,
    pnl_pct REAL,
    pnl_usd REAL,
    paper_size_usd REAL NOT NULL,
    model_id TEXT,
    jev_answers_json TEXT NOT NULL,
    rejection_counters_json TEXT NOT NULL,
    pick_json TEXT,
    baseline_choice TEXT,
    agrees_with_baseline INTEGER,
    opened_cycle_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_bench_expires ON bench (expires_at);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);
CREATE INDEX IF NOT EXISTS idx_rejection_reason ON rejection_log (reason);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class Book:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def start_cycle(self, started: datetime, mode: str = "shadow") -> int:
        cur = self.conn.execute(
            "INSERT INTO cycles (started_at, mode) VALUES (?, ?)",
            (iso(started), mode),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_cycle(self, cycle_id: int, finished: datetime, summary: dict) -> None:
        self.conn.execute(
            "UPDATE cycles SET finished_at = ?, summary_json = ? WHERE id = ?",
            (iso(finished), json.dumps(summary, default=str), cycle_id),
        )
        self.conn.commit()

    def is_benched(self, token_key: str, now: datetime) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM bench
            WHERE token_key = ? AND (expires_at IS NULL OR expires_at > ?)
            LIMIT 1
            """,
            (token_key, iso(now)),
        ).fetchone()
        return row is not None

    def bench(
        self,
        *,
        token_key: str,
        reason: str,
        stage: str,
        detail: str,
        chain: str,
        symbol: str,
        token_address: str,
        now: datetime,
        thresholds: Thresholds | None = None,
        cycle_id: int | None = None,
    ) -> None:
        limits = thresholds or DEFAULTS
        seconds = limits.ttl(reason)
        expires = None if seconds is None else iso(now + timedelta(seconds=seconds))
        self.conn.execute(
            """
            INSERT INTO bench (
                token_key, reason, stage, detail, chain, symbol, token_address, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_key, reason) DO UPDATE SET
                stage = excluded.stage,
                detail = excluded.detail,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
            """,
            (token_key, reason, stage, detail, chain, symbol, token_address, iso(now), expires),
        )
        self.conn.execute(
            """
            INSERT INTO rejection_log (
                cycle_id, created_at, token_key, chain, symbol, token_address, stage, reason, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cycle_id, iso(now), token_key, chain, symbol, token_address, stage, reason, detail),
        )
        self.conn.commit()

    def get_open(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def open_position(self, row: dict[str, Any]) -> int:
        if self.get_open() is not None:
            raise RuntimeError("one shadow position at a time")
        columns = (
            "status",
            "chain",
            "symbol",
            "token_address",
            "pool_address",
            "option_id",
            "entry_price_usd",
            "entry_mcap_usd",
            "entry_at",
            "paper_size_usd",
            "model_id",
            "jev_answers_json",
            "rejection_counters_json",
            "pick_json",
            "baseline_choice",
            "agrees_with_baseline",
            "opened_cycle_id",
        )
        payload = {
            "status": "open",
            "entry_at": row["entry_at"] if isinstance(row["entry_at"], str) else iso(row["entry_at"]),
            "jev_answers_json": _json(row["jev_answers"]),
            "rejection_counters_json": _json(row["rejection_counters"]),
            "pick_json": _json(row.get("pick")),
            "agrees_with_baseline": 1 if row.get("agrees_with_baseline") else 0,
        }
        payload.update({key: row.get(key) for key in columns if key not in payload})
        placeholders = ", ".join("?" for _ in columns)
        cur = self.conn.execute(
            f"INSERT INTO positions ({', '.join(columns)}) VALUES ({placeholders})",
            [payload.get(column) for column in columns],
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_position(
        self,
        position_id: int,
        *,
        exit_price: float | None,
        exit_mcap: float | None,
        exit_at: datetime,
        reason: str,
        pnl_pct: float | None,
        pnl_usd: float | None,
    ) -> None:
        updated = self.conn.execute(
            """
            UPDATE positions
            SET status = 'closed', exit_price_usd = ?, exit_mcap_usd = ?, exit_at = ?,
                exit_reason = ?, pnl_pct = ?, pnl_usd = ?
            WHERE id = ? AND status = 'open'
            """,
            (exit_price, exit_mcap, iso(exit_at), reason, pnl_pct, pnl_usd, position_id),
        )
        self.conn.commit()
        if updated.rowcount != 1:
            raise RuntimeError(f"position {position_id} is not open")

    def rejection_stats(self) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT reason, COUNT(*) AS n FROM rejection_log GROUP BY reason ORDER BY n DESC, reason"
        ).fetchall()
        return [(str(row["reason"]), int(row["n"])) for row in rows]

    def performance(self) -> dict[str, Any]:
        closed = self.conn.execute(
            "SELECT * FROM positions WHERE status = 'closed' ORDER BY id"
        ).fetchall()
        priced = [row for row in closed if row["pnl_pct"] is not None]
        wins = [row for row in priced if row["pnl_pct"] > 0]
        pnl = sum(row["pnl_usd"] or 0 for row in priced)
        return {
            "open": self.get_open(),
            "closed": len(closed),
            "priced": len(priced),
            "wins": len(wins),
            "hit_rate": (len(wins) / len(priced)) if priced else None,
            "pnl_usd": pnl,
        }


def format_report(book: Book) -> str:
    stats = book.performance()
    lines = ["KillDesk shadow report"]
    pnl = stats["pnl_usd"]
    lines.append(f"realized shadow P&L: ${pnl:,.2f} across {stats['priced']} priced closes")
    if stats["hit_rate"] is None:
        lines.append("hit rate: n/a (no priced closes)")
    else:
        lines.append(
            f"hit rate: {stats['hit_rate']:.1%} ({stats['wins']}/{stats['priced']} priced closes)"
        )
    lines.append(f"closed positions: {stats['closed']}")
    open_row = stats["open"]
    if open_row:
        lines.append(
            "open: {symbol} {chain} ca={token_address} entry_price={entry_price_usd} "
            "entry_mcap={entry_mcap_usd} since {entry_at}".format(**open_row)
        )
    else:
        lines.append("open: none")
    lines.append("rejection stats:")
    reasons = book.rejection_stats()
    if not reasons:
        lines.append("  (none)")
    else:
        for reason, count in reasons:
            lines.append(f"  {reason}: {count}")
    return "\n".join(lines)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)
