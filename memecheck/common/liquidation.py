"""Isolated-margin liquidation-price calculator.

Independent of the screening pipeline — useful on its own as a sanity
check before opening a leveraged position. Ignores funding, slippage,
and venue-specific liquidation auctions, all of which make real
liquidations closer to entry than this formula suggests.
"""

from __future__ import annotations

from typing import Any


def liquidation_price(
    entry: float, leverage: float, side: str = "long", maint_margin: float = 0.005
) -> float:
    """Approx isolated-margin liquidation price.
       long:  P_liq = P * (1 - 1/L + mm)
       short: P_liq = P * (1 + 1/L - mm)"""
    if side == "long":
        return entry * (1 - 1 / leverage + maint_margin)
    return entry * (1 + 1 / leverage - maint_margin)


def liq_report(entry: float, leverage: float) -> None:
    print("\n=== LIQUIDATION MATH (isolated margin, approx) ===")
    for side in ("long", "short"):
        lp = liquidation_price(entry, leverage, side)
        move = abs(lp - entry) / entry * 100
        print(
            f"  {side.upper():5s} @ {leverage}x  ->  liq price {lp:.10g}  "
            f"(a {move:.1f}% adverse move wipes you)"
        )
    print(
        "  Reminder: memecoins do double-digit % candles routinely. "
        "Above ~3x, noise alone liquidates you before any thesis plays out."
    )


def liq_report_dict(entry: float, leverage: float) -> dict[str, Any]:
    out: dict[str, Any] = {"entry": entry, "leverage": leverage, "sides": {}}
    for side in ("long", "short"):
        lp = liquidation_price(entry, leverage, side)
        out["sides"][side] = {
            "liq_price": lp,
            "adverse_move_pct": abs(lp - entry) / entry * 100,
        }
    return out
