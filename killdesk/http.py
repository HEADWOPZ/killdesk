"""Retries, a token bucket, and a per-source circuit breaker.

One source failing is a logged error. It is never an exception for the cycle
to crash on. Callers catch `SourceError`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger("killdesk.http")

Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

RETRY_STATUSES = frozenset({429, 500, 502, 503, 529})


class SourceError(Exception):
    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source


class CircuitOpen(SourceError):
    pass


class TokenBucket:
    """Refilling bucket. `per_minute` is the rate; `burst` caps back-to-back calls (default: per_minute)."""

    def __init__(
        self,
        per_minute: float,
        *,
        burst: float | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper | None = None,
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        capacity = float(per_minute if burst is None else burst)
        if capacity <= 0:
            raise ValueError("burst must be positive")
        self.capacity = capacity
        self.tokens = capacity
        self.per_second = float(per_minute) / 60.0
        self.clock = clock
        self.sleeper = sleeper or asyncio.sleep
        self.updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = self.clock()
                elapsed = max(0.0, now - self.updated)
                self.updated = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.per_second)
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                wait = (cost - self.tokens) / self.per_second
            await self.sleeper(wait)


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        threshold: int = 3,
        reset_seconds: float = 120,
        clock: Clock = time.monotonic,
    ) -> None:
        self.name = name
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.clock = clock
        self.failures = 0
        self.open_until = 0.0

    def allow(self) -> bool:
        return self.clock() >= self.open_until

    def success(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = self.clock() + self.reset_seconds
            log.warning("circuit open source=%s reset_s=%s", self.name, self.reset_seconds)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, 20.0)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    source: str,
    bucket: TokenBucket | None = None,
    breaker: CircuitBreaker | None = None,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    params: dict[str, str] | None = None,
    retries: int = 3,
    backoff: float = 0.4,
    max_delay: float = 8.0,
    min_429_wait: float = 2.0,
    sleeper: Sleeper | None = None,
) -> Any:
    """HTTP JSON with backoff. 429 and 5xx retry. Other 4xx fail immediately."""
    sleep = sleeper or asyncio.sleep
    delay = backoff
    last = "request failed"
    for attempt in range(retries):
        if breaker is not None and not breaker.allow():
            raise CircuitOpen(source, "circuit open")
        try:
            if bucket is not None:
                await bucket.acquire()
            response = await client.request(
                method, url, headers=headers, json=json_body, params=params
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = str(exc)
            if breaker is not None:
                breaker.failure()
            if attempt + 1 >= retries:
                break
            log.info("retry source=%s attempt=%s error=%s", source, attempt + 1, exc)
            await sleep(delay)
            delay = min(delay * 2, max_delay)
            continue
        if response.status_code in RETRY_STATUSES:
            last = f"HTTP {response.status_code}"
            if attempt + 1 >= retries:
                # One exhausted request is one breaker failure, not one per attempt.
                if breaker is not None:
                    breaker.failure()
                break
            retry_after = _retry_after_seconds(response)
            # GeckoTerminal answers 429 with Retry-After: 0; never hammer it back-to-back.
            floor = min_429_wait if response.status_code == 429 else 0.0
            wait = max(retry_after or 0.0, delay, floor)
            log.info(
                "retry source=%s attempt=%s status=%s wait=%.2f",
                source,
                attempt + 1,
                response.status_code,
                wait,
            )
            await sleep(wait)
            delay = min(delay * 2, max_delay)
            continue
        if response.status_code >= 400:
            if breaker is not None:
                breaker.failure()
            raise SourceError(source, f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            if breaker is not None:
                breaker.failure()
            raise SourceError(source, "invalid json") from exc
        if breaker is not None:
            breaker.success()
        return payload
    raise SourceError(source, last)
