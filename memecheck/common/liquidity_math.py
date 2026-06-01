"""Exit-liquidity simulator: would your buy actually be exitable?

The displayed price on a DEX (the "market price" you see on a chart) is the
*marginal* price of swapping one infinitesimal unit. The price you actually
realise on a non-trivial trade is determined by constant-product (or
concentrated-liquidity) math against finite pool reserves. On thin pools, even
small trades walk the curve a long way and the price you actually pay can be
materially higher than what's displayed.

Two metrics are reported:

  1. PRICE IMPACT — (effective_buy_price - displayed_price) / displayed_price.
     This is the metric that grows quickly as your trade size approaches the
     pool's depth. It is the primary warning signal: a 10% price impact means
     you paid 10% more than the chart showed. This is what bites you when
     you buy into a thin pool.

  2. ROUND-TRIP SLIPPAGE — the loss you'd take if you bought and immediately
     re-sold. Reported as a sanity check, but note: on a constant-product
     AMM with fee f, immediate round-trip slippage is bounded by approximately
     2 * f regardless of trade size (the fee stays in the pool and is partially
     recovered on the sell). It is NOT a useful measure of "stuck bag" risk —
     that comes from the pool changing over time after your buy.

Math (constant-product V2-style AMM, single hop):

  Given gross input amount A_in and reserves (R_in, R_out), with fee f in
  basis points (Raydium 25, Uniswap V2 30, pump.fun 100), the output is:

      A_out = R_out * A_in * (1 - f/10000) / (R_in + A_in * (1 - f/10000))

  For a buy then immediate sell starting with X USD of quote:
      1. tokens_out = cp_amm_out(X_quote,    R_quote, R_base, f)
      2. After buy:  R_quote' = R_quote + X_quote,  R_base' = R_base - tokens_out
      3. quote_back = cp_amm_out(tokens_out, R_base', R_quote', f)
      4. round_trip_pct  = 1 - (quote_back / X_quote)
      5. price_impact    = (X_quote / tokens_out) / (R_quote / R_base) - 1

Concentrated-liquidity pools (Uni V3, Orca Whirlpool, Meteora DLMM) violate
the constant-product assumption around the active tick. We treat the V2
calculation as a lower bound on the impact on those — it tends to
under-estimate price impact when the trade crosses ticks, so the verdict is
conservative in the right direction (says "at least this bad") rather than
falsely optimistic.
"""

from __future__ import annotations

from typing import Any, Optional

# Per-DEX swap fee in basis points. Conservative defaults when uncertain.
_DEX_FEE_BPS: dict[str, int] = {
    "raydium": 25,
    "raydium-clmm": 25,
    "raydium-cpmm": 25,
    "orca": 30,
    "whirlpool": 30,
    "meteora": 30,
    "meteora-dlmm": 30,
    "uniswap": 30,
    "uniswap-v2": 30,
    "uniswap-v3": 30,  # fee tier is per-pool; 30 is the most common
    "pancakeswap": 25,
    "sushiswap": 30,
    "pumpfun": 100,
    "pump-fun": 100,
    "pumpswap": 100,
}

DEFAULT_FEE_BPS: int = 30  # Uniswap-V2-equivalent — slightly conservative


def fee_bps_for_dex(dex_id: Optional[str]) -> int:
    """Look up the typical fee for a DexScreener dexId, or fall back to default."""
    if not dex_id:
        return DEFAULT_FEE_BPS
    return _DEX_FEE_BPS.get(dex_id.lower(), DEFAULT_FEE_BPS)


def cp_amm_out(amount_in: float, reserve_in: float, reserve_out: float, fee_bps: int) -> float:
    """Constant-product AMM output.

    amount_in: gross input (the full amount the user is sending in)
    reserve_in: pool reserve of the input token
    reserve_out: pool reserve of the output token
    fee_bps: pool swap fee in basis points (Raydium 25, Uni V2 30, pump.fun 100)
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0.0
    effective_in = amount_in * (1 - fee_bps / 10_000)
    return reserve_out * effective_in / (reserve_in + effective_in)


def derive_reserves(pair: dict[str, Any]) -> Optional[tuple[float, float, float]]:
    """Pull (base_reserve, quote_reserve, quote_price_usd) from a DexScreener pair.

    Returns None if there isn't enough information to derive them. Uses the
    pair's `liquidity.base`/`liquidity.quote` when available, otherwise falls
    back to deriving from `liquidity.usd` + `priceUsd` + `priceNative`
    assuming a balanced V2-style pool.
    """
    liq = pair.get("liquidity") or {}
    price_usd_raw = pair.get("priceUsd")
    price_native_raw = pair.get("priceNative")

    try:
        price_usd = float(price_usd_raw) if price_usd_raw is not None else None
        price_native = float(price_native_raw) if price_native_raw is not None else None
    except (TypeError, ValueError):
        return None

    if not price_usd or not price_native or price_native == 0:
        return None

    # priceUsd = USD per base token; priceNative = quote per base token
    # quote_price_usd = USD per quote token
    quote_price_usd = price_usd / price_native

    base = liq.get("base")
    quote = liq.get("quote")
    if base and quote:
        try:
            return float(base), float(quote), quote_price_usd
        except (TypeError, ValueError):
            pass

    # Fallback: derive from liquidity.usd assuming a balanced pool.
    # USD ≈ 2 * R_quote * P_quote_usd  →  R_quote ≈ USD / (2 * P_quote_usd)
    # priceNative = R_quote / R_base  →  R_base = R_quote / priceNative
    liq_usd = liq.get("usd")
    if liq_usd:
        try:
            r_quote = float(liq_usd) / (2 * quote_price_usd)
            r_base = r_quote / price_native
            return r_base, r_quote, quote_price_usd
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    return None


def round_trip_slippage(
    buy_size_usd: float,
    base_reserve: float,
    quote_reserve: float,
    quote_price_usd: float,
    fee_bps: int = DEFAULT_FEE_BPS,
) -> dict[str, Any]:
    """Simulate buying buy_size_usd then immediately selling the resulting bag.

    Returns a dict with:
      buy_size_usd:            input echo
      tokens_out:              tokens received from the buy
      realised_usd:            USD you'd get back from immediately re-selling
      round_trip_pct:          1 - realised_usd/buy_size_usd (positive = loss)
      displayed_price_usd:     marginal price of one infinitesimal unit
      effective_buy_price_usd: actual USD/token you paid on the buy
      price_impact_pct:        (effective / displayed) - 1
      pool_quote_depth_usd:    R_quote * P_quote_usd (one-sided USD depth)
    """
    out: dict[str, Any] = {
        "buy_size_usd": buy_size_usd,
        "tokens_out": None,
        "realised_usd": None,
        "round_trip_pct": None,
        "displayed_price_usd": None,
        "effective_buy_price_usd": None,
        "price_impact_pct": None,
        "pool_quote_depth_usd": None,
        "fee_bps": fee_bps,
    }
    if (
        buy_size_usd <= 0
        or base_reserve <= 0
        or quote_reserve <= 0
        or quote_price_usd <= 0
    ):
        return out

    out["pool_quote_depth_usd"] = quote_reserve * quote_price_usd
    out["displayed_price_usd"] = (quote_reserve / base_reserve) * quote_price_usd

    # Step 1: buy. Convert USD → quote, then swap quote → base.
    buy_size_quote = buy_size_usd / quote_price_usd
    tokens_out = cp_amm_out(buy_size_quote, quote_reserve, base_reserve, fee_bps)
    if tokens_out <= 0:
        return out
    out["tokens_out"] = tokens_out
    out["effective_buy_price_usd"] = buy_size_usd / tokens_out
    out["price_impact_pct"] = (
        out["effective_buy_price_usd"] / out["displayed_price_usd"] - 1
    ) * 100

    # Reserves after the buy.
    new_quote = quote_reserve + buy_size_quote
    new_base = base_reserve - tokens_out

    # Step 2: immediately sell the bag back.
    quote_back = cp_amm_out(tokens_out, new_base, new_quote, fee_bps)
    realised_usd = quote_back * quote_price_usd
    out["realised_usd"] = realised_usd
    out["round_trip_pct"] = (1 - realised_usd / buy_size_usd) * 100
    return out


def max_safe_buy_usd(
    target_price_impact_pct: float,
    base_reserve: float,
    quote_reserve: float,
    quote_price_usd: float,
    fee_bps: int = DEFAULT_FEE_BPS,
    iterations: int = 50,
) -> float:
    """Binary-search the largest buy size that stays at or under the target
       PRICE IMPACT percentage.

    Price impact, not round-trip slippage, is the meaningful constraint
    here — round-trip is bounded by ~2*fee on a constant-product AMM
    regardless of size, while price impact grows monotonically with trade
    size relative to pool depth.
    """
    if base_reserve <= 0 or quote_reserve <= 0 or quote_price_usd <= 0:
        return 0.0
    if target_price_impact_pct <= 0:
        return 0.0
    lo, hi = 0.0, quote_reserve * quote_price_usd  # upper bound: full pool quote-side USD
    for _ in range(iterations):
        mid = (lo + hi) / 2
        if mid == 0:
            break
        r = round_trip_slippage(mid, base_reserve, quote_reserve, quote_price_usd, fee_bps)
        impact = r.get("price_impact_pct")
        if impact is None or impact > target_price_impact_pct:
            hi = mid
        else:
            lo = mid
    return lo
