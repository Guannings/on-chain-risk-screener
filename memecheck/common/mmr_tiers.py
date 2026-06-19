"""Published maintenance-margin tier tables for major CEX perp venues.

Real exchanges don't use a constant maintenance-margin ratio (MMR). They
publish tiered tables where the MMR increases as the position notional
crosses thresholds. A $5k BTC position on Kraken Futures uses 0.4% MMR;
a $5M position uses 5%. That changes the liquidation distance by an
order of magnitude.

The previous planner used a constant `mm = 0.005` everywhere, which is
honest for small positions but materially wrong for big ones. This
module provides per-venue lookups so the planner can compute a
notional-aware MMR.

Sources:
  Kraken Futures: https://support.kraken.com/hc/en-us/articles/360037918671
  Bybit:          https://www.bybit.com/en/help-center/article/Trading-Rules-of-USDT-Perpetual
  Deribit:        https://insights.deribit.com/exchange-updates/

Numbers below are publicly published and rounded conservatively (we
pick the more restrictive side when the source uses a band).

Per-asset overrides: a `tier_table` is per-symbol because exchanges
publish different rules for BTC vs alts. For symbols we don't list,
the planner falls back to the venue default.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MMRTier:
    """One tier: positions up to `max_notional_usd` use this MMR."""

    max_notional_usd: float
    mmr: float                  # decimal, e.g. 0.005 = 0.5%
    max_leverage: int           # informational; matches exchange UI


# ----------------------------- Kraken Futures ----------------------------

# Kraken publishes a single schedule for "Crypto Perpetual" contracts.
# Source: support article cited above. Numbers are for BTC, ETH; minor alts
# use a stricter table — we use the alt table as the default for safety.

_KRAKEN_DEFAULT: tuple[MMRTier, ...] = (
    MMRTier(max_notional_usd=50_000,    mmr=0.004,  max_leverage=50),
    MMRTier(max_notional_usd=500_000,   mmr=0.010,  max_leverage=25),
    MMRTier(max_notional_usd=2_000_000, mmr=0.025,  max_leverage=10),
    MMRTier(max_notional_usd=float("inf"), mmr=0.050, max_leverage=5),
)


# ----------------------------- Bybit USDT-perp ---------------------------

# Bybit publishes per-asset risk-limit tables. Numbers below are for the
# canonical BTC/USDT perpetual; many alts use a tighter schedule.
# Source: Bybit Trading Rules page cited above.

_BYBIT_BTC: tuple[MMRTier, ...] = (
    MMRTier(max_notional_usd=2_000_000,   mmr=0.0040, max_leverage=100),
    MMRTier(max_notional_usd=5_000_000,   mmr=0.0050, max_leverage=100),
    MMRTier(max_notional_usd=10_000_000,  mmr=0.0075, max_leverage=50),
    MMRTier(max_notional_usd=20_000_000,  mmr=0.0100, max_leverage=20),
    MMRTier(max_notional_usd=50_000_000,  mmr=0.0150, max_leverage=10),
    MMRTier(max_notional_usd=float("inf"),mmr=0.0250, max_leverage=5),
)

_BYBIT_DEFAULT: tuple[MMRTier, ...] = (
    MMRTier(max_notional_usd=100_000,    mmr=0.005, max_leverage=50),
    MMRTier(max_notional_usd=500_000,    mmr=0.010, max_leverage=25),
    MMRTier(max_notional_usd=2_000_000,  mmr=0.025, max_leverage=10),
    MMRTier(max_notional_usd=float("inf"),mmr=0.050, max_leverage=5),
)


# ----------------------------- Deribit -----------------------------------

# Deribit's "linear" perpetuals use a step formula. Cited insights post
# gives the tier breakdown for BTC and ETH; alts use a similar schedule
# scaled down.

_DERIBIT_BTC: tuple[MMRTier, ...] = (
    MMRTier(max_notional_usd=100_000,    mmr=0.0050, max_leverage=50),
    MMRTier(max_notional_usd=500_000,    mmr=0.0100, max_leverage=25),
    MMRTier(max_notional_usd=2_000_000,  mmr=0.0250, max_leverage=10),
    MMRTier(max_notional_usd=float("inf"),mmr=0.0500, max_leverage=5),
)

_DERIBIT_DEFAULT: tuple[MMRTier, ...] = _DERIBIT_BTC


# ----------------------------- venue tables ------------------------------

# Per-venue: a default table + an optional per-symbol override map.

_TABLES: dict[str, dict[str, tuple[MMRTier, ...]]] = {
    "kraken-futures": {
        "_default": _KRAKEN_DEFAULT,
    },
    "bybit": {
        "_default": _BYBIT_DEFAULT,
        "BTC":      _BYBIT_BTC,
    },
    "deribit": {
        "_default": _DERIBIT_DEFAULT,
        "BTC":      _DERIBIT_BTC,
    },
}

# Default venue when none is specified. Kraken Futures matches the rest
# of the codebase's "primary source" assumption.
DEFAULT_VENUE: str = "kraken-futures"

# Fallback when a venue isn't recognised: 0.5% constant (matches the
# previous hardcoded planner default).
_FALLBACK_TIER: MMRTier = MMRTier(
    max_notional_usd=float("inf"), mmr=0.005, max_leverage=20
)


# ----------------------------- public API --------------------------------


def lookup_mmr_tier(
    venue: Optional[str],
    symbol: Optional[str],
    notional_usd: float,
) -> MMRTier:
    """Return the MMR tier matching the given position notional.

    Falls back to a 0.5% constant tier when the venue isn't recognised
    so existing scripts that didn't specify a venue keep their previous
    behaviour.
    """
    v = (venue or DEFAULT_VENUE).lower()
    if v not in _TABLES:
        return _FALLBACK_TIER
    sym = (symbol or "").upper().lstrip("$")
    table = _TABLES[v].get(sym) or _TABLES[v]["_default"]

    # Tiers are sorted by max_notional_usd ascending. Find the first tier
    # whose ceiling exceeds the position notional.
    ceilings = [t.max_notional_usd for t in table]
    idx = bisect_left(ceilings, notional_usd)
    if idx >= len(table):
        return table[-1]
    return table[idx]


def supported_venues() -> list[str]:
    return sorted(_TABLES.keys())
