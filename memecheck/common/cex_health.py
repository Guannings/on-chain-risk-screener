"""CEX perpetual pre-trade health screen.

Pulls the full /tickers payload from Kraken Futures (non-China, public,
no auth) and runs threshold-based checks specific to centralised
perpetual contracts:

  - Funding-rate magnitude (annualised)
  - Funding-direction vs trade side (if --side given): is funding a
    headwind or tailwind for the position you'd take?
  - Mark-vs-index basis (perp premium/discount → mean-reversion risk)
  - 24h volume (book health / slippage on stop fills)
  - Bid-ask spread at the touch (immediate execution cost)
  - 24h range and last move (entering after a violent candle is costly)

Returns the same (flags, notes, metrics) shape as the DEX scanners so
the downstream verdict + format helpers are consistent across the two
sides of the repo.
"""

from __future__ import annotations

from typing import Any, Optional

from memecheck.common.funding import _kraken_perp_symbol
from memecheck.common.http import get_json


# ----------------------------- thresholds --------------------------------

EXTREME_FUNDING_PER_8H_PCT: float = 0.05     # 0.05% per 8h ≈ 55% APY → extreme
ELEVATED_FUNDING_PER_8H_PCT: float = 0.02    # noteworthy but not extreme
BASIS_BLOWOUT_PCT: float = 0.50              # mark vs index > 0.5%
THIN_VOLUME_USD: float = 1_000_000           # < $1M / 24h = thin book
WIDE_SPREAD_BPS: float = 20.0                # bid-ask > 20 bps = thin touch
EXTREME_24H_MOVE_PCT: float = 10.0           # already > 10% in 24h = chasing

CEX_HARD_PASS_FLAG_COUNT: int = 3            # >= this many flags → HARD PASS


# ----------------------------- fetcher -----------------------------------


def fetch_cex_ticker(symbol: str) -> Optional[dict[str, Any]]:
    """Pull the full Kraken Futures ticker for a symbol. None if not listed."""
    url = "https://futures.kraken.com/derivatives/api/v3/tickers"
    data = get_json(url)
    if "_error" in data:
        return None
    target = _kraken_perp_symbol(symbol)
    for ticker in data.get("tickers", []) or []:
        if ticker.get("symbol") == target:
            return ticker
    return None


# ----------------------------- analyser ----------------------------------


def _f(x: Any) -> Optional[float]:
    """Safe float conversion. None on failure."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def analyze_cex_perp(
    ticker: dict[str, Any],
    side: Optional[str] = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Return (flags, notes, metrics) for one CEX perp ticker.

    side: 'long' / 'short' / None. When given, the funding analysis flags
    a headwind based on the side rather than just the magnitude.
    """
    flags: list[str] = []
    notes: list[str] = []

    symbol = ticker.get("symbol", "?")
    mark = _f(ticker.get("markPrice"))
    index = _f(ticker.get("indexPrice"))
    last = _f(ticker.get("last"))
    bid = _f(ticker.get("bid"))
    ask = _f(ticker.get("ask"))
    vol24h = _f(ticker.get("vol24h"))
    oi = _f(ticker.get("openInterest"))
    change24h = _f(ticker.get("change24h"))
    high24h = _f(ticker.get("high24h"))
    low24h = _f(ticker.get("low24h"))
    raw_funding = _f(ticker.get("fundingRate"))            # absolute USD/contract/hour
    raw_funding_pred = _f(ticker.get("fundingRatePrediction"))

    # Kraken's fundingRate is absolute USD/contract/hour → percent per 8h.
    funding_8h_pct: Optional[float] = None
    funding_8h_pred_pct: Optional[float] = None
    if raw_funding is not None and mark and mark > 0:
        funding_8h_pct = (raw_funding / mark) * 8 * 100
    if raw_funding_pred is not None and mark and mark > 0:
        funding_8h_pred_pct = (raw_funding_pred / mark) * 8 * 100

    vol_usd = (vol24h * mark) if (vol24h is not None and mark) else None
    oi_usd = (oi * mark) if (oi is not None and mark) else None
    spread_bps = (
        ((ask - bid) / mark * 10_000) if (bid and ask and mark and mark > 0) else None
    )
    basis_pct = (
        ((mark - index) / index * 100) if (mark and index and index > 0) else None
    )

    metrics: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "mark": mark,
        "index": index,
        "last": last,
        "bid": bid,
        "ask": ask,
        "spread_bps": spread_bps,
        "vol_24h_contracts": vol24h,
        "vol_24h_usd_approx": vol_usd,
        "open_interest_contracts": oi,
        "open_interest_usd_approx": oi_usd,
        "change_24h_pct": change24h,
        "high_24h": high24h,
        "low_24h": low24h,
        "funding_per_8h_pct": funding_8h_pct,
        "funding_per_8h_pct_predicted": funding_8h_pred_pct,
        "funding_apy_pct": (funding_8h_pct * 3 * 365) if funding_8h_pct is not None else None,
        "basis_pct": basis_pct,
    }

    # ----- notes header -------------------------------------------------
    notes.append(f"Symbol: {symbol}")
    if mark is not None and index is not None and last is not None:
        notes.append(
            f"Mark ${mark:,.4f}   Index ${index:,.4f}   Last ${last:,.4f}"
        )
    if vol_usd is not None or oi_usd is not None:
        notes.append(
            f"24h volume {'$' + format(vol_usd, ',.0f') if vol_usd else 'n/a'}   "
            f"Open interest {'$' + format(oi_usd, ',.0f') if oi_usd else 'n/a'}"
        )

    # ----- funding ------------------------------------------------------
    if funding_8h_pct is not None:
        apy = funding_8h_pct * 3 * 365
        notes.append(
            f"Funding {funding_8h_pct:+.4f}% per 8h  ({apy:+.1f}% APY annualised)"
        )
        if funding_8h_pred_pct is not None:
            notes.append(f"Next-cycle prediction: {funding_8h_pred_pct:+.4f}% per 8h")

        if abs(funding_8h_pct) >= EXTREME_FUNDING_PER_8H_PCT:
            flags.append(
                f"Funding extreme: {funding_8h_pct:+.3f}% per 8h "
                f"({apy:+.0f}% APY). Positioning is crowded — mean-reversion risk."
            )
        elif abs(funding_8h_pct) >= ELEVATED_FUNDING_PER_8H_PCT:
            notes.append(
                f"Funding elevated. Watch for positioning unwind if it goes parabolic."
            )

        # Side-aware tailwind/headwind.
        if side == "long":
            if funding_8h_pct > 0:
                daily_cost = funding_8h_pct * 3
                notes.append(
                    f"→ As a LONG you PAY funding (-{daily_cost:.3f}%/day — headwind)."
                )
                if funding_8h_pct >= ELEVATED_FUNDING_PER_8H_PCT:
                    flags.append(
                        f"Long pays {daily_cost:.3f}%/day funding. TP target must "
                        "clear this just to break even."
                    )
            elif funding_8h_pct < 0:
                notes.append(
                    f"→ As a LONG you RECEIVE funding (+{abs(funding_8h_pct)*3:.3f}%/day — tailwind)."
                )
        elif side == "short":
            if funding_8h_pct < 0:
                daily_cost = abs(funding_8h_pct) * 3
                notes.append(
                    f"→ As a SHORT you PAY funding (-{daily_cost:.3f}%/day — headwind)."
                )
                if abs(funding_8h_pct) >= ELEVATED_FUNDING_PER_8H_PCT:
                    flags.append(
                        f"Short pays {daily_cost:.3f}%/day funding. TP target must "
                        "clear this just to break even."
                    )
            elif funding_8h_pct > 0:
                notes.append(
                    f"→ As a SHORT you RECEIVE funding (+{funding_8h_pct*3:.3f}%/day — tailwind)."
                )

    # ----- basis (mark vs index) ---------------------------------------
    if basis_pct is not None:
        notes.append(f"Basis (mark − index): {basis_pct:+.3f}%")
        if abs(basis_pct) >= BASIS_BLOWOUT_PCT:
            kind = "premium" if basis_pct > 0 else "discount"
            flags.append(
                f"Perp trading at {abs(basis_pct):.2f}% {kind} to index. "
                "Convergence to index can blow stops in seconds."
            )

    # ----- volume ------------------------------------------------------
    if vol_usd is not None and vol_usd < THIN_VOLUME_USD:
        flags.append(
            f"Thin 24h volume (${vol_usd:,.0f} < ${THIN_VOLUME_USD:,.0f}) — "
            "expect material slippage on stop fills."
        )

    # ----- spread ------------------------------------------------------
    if spread_bps is not None:
        notes.append(f"Bid-ask spread: {spread_bps:.1f} bps")
        if spread_bps > WIDE_SPREAD_BPS:
            flags.append(
                f"Spread {spread_bps:.0f} bps is wide (>{WIDE_SPREAD_BPS:.0f} bps). "
                "Round-trip immediate-execution cost is high."
            )

    # ----- 24h move ----------------------------------------------------
    if change24h is not None:
        notes.append(f"24h change: {change24h:+.2f}%")
        if abs(change24h) >= EXTREME_24H_MOVE_PCT:
            kind = "rallied" if change24h > 0 else "dropped"
            flags.append(
                f"Already {kind} {abs(change24h):.1f}% in 24h. Entering after "
                "a violent move is statistically a coinflip."
            )

    return flags, notes, metrics


# ----------------------------- verdict -----------------------------------


def make_cex_verdict(flags: list[str]) -> str:
    if not flags:
        return (
            "No automatic red flags — but 'no flags' != 'wise entry'. "
            "Your edge still has to come from somewhere."
        )
    if len(flags) >= CEX_HARD_PASS_FLAG_COUNT:
        return "HARD PASS"
    return "RISKY — proceed only with money already written off"


def exit_code_for_cex(verdict: str) -> int:
    if verdict.startswith("HARD PASS") or verdict.startswith("RISKY"):
        return 1
    return 0
