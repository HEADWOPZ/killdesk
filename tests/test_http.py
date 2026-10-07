import asyncio
from datetime import timedelta

import httpx

from killdesk.http import CircuitBreaker, CircuitOpen, SourceError, TokenBucket, request_json


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_token_bucket_waits_for_a_refill() -> None:
    clock = Clock()

    async def sleeper(seconds: float) -> None:
        clock.now += seconds

    async def run() -> None:
        bucket = TokenBucket(60, clock=clock, sleeper=sleeper)
        for _ in range(60):
            await bucket.acquire()
        assert clock.now == 0
        await bucket.acquire()

    asyncio.run(run())
    assert clock.now == 1


def test_circuit_opens_and_resets() -> None:
    clock = Clock()
    breaker = CircuitBreaker("gecko", threshold=3, reset_seconds=30, clock=clock)
    assert breaker.allow()
    breaker.failure()
    breaker.failure()
    assert breaker.allow()
    breaker.failure()
    assert not breaker.allow()
    clock.now = 31
    assert breaker.allow()
    breaker.success()
    assert breaker.failures == 0


def test_request_retries_429_then_succeeds() -> None:
    calls = {"n": 0}
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    async def sleeper(seconds: float) -> None:
        waits.append(seconds)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            payload = await request_json(
                client,
                "GET",
                "https://example.test/pools",
                source="gecko",
                sleeper=sleeper,
            )
            assert payload == {"ok": True}

    asyncio.run(run())
    assert calls["n"] == 3
    assert waits == [0.0, 0.0]


def test_request_does_not_retry_client_errors() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"err": True})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            try:
                await request_json(client, "GET", "https://example.test/missing", source="gecko")
            except SourceError as exc:
                assert exc.source == "gecko"
            else:
                raise AssertionError("expected SourceError")

    asyncio.run(run())
    assert calls["n"] == 1


def test_open_circuit_skips_the_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request should not be sent")

    breaker = CircuitBreaker("gecko", threshold=1, reset_seconds=60)
    breaker.failure()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            try:
                await request_json(
                    client,
                    "GET",
                    "https://example.test/pools",
                    source="gecko",
                    breaker=breaker,
                )
            except CircuitOpen:
                return
            raise AssertionError("expected CircuitOpen")

    asyncio.run(run())
    assert timedelta(seconds=60).total_seconds() == 60
