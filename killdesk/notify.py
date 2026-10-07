"""Optional Telegram notify. Skipped when the env vars are unset."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("killdesk.notify")


async def send_telegram(
    text: str,
    *,
    token: str | None,
    chat_id: str | None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    if not token or not chat_id:
        return False
    owns = client is None
    http = client or httpx.AsyncClient(timeout=15)
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
        )
        if response.status_code >= 400:
            log.warning("telegram notify failed status=%s", response.status_code)
            return False
        return True
    except (httpx.HTTPError, OSError) as exc:
        log.warning("telegram notify failed: %s", exc)
        return False
    finally:
        if owns:
            await http.aclose()
