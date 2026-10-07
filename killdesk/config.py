"""Runtime settings. Thresholds stay in thresholds.py; this is wiring."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from killdesk.thresholds import DEFAULTS, Thresholds


def load_dotenv(path: Path | None = None) -> None:
    """Fill empty environment variables from a local .env. Never overrides."""
    file = path or Path(".env")
    if not file.exists():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass
class Settings:
    thresholds: Thresholds = field(default_factory=lambda: DEFAULTS)
    pages: int = 1
    db_path: Path = Path("desk.db")
    shadow: bool = True
    fixtures: Path | None = None
    solana_rpc_urls: tuple[str, ...] = (
        "https://api.mainnet-beta.solana.com",
        "https://solana-rpc.publicnode.com",
    )
    robinhood_rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    bsc_rpc_url: str = "https://bsc-dataseed.binance.org"
    etherscan_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    judge_backend: str = "typesafe"
    typesafe_api_key: str | None = None
    openrouter_api_key: str | None = None
    typesafe_base_url: str = "https://api.typesafe.ai"
    openrouter_base_url: str = "https://openrouter.ai/api"
    judge_model: str = "jev-latest"
    interval_minutes: int = 15

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        solana = _env("SOLANA_RPC_URL")
        urls = (solana, "https://solana-rpc.publicnode.com") if solana else (
            "https://api.mainnet-beta.solana.com",
            "https://solana-rpc.publicnode.com",
        )
        backend = (_env("KILLDESK_JUDGE_BACKEND") or "").lower()
        if backend not in {"typesafe", "openrouter", "mock"}:
            if _env("TYPESAFE_API_KEY"):
                backend = "typesafe"
            elif _env("OPENROUTER_API_KEY"):
                backend = "openrouter"
            else:
                backend = "typesafe"
        db = _env("KILLDESK_DB") or "desk.db"
        return cls(
            db_path=Path(db),
            solana_rpc_urls=urls,
            robinhood_rpc_url=_env("ROBINHOOD_RPC_URL") or "https://rpc.mainnet.chain.robinhood.com",
            bsc_rpc_url=_env("BSC_RPC_URL") or "https://bsc-dataseed.binance.org",
            etherscan_api_key=_env("ETHERSCAN_API_KEY"),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            judge_backend=backend,
            typesafe_api_key=_env("TYPESAFE_API_KEY"),
            openrouter_api_key=_env("OPENROUTER_API_KEY"),
            typesafe_base_url=_env("TYPESAFE_BASE_URL") or "https://api.typesafe.ai",
            openrouter_base_url=_env("OPENROUTER_BASE_URL") or "https://openrouter.ai/api",
            judge_model=_env("KILLDESK_JUDGE_MODEL") or "jev-latest",
        )

    def judge_key_and_url(self) -> tuple[str | None, str]:
        if self.judge_backend == "openrouter":
            return self.openrouter_api_key, self.openrouter_base_url
        return self.typesafe_api_key, self.typesafe_base_url
