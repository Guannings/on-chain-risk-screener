"""Real-time funding-rate fetcher for CEX perpetuals.

Used by `memecheck plan --symbol <TICKER>` so the user doesn't have to type
the current funding rate manually. Single source for v1: Kraken Futures
(non-China, public, no auth, well-documented).

Kraken's funding-rate format (verified empirically and per their docs):

  The `fundingRate` field in /tickers is the ABSOLUTE rate in
  USD per contract PER HOUR. To get the relative percent per 8h cycle
  (the convention Binance/Bybit/OKX use and that the planner expects):

      pct_per_8h = (raw / markPrice) * 8 * 100

  Sanity check across symbols on a single fetch:
    BTC  raw  0.294   / mark 64,465 * 8 * 100 =  0.0036% per 8h
    ETH  raw  0.0205  / mark  1,747 * 8 * 100 =  0.0094% per 8h
    XRP  raw -2.07e-5 / mark   1.18 * 8 * 100 = -0.014%  per 8h
    SOL  raw -6.27e-4 / mark  71.76 * 8 * 100 = -0.007%  per 8h
  All in the expected magnitude range for normal market conditions.

  Sign convention is exchange-uniform: negative rate = shorts pay longs
  (short-crowded market), positive = longs pay shorts (long-crowded).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from memecheck.common.http import get_json


@dataclass(frozen=True)
class FundingRateResult:
    """One funding-rate observation, normalised to per-8h cycle units."""

    symbol: str                       # canonical user-facing ticker (XRP, BTC, ...)
    rate_per_8h_pct: float            # normalised: percent per 8h cycle
    raw_rate: float                   # raw exchange rate, source's native unit
    raw_unit: str                     # describes what raw_rate means
    source: str                       # exchange / endpoint label
    mark_price: Optional[float] = None
    perp_symbol: Optional[str] = None  # exchange-specific symbol used


# ----------------------------- Kraken Futures ----------------------------

# Kraken uses XBT for BTC; everything else maps cleanly.
_KRAKEN_ALIASES: dict[str, str] = {
    "BTC": "XBT",
    "WBTC": "XBT",
}

# Hours per Kraken's reported rate. Their `fundingRate` is per HOUR (absolute
# USD per contract per hour); we multiply by 8 to get the per-8h equivalent.
KRAKEN_RATE_PERIOD_HOURS: float = 1.0


def _kraken_perp_symbol(symbol: str) -> str:
    """Construct Kraken's PF_<TICKER>USD perp symbol from a user ticker."""
    canon = symbol.upper().lstrip("$")
    canon = _KRAKEN_ALIASES.get(canon, canon)
    return f"PF_{canon}USD"


def fetch_kraken_funding(symbol: str) -> Optional[FundingRateResult]:
    """Hit Kraken Futures' /tickers endpoint and pull the funding rate.

    Kraken's `fundingRate` is the absolute USD-per-contract-per-hour rate,
    NOT a relative-percent. We convert with:
        pct_per_8h = (raw / mark) * 8 * 100

    Returns None if the symbol isn't listed there or the response is
    malformed. Never raises.
    """
    url = "https://futures.kraken.com/derivatives/api/v3/tickers"
    data = get_json(url)
    if "_error" in data:
        return None
    target = _kraken_perp_symbol(symbol)
    for ticker in data.get("tickers", []) or []:
        if ticker.get("symbol") != target:
            continue
        raw = ticker.get("fundingRate")
        mark = ticker.get("markPrice")
        if raw is None or mark is None:
            return None
        try:
            raw_f = float(raw)
            mark_f = float(mark)
        except (TypeError, ValueError):
            return None
        if mark_f <= 0:
            return None
        # Normalise: absolute hourly USD/contract → relative percent per 8h.
        per_8h_pct = (raw_f / mark_f) * (8.0 / KRAKEN_RATE_PERIOD_HOURS) * 100.0
        return FundingRateResult(
            symbol=symbol.upper().lstrip("$"),
            rate_per_8h_pct=per_8h_pct,
            raw_rate=raw_f,
            raw_unit="USD/contract/hour (absolute)",
            source="kraken-futures",
            mark_price=mark_f,
            perp_symbol=target,
        )
    return None


# ----------------------------- public entry point ------------------------

# Source order — first that returns wins. Add additional non-China venues here.
_SOURCES = (fetch_kraken_funding,)


def fetch_funding_rate(symbol: str) -> Optional[FundingRateResult]:
    """Try each configured source in order; return the first hit."""
    if not symbol or not symbol.strip():
        return None
    for fetcher in _SOURCES:
        result = fetcher(symbol)
        if result is not None:
            return result
    return None
