"""Analyzer tests for DexScreener data — fully mocked."""

from __future__ import annotations

import memecheck


def test_clean_token_raises_no_flags(clean_dex_pairs) -> None:
    primary = clean_dex_pairs[0]
    flags, _notes, metrics = memecheck.analyze_dexscreener(primary, clean_dex_pairs)
    assert flags == []
    # Aggregation across the two pools
    assert metrics["liquidity_usd"] == 10_000_000
    assert metrics["volume_24h_usd"] == 4_800_000
    assert metrics["pool_count"] == 2
    assert metrics["chain"] == "solana"


def test_thin_pool_flags_liquidity_and_ratio_and_sells(thin_dex_pairs) -> None:
    primary = thin_dex_pairs[0]
    flags, _notes, metrics = memecheck.analyze_dexscreener(primary, thin_dex_pairs)
    assert metrics["liquidity_usd"] == 8_000
    # Multiple flags expected: thin liq + low liq/mc + sells > buys * 1.5
    joined = " ".join(flags).lower()
    assert "thin liquidity" in joined
    assert "liq/mc" in joined
    assert "sells heavily outpacing buys" in joined


def test_wash_trading_flagged(wash_dex_pairs) -> None:
    primary = wash_dex_pairs[0]
    flags, _notes, metrics = memecheck.analyze_dexscreener(primary, wash_dex_pairs)
    assert metrics["vol_liq_ratio"] == 100.0
    assert any("wash trading" in f.lower() for f in flags)


def test_metrics_shape_is_stable(clean_dex_pairs) -> None:
    primary = clean_dex_pairs[0]
    _flags, _notes, metrics = memecheck.analyze_dexscreener(primary, clean_dex_pairs)
    # JSON consumers depend on these keys existing.
    for k in (
        "chain", "dex", "base_symbol", "quote_symbol", "pool_count",
        "liquidity_usd", "market_cap_usd", "volume_24h_usd",
        "buys_24h", "sells_24h", "age_hours", "liq_mc_ratio", "vol_liq_ratio",
    ):
        assert k in metrics
