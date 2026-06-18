"""CEX perp event source — polls Kraken Futures for live ticker data.

Drop-in replacement for the DEX `DexScreenerPollSource` so the existing
state + decision + alert pipeline can monitor centralised perpetuals
without changes downstream.

Produces `CexPerpEvent` instead of `LiquidityEvent` because the per-tick
data shape is different: CEX perps don't have "liquidity in USD" as the
primary signal — funding rate, basis, and open interest matter more.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from memecheck.common.cex_health import fetch_cex_ticker


@dataclass(frozen=True)
class CexPerpEvent:
    """One snapshot of a CEX perp's state."""

    ts: float
    symbol: str
    mark: float
    index: Optional[float]
    funding_per_8h_pct: Optional[float]
    funding_next_pct: Optional[float]
    basis_pct: Optional[float]
    vol_24h_usd: Optional[float]
    open_interest_usd: Optional[float]
    spread_bps: Optional[float]
    change_24h_pct: Optional[float]
    source: str = "kraken-futures"
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


class CexPerpPollSource:
    """Polls Kraken Futures /tickers and yields CexPerpEvent per cycle."""

    def __init__(
        self,
        symbol: str,
        interval_seconds: float = 30.0,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._symbol = symbol.upper().lstrip("$")
        self._interval = interval_seconds
        self._on_error = on_error or (lambda msg: None)
        # Eager resolution so the user gets immediate feedback if the symbol
        # isn't listed.
        ticker = fetch_cex_ticker(symbol)
        if ticker is None:
            raise RuntimeError(
                f"Symbol {self._symbol} not found on Kraken Futures"
            )
        self._first_ticker: Optional[dict[str, Any]] = ticker

    @property
    def symbol(self) -> str:
        return self._symbol

    def _poll_once(self) -> Optional[CexPerpEvent]:
        # Reuse the eager first fetch for the first tick so we don't waste
        # an extra round trip.
        ticker: Optional[dict[str, Any]]
        if self._first_ticker is not None:
            ticker = self._first_ticker
            self._first_ticker = None
        else:
            ticker = fetch_cex_ticker(self._symbol)
        if ticker is None:
            self._on_error(f"poll error: {self._symbol} disappeared from ticker feed")
            return None
        return self._event_from_ticker(ticker)

    def _event_from_ticker(self, t: dict[str, Any]) -> Optional[CexPerpEvent]:
        def _f(x: Any) -> Optional[float]:
            try:
                return float(x) if x is not None else None
            except (TypeError, ValueError):
                return None

        mark = _f(t.get("markPrice"))
        index = _f(t.get("indexPrice"))
        if mark is None or mark <= 0:
            self._on_error("poll error: missing or zero mark price")
            return None

        raw_fund = _f(t.get("fundingRate"))
        raw_fund_pred = _f(t.get("fundingRatePrediction"))
        funding_8h_pct = (raw_fund / mark) * 8 * 100 if raw_fund is not None else None
        funding_next_pct = (
            (raw_fund_pred / mark) * 8 * 100 if raw_fund_pred is not None else None
        )

        basis_pct = (
            ((mark - index) / index * 100) if (index is not None and index > 0) else None
        )

        vol24 = _f(t.get("vol24h"))
        vol_usd = vol24 * mark if vol24 is not None else None
        oi = _f(t.get("openInterest"))
        oi_usd = oi * mark if oi is not None else None

        bid = _f(t.get("bid"))
        ask = _f(t.get("ask"))
        spread_bps = (
            ((ask - bid) / mark * 10_000) if (bid is not None and ask is not None) else None
        )

        return CexPerpEvent(
            ts=time.time(),
            symbol=self._symbol,
            mark=mark,
            index=index,
            funding_per_8h_pct=funding_8h_pct,
            funding_next_pct=funding_next_pct,
            basis_pct=basis_pct,
            vol_24h_usd=vol_usd,
            open_interest_usd=oi_usd,
            spread_bps=spread_bps,
            change_24h_pct=_f(t.get("change24h")),
        )

    async def stream(self) -> AsyncIterator[CexPerpEvent]:
        loop = asyncio.get_running_loop()
        while True:
            event = await loop.run_in_executor(None, self._poll_once)
            if event is not None:
                yield event
            await asyncio.sleep(self._interval)
