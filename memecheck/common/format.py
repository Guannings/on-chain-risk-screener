"""Pure formatting helpers — no side effects, no I/O."""

from __future__ import annotations

import re
from typing import Any


def is_solana_address(addr: str) -> bool:
    """EVM = 0x + 40 hex; Solana = base58, ~32-44 chars, no 0x."""
    return not addr.startswith("0x") and bool(
        re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr)
    )


def fmt_usd(x: Any) -> str:
    if x is None:
        return "n/a"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "n/a"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x/div:.2f}{unit}"
    return f"${x:,.2f}"


def pct(x: Any) -> str:
    return "n/a" if x is None else f"{float(x):.1f}%"
