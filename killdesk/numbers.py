"""Parse numbers that arrive as strings from public market APIs."""

from __future__ import annotations


def fnum(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def inum(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 0) if text.startswith(("0x", "0X")) else int(float(text))
    except ValueError:
        return None


def fraction(value: object) -> float | None:
    """Normalize a percent-or-fraction field into a 0-1 fraction.

    GeckoTerminal holder distribution arrives as a percent string ("31.38").
    Values already inside [-1, 1] are left alone.
    """
    number = fnum(value)
    if number is None:
        return None
    if abs(number) > 1:
        return number / 100.0
    return number
