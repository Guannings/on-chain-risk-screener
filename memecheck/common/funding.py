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
    rate_per_8h_pct_next: Optional[float] = None  # predicted next-cycle rate


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
        raw_next = ticker.get("fundingRatePrediction")
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
        per_8h_pct_next: Optional[float] = None
        if raw_next is not None:
            try:
                per_8h_pct_next = (float(raw_next) / mark_f) * (8.0 / KRAKEN_RATE_PERIOD_HOURS) * 100.0
            except (TypeError, ValueError):
                per_8h_pct_next = None
        return FundingRateResult(
            symbol=symbol.upper().lstrip("$"),
            rate_per_8h_pct=per_8h_pct,
            raw_rate=raw_f,
            raw_unit="USD/contract/hour (absolute)",
            source="kraken-futures",
            mark_price=mark_f,
            perp_symbol=target,
            rate_per_8h_pct_next=per_8h_pct_next,
        )
    return None


# ----------------------------- Hyperliquid (DEX perp) -------------------

# Hyperliquid's /info endpoint with type=metaAndAssetCtxs returns
# [meta, ctxs] where meta.universe[i] has the symbol and ctxs[i].funding
# is the decimal rate per HOUR (already relative to mark). Conversion to
# per-8h percent is just × 8 × 100.

import json as _json
import urllib.error as _urllib_error
import urllib.request as _urllib_request


def fetch_hyperliquid_funding(symbol: str) -> Optional[FundingRateResult]:
    """Pull funding rate from Hyperliquid's public API.

    Useful as a fallback when a symbol isn't listed on Kraken Futures —
    Hyperliquid lists many newer perpetuals that CEXes haven't picked up.
    Returns None if the symbol isn't in their universe or the fetch fails.
    """
    canon = symbol.upper().lstrip("$")
    body = _json.dumps({"type": "metaAndAssetCtxs"}).encode("utf-8")
    req = _urllib_request.Request(
        "https://api.hyperliquid.xyz/info",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "memecheck/0.5"},
        method="POST",
    )
    try:
        with _urllib_request.urlopen(req, timeout=15) as r:
            data = _json.loads(r.read().decode("utf-8"))
    except (_urllib_error.HTTPError, Exception):
        return None

    if not isinstance(data, list) or len(data) < 2:
        return None
    meta = data[0]
    ctxs = data[1]
    universe = (meta or {}).get("universe") or []

    for i, u in enumerate(universe):
        if (u or {}).get("name") == canon:
            if i >= len(ctxs):
                return None
            ctx = ctxs[i] or {}
            raw = ctx.get("funding")
            mark = ctx.get("markPx")
            if raw is None:
                return None
            try:
                raw_f = float(raw)
                mark_f = float(mark) if mark is not None else None
            except (TypeError, ValueError):
                return None
            # Hyperliquid's funding is already relative per hour. Convert
            # to per-8h percent: × 8 × 100.
            per_8h_pct = raw_f * 8.0 * 100.0
            # Hyperliquid doesn't publish a predicted next-cycle rate via
            # the metaAndAssetCtxs endpoint — leave as None.
            return FundingRateResult(
                symbol=canon,
                rate_per_8h_pct=per_8h_pct,
                raw_rate=raw_f,
                raw_unit="decimal per hour (relative)",
                source="hyperliquid",
                mark_price=mark_f,
                perp_symbol=canon,
                rate_per_8h_pct_next=None,
            )
    return None


# ----------------------------- Deribit ----------------------------------

# Deribit publishes per-8h funding directly on the ticker.
#   `current_funding` = current per-8h decimal rate (e.g. 0.0001 = 0.01%)
#   `funding_8h`      = realised average over the previous 8h
# Endpoint: /public/ticker?instrument_name=BTC-PERPETUAL
# Linear perps follow the {COIN}-PERPETUAL or {COIN}_USDC-PERPETUAL pattern.


def _deribit_perp_symbol(symbol: str) -> str:
    return f"{symbol.upper().lstrip('$')}-PERPETUAL"


def fetch_deribit_funding(symbol: str) -> Optional[FundingRateResult]:
    """Pull Deribit's current per-8h funding rate. Predicted next-cycle
    isn't exposed by Deribit's public API in a stable form, so left None."""
    canon = symbol.upper().lstrip("$")
    instrument = _deribit_perp_symbol(canon)
    url = f"https://www.deribit.com/api/v2/public/ticker?instrument_name={instrument}"
    data = get_json(url)
    if "_error" in data or "result" not in data:
        return None
    r = data["result"] or {}
    raw = r.get("current_funding")
    mark = r.get("mark_price")
    if raw is None:
        return None
    try:
        raw_f = float(raw)
        mark_f = float(mark) if mark is not None else None
    except (TypeError, ValueError):
        return None
    # Already per-8h decimal → percent is *100.
    per_8h_pct = raw_f * 100.0
    return FundingRateResult(
        symbol=canon,
        rate_per_8h_pct=per_8h_pct,
        raw_rate=raw_f,
        raw_unit="decimal per 8h (relative)",
        source="deribit",
        mark_price=mark_f,
        perp_symbol=instrument,
        rate_per_8h_pct_next=None,
    )


# ----------------------------- BitMEX -----------------------------------

# BitMEX publishes funding on /instrument:
#   `fundingRate`           = current per-8h decimal
#   `indicativeFundingRate` = next-cycle predicted decimal
#   `fundingInterval`       = ISO format always equivalent to 8h for perps
# Symbol convention: XBTUSD for BTC, ETHUSD for ETH, etc.

_BITMEX_ALIASES: dict[str, str] = {
    "BTC": "XBT",
    "WBTC": "XBT",
}


def _bitmex_perp_symbol(symbol: str) -> str:
    canon = symbol.upper().lstrip("$")
    canon = _BITMEX_ALIASES.get(canon, canon)
    return f"{canon}USD"


def fetch_bitmex_funding(symbol: str) -> Optional[FundingRateResult]:
    """Pull BitMEX's current and predicted per-8h funding rates."""
    canon = symbol.upper().lstrip("$")
    target = _bitmex_perp_symbol(canon)
    url = f"https://www.bitmex.com/api/v1/instrument?symbol={target}"
    data = get_json(url)
    if "_error" in data:
        return None
    # /instrument returns a list (filtered by symbol).
    if not isinstance(data, list) or not data:
        return None
    r = data[0] or {}
    raw = r.get("fundingRate")
    raw_next = r.get("indicativeFundingRate")
    mark = r.get("markPrice")
    if raw is None:
        return None
    try:
        raw_f = float(raw)
        mark_f = float(mark) if mark is not None else None
    except (TypeError, ValueError):
        return None
    per_8h_pct = raw_f * 100.0
    per_8h_pct_next: Optional[float] = None
    if raw_next is not None:
        try:
            per_8h_pct_next = float(raw_next) * 100.0
        except (TypeError, ValueError):
            per_8h_pct_next = None
    return FundingRateResult(
        symbol=canon,
        rate_per_8h_pct=per_8h_pct,
        raw_rate=raw_f,
        raw_unit="decimal per 8h (relative)",
        source="bitmex",
        mark_price=mark_f,
        perp_symbol=target,
        rate_per_8h_pct_next=per_8h_pct_next,
    )


# ----------------------------- public entry point ------------------------

# Source order — names looked up dynamically at call time (NOT captured as
# function references) so tests can monkeypatch each source individually.
# Order matters: Kraken Futures (deepest books for majors) → Hyperliquid
# (newer DEX-perp coverage) → Deribit (regulated venue, BTC/ETH focus) →
# BitMEX (legacy but still listed). First non-None result wins.
_SOURCE_NAMES: tuple[str, ...] = (
    "fetch_kraken_funding",
    "fetch_hyperliquid_funding",
    "fetch_deribit_funding",
    "fetch_bitmex_funding",
)


def fetch_funding_rate(symbol: str) -> Optional[FundingRateResult]:
    """Try each configured source in order; return the first hit."""
    if not symbol or not symbol.strip():
        return None
    import sys as _sys
    module = _sys.modules[__name__]
    for name in _SOURCE_NAMES:
        fetcher = getattr(module, name)
        result = fetcher(symbol)
        if result is not None:
            return result
    return None
