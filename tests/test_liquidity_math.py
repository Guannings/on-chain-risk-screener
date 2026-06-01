"""Tests for the exit-liquidity simulator.

The math is constant-product V2-style. Most assertions use known-answer values
derived analytically from the formulas in the module docstring.

IMPORTANT: this module reports two metrics. Tests that exercise "how thin is
this pool for my buy" assert against PRICE IMPACT, not ROUND-TRIP. Round-trip
on a V2 AMM is bounded by ~2*fee regardless of trade size (the fee stays in
the pool and is partially recovered by the immediate sell), so it cannot
distinguish between a healthy pool and a death-trap pool. Price impact does.
"""

from __future__ import annotations

import math

import pytest

from memecheck.common.liquidity_math import (
    DEFAULT_FEE_BPS,
    cp_amm_out,
    derive_reserves,
    fee_bps_for_dex,
    max_safe_buy_usd,
    round_trip_slippage,
)


# ----------------------------- fee lookup --------------------------------

def test_fee_bps_known_dexes() -> None:
    assert fee_bps_for_dex("raydium") == 25
    assert fee_bps_for_dex("orca") == 30
    assert fee_bps_for_dex("uniswap") == 30
    assert fee_bps_for_dex("pumpfun") == 100
    assert fee_bps_for_dex("pump-fun") == 100


def test_fee_bps_unknown_falls_back() -> None:
    assert fee_bps_for_dex(None) == DEFAULT_FEE_BPS
    assert fee_bps_for_dex("totally-made-up-dex") == DEFAULT_FEE_BPS


# ----------------------------- cp_amm_out --------------------------------

def test_zero_inputs_return_zero() -> None:
    assert cp_amm_out(0, 100, 100, 30) == 0
    assert cp_amm_out(10, 0, 100, 30) == 0
    assert cp_amm_out(10, 100, 0, 30) == 0


def test_cp_amm_out_known_value_no_fee() -> None:
    # No fee: A_out = R_out * A_in / (R_in + A_in)
    # 100 in, 1000/1000 reserves -> 1000 * 100 / 1100 = 90.909...
    out = cp_amm_out(100, 1000, 1000, 0)
    assert math.isclose(out, 90.909090909, rel_tol=1e-6)


def test_cp_amm_out_with_fee_reduces_output() -> None:
    out_no_fee = cp_amm_out(100, 1000, 1000, 0)
    out_with_fee = cp_amm_out(100, 1000, 1000, 30)
    assert out_with_fee < out_no_fee


# ----------------------------- derive_reserves ---------------------------

def test_derive_reserves_from_explicit_base_quote() -> None:
    pair = {
        "liquidity": {"base": 1_000_000, "quote": 10},
        "priceUsd": "0.00115",
        "priceNative": "0.00001",  # quote-per-base; quote = SOL-like at $115
    }
    out = derive_reserves(pair)
    assert out is not None
    base_r, quote_r, qp_usd = out
    assert base_r == 1_000_000
    assert quote_r == 10
    # quote_price_usd = priceUsd / priceNative = 115
    assert math.isclose(qp_usd, 115.0, rel_tol=1e-6)


def test_derive_reserves_fallback_from_liq_usd() -> None:
    # No base/quote in liquidity, only liquidity.usd. Fallback path.
    pair = {
        "liquidity": {"usd": 2300},
        "priceUsd": "0.00115",
        "priceNative": "0.00001",
    }
    out = derive_reserves(pair)
    assert out is not None
    base_r, quote_r, qp_usd = out
    # USD ≈ 2 * R_q * P_q_usd → R_q ≈ 2300 / (2 * 115) = 10
    # priceNative = R_q / R_b → R_b = 10 / 0.00001 = 1_000_000
    assert math.isclose(quote_r, 10.0, rel_tol=1e-6)
    assert math.isclose(base_r, 1_000_000.0, rel_tol=1e-6)


def test_derive_reserves_returns_none_on_missing_prices() -> None:
    assert derive_reserves({"liquidity": {"base": 100, "quote": 1}}) is None
    assert derive_reserves({"priceUsd": "0.5", "priceNative": "0"}) is None
    assert derive_reserves({"priceUsd": "abc", "priceNative": "1"}) is None


# ----------------------------- price impact (the real metric) ------------

def test_price_impact_on_deep_pool_is_negligible() -> None:
    """A $10 buy on a $1M-deep pool should have tiny price impact (<1%)."""
    sim = round_trip_slippage(
        buy_size_usd=10,
        base_reserve=10_000_000,
        quote_reserve=10_000,
        quote_price_usd=100.0,
        fee_bps=30,
    )
    assert sim["price_impact_pct"] is not None
    assert sim["price_impact_pct"] < 1.0


def test_price_impact_on_thin_pool_is_severe() -> None:
    """A $10 buy on a $100-deep pool should have >5% price impact.

    Reproduces the realistic case: small buy on a thin pool. The price impact
    metric is what would have warned the user before they sent the trade.
    """
    # Pool quote-side USD = 1 * 100 = $100
    sim = round_trip_slippage(
        buy_size_usd=10,
        base_reserve=333_333,
        quote_reserve=1,
        quote_price_usd=100.0,
        fee_bps=100,  # pump.fun
    )
    assert sim["price_impact_pct"] is not None
    assert sim["price_impact_pct"] > 5.0


def test_price_impact_scales_with_trade_size() -> None:
    """Bigger buy → bigger price impact, monotonically, on the same pool."""
    pool = dict(base_reserve=1_000_000, quote_reserve=3, quote_price_usd=100.0, fee_bps=30)
    impacts = [
        round_trip_slippage(buy_size_usd=s, **pool)["price_impact_pct"]
        for s in (1, 10, 50, 100, 200)
    ]
    assert all(a < b for a, b in zip(impacts, impacts[1:]))


def test_round_trip_is_small_even_on_thin_pool() -> None:
    """Sanity check: on a V2 AMM, round-trip slippage is bounded by ~2*fee
    regardless of pool depth. Verifies the docstring claim."""
    sim = round_trip_slippage(
        buy_size_usd=10,
        base_reserve=1_000_000,
        quote_reserve=3,
        quote_price_usd=100.0,
        fee_bps=100,  # 1% fee → expect round-trip ~ 1.5-2%
    )
    assert sim["round_trip_pct"] < 3.0


# ----------------------------- max_safe_buy_usd --------------------------

def test_max_safe_buy_stays_under_target_impact() -> None:
    """Buying max_safe_buy_usd must produce a price impact at or under target."""
    pool = dict(base_reserve=1_000_000, quote_reserve=100, quote_price_usd=50.0, fee_bps=30)
    target_pct = 5.0
    safe = max_safe_buy_usd(target_pct, **pool)
    sim = round_trip_slippage(buy_size_usd=safe, **pool)
    assert sim["price_impact_pct"] is not None
    assert sim["price_impact_pct"] <= target_pct + 0.1


def test_max_safe_buy_returns_zero_on_invalid_pool() -> None:
    assert max_safe_buy_usd(5.0, 0, 100, 50.0) == 0
    assert max_safe_buy_usd(5.0, 100, 0, 50.0) == 0
    assert max_safe_buy_usd(0, 100, 100, 50.0) == 0


def test_max_safe_buy_larger_pool_allows_larger_buy() -> None:
    target_pct = 5.0
    safe_thin = max_safe_buy_usd(target_pct, 1_000_000, 10, 100.0, fee_bps=30)
    safe_deep = max_safe_buy_usd(target_pct, 1_000_000, 1_000, 100.0, fee_bps=30)
    assert safe_deep > safe_thin


# ----------------------------- effective vs displayed --------------------

def test_displayed_vs_effective_price_diverge_on_thin_pool() -> None:
    sim = round_trip_slippage(
        buy_size_usd=50,
        base_reserve=1_000_000,
        quote_reserve=5,
        quote_price_usd=100.0,
        fee_bps=30,
    )
    assert sim["effective_buy_price_usd"] > sim["displayed_price_usd"]
    assert sim["price_impact_pct"] > 0


# ----------------------------- zero-buy safety ---------------------------

def test_zero_buy_returns_none_fields() -> None:
    sim = round_trip_slippage(0, 1000, 10, 100, 30)
    assert sim["price_impact_pct"] is None
    assert sim["tokens_out"] is None
    assert sim["round_trip_pct"] is None


# ----------------------------- the user's scenario, sanity-checked -------

def test_realistic_thin_pool_scenario_via_price_impact() -> None:
    """Reconstruct the user's lived experience: a $10 buy on what looked
    like a 6000% pump but was actually a pool with ~$100 quote depth.
    The price impact at buy time is what would have warned them off.
    """
    sim = round_trip_slippage(
        buy_size_usd=10,
        base_reserve=200_000_000,
        quote_reserve=1,         # 1 SOL ≈ $100 of quote-side depth
        quote_price_usd=100.0,
        fee_bps=100,             # pump.fun
    )
    # The price you'd actually pay is significantly above the displayed
    # marginal price.
    assert sim["price_impact_pct"] > 5.0
    # And the effective price strictly exceeds the displayed price.
    assert sim["effective_buy_price_usd"] > sim["displayed_price_usd"]
