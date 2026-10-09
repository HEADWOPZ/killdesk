"""Stdlib HTTP server for the dashboard. One page, a JSON snapshot, and SSE.

Live marks (DexScreener) and 1-minute candles (GeckoTerminal) for the focus
token are fetched server-side, cached, and rate-limited so an open browser
tab never hammers the public APIs. Pass live_marks=False to stay offline.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from killdesk.collect import DEX_BASE, GECKO_ACCEPT, GECKO_BASE, index_pairs
from killdesk.dashboard.snapshot import build_snapshot

log = logging.getLogger("killdesk.dashboard")

UA = "KillDesk-dashboard/0.2 (+https://github.com/HEADWOPZ/killdesk)"
MARK_TTL = 15.0
CANDLE_TTL = 60.0
SSE_INTERVAL = 2.0


def _index_html() -> bytes:
    return resources.files("killdesk.dashboard").joinpath("static/index.html").read_bytes()


class LiveMarket:
    """Cached public market data for at most a couple of tokens at a time."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()
        self._marks: dict[str, tuple[float, dict | None]] = {}
        self._candles: dict[str, tuple[float, list]] = {}
        self._history: dict[str, list[tuple[float, float]]] = {}

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=8, headers={"User-Agent": UA})
        return self._client

    def market(self, chain: str, token: str) -> dict | None:
        if not self.enabled or not chain or not token:
            return None
        key = f"{chain}:{token}"
        with self._lock:
            cached = self._marks.get(key)
            if cached and time.monotonic() - cached[0] < MARK_TTL:
                return cached[1]
            info = None
            try:
                resp = self._http().get(f"{DEX_BASE}/tokens/v1/{chain}/{token}")
                if resp.status_code == 200:
                    rows = resp.json()
                    rows = rows if isinstance(rows, list) else (rows or {}).get("pairs") or []
                    stats = index_pairs(chain, rows).get(token if chain == "solana" else token.lower())
                    best = max(
                        (r for r in rows if isinstance(r, dict)),
                        key=lambda r: ((r.get("liquidity") or {}).get("usd") or 0),
                        default=None,
                    )
                    if stats is not None:
                        info = stats.model_dump()
                        if best:
                            info["price_change_h24"] = (best.get("priceChange") or {}).get("h24")
                            info["volume_h24"] = (best.get("volume") or {}).get("h24")
                            info["fdv_usd"] = best.get("fdv")
                            info["pair_created_at"] = best.get("pairCreatedAt")
                            info["url"] = best.get("url")
                        if info.get("price_usd"):
                            hist = self._history.setdefault(key, [])
                            hist.append((time.time(), float(info["price_usd"])))
                            del hist[:-240]
            except Exception as exc:  # network is optional
                log.info("mark fetch failed %s: %s", key, exc)
            self._marks[key] = (time.monotonic(), info)
            return info

    def history(self, chain: str, token: str) -> list[list[float]]:
        return [[t, p] for t, p in self._history.get(f"{chain}:{token}", [])]

    def candles(self, chain: str, pool: str | None) -> list:
        if not self.enabled or not chain or not pool:
            return []
        key = f"{chain}:{pool}"
        with self._lock:
            cached = self._candles.get(key)
            if cached and time.monotonic() - cached[0] < CANDLE_TTL:
                return cached[1]
            rows: list = []
            try:
                resp = self._http().get(
                    f"{GECKO_BASE}/networks/{chain}/pools/{pool}/ohlcv/minute",
                    params={"aggregate": "1", "limit": "90", "currency": "usd"},
                    headers={"Accept": GECKO_ACCEPT},
                )
                if resp.status_code == 200:
                    data = ((resp.json() or {}).get("data") or {}).get("attributes") or {}
                    rows = sorted(data.get("ohlcv_list") or [], key=lambda r: r[0])
                elif cached:
                    rows = cached[1]
            except Exception as exc:
                log.info("candle fetch failed %s: %s", key, exc)
                rows = cached[1] if cached else []
            self._candles[key] = (time.monotonic(), rows)
            return rows


def enrich(snapshot: dict[str, Any], market: LiveMarket) -> dict[str, Any]:
    focus = snapshot.get("focus")
    if focus and focus.get("token_address"):
        focus["market"] = market.market(focus["chain"], focus["token_address"])
        focus["candles"] = market.candles(focus["chain"], focus.get("pool_address"))
    position = snapshot.get("open")
    if position:
        info = market.market(position["chain"], position["token_address"])
        mark = (info or {}).get("price_usd")
        position["mark_usd"] = mark
        position["mark_history"] = market.history(position["chain"], position["token_address"])
        entry = position.get("entry_price_usd")
        if mark and entry:
            pct = (float(mark) - float(entry)) / float(entry)
            position["live_pnl_pct"] = pct
            position["live_pnl_usd"] = pct * float(position.get("paper_size_usd") or 0)
    snapshot["live_marks"] = market.enabled
    return snapshot


def make_handler(db_path: Path, market: LiveMarket) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "KillDesk/0.2"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _snapshot(self) -> dict[str, Any]:
            return enrich(build_snapshot(db_path, datetime.now(timezone.utc)), market)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, _index_html(), "text/html; charset=utf-8")
            elif url.path == "/api/snapshot":
                body = json.dumps(self._snapshot(), default=str).encode()
                self._send(200, body, "application/json")
            elif url.path == "/api/stream":
                self._stream(parse_qs(url.query))
            elif url.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif url.path == "/healthz":
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def _stream(self, query: dict) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            limit = int((query.get("limit") or ["0"])[0] or 0)
            sent = 0
            try:
                while True:
                    payload = json.dumps(self._snapshot(), default=str)
                    self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    sent += 1
                    if limit and sent >= limit:
                        return
                    time.sleep(SSE_INTERVAL)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def build_server(db_path: str | Path, host: str = "127.0.0.1", port: int = 8787, live_marks: bool = True):
    market = LiveMarket(enabled=live_marks)
    server = ThreadingHTTPServer((host, port), make_handler(Path(db_path), market))
    server.daemon_threads = True
    return server


def serve(db_path: str | Path, host: str = "127.0.0.1", port: int = 8787, live_marks: bool = True) -> None:
    server = build_server(db_path, host, port, live_marks)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def start_in_thread(db_path: str | Path, host: str = "127.0.0.1", port: int = 8787, live_marks: bool = True):
    server = build_server(db_path, host, port, live_marks)
    thread = threading.Thread(target=server.serve_forever, name="killdesk-dashboard", daemon=True)
    thread.start()
    return server
