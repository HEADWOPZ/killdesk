"""Shared EVM decoding for BSC and Robinhood Chain."""

from __future__ import annotations

ZERO_ADDRESS = "0x" + "0" * 40
OWNER_SELECTOR = "0x8da5cb5b"
SUPPLY_SELECTOR = "0x18160ddd"


def decode_address(word: object) -> str | None:
    if not isinstance(word, str) or not word.startswith("0x"):
        return None
    hex_body = word[2:]
    if len(hex_body) < 40:
        return None
    return "0x" + hex_body[-40:].lower()


def code_is_empty(code: object) -> bool:
    if not isinstance(code, str):
        return True
    return code in {"0x", "0x0", ""}


def code_size(code: object) -> int:
    if not isinstance(code, str) or not code.startswith("0x"):
        return 0
    body = code[2:]
    return len(body) // 2


def pad_address(address: str) -> str:
    body = address[2:] if address.startswith("0x") else address
    return "0x" + body.lower().rjust(64, "0")
