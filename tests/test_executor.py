import asyncio

import httpx

from killdesk.executor import Executor, ShadowOrder
from killdesk.notify import send_telegram


def test_executor_cannot_leave_shadow_mode() -> None:
    desk = Executor(shadow=True)
    assert desk.shadow is True
    try:
        desk.submit(ShadowOrder(chain="solana", symbol="GOOD", token_address="mint", side="buy"))
    except NotImplementedError as exc:
        assert "shadow" in str(exc).lower() or "No order" in str(exc)
    else:
        raise AssertionError("submit must refuse")
    try:
        Executor(shadow=False)
    except NotImplementedError:
        pass
    else:
        raise AssertionError("live executor must refuse")


def test_telegram_skips_when_unset_and_posts_when_set() -> None:
    async def skip() -> None:
        assert await send_telegram("hi", token=None, chat_id=None) is False
        assert await send_telegram("hi", token="", chat_id="1") is False

    asyncio.run(skip())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        assert "botsecret-token" in str(request.url)
        return httpx.Response(200, json={"ok": True})

    async def send() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ok = await send_telegram("cycle ok", token="secret-token", chat_id="99", client=client)
            assert ok is True

    asyncio.run(send())
