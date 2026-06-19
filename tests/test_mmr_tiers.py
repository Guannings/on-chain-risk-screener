"""Tiered MMR lookup tables — venue + symbol + notional-aware."""

from __future__ import annotations

import pytest

from memecheck.common.mmr_tiers import (
    DEFAULT_VENUE,
    lookup_mmr_tier,
    supported_venues,
)


def test_supported_venues_includes_majors() -> None:
    venues = supported_venues()
    assert "kraken-futures" in venues
    assert "bybit" in venues
    assert "deribit" in venues


def test_small_position_gets_lowest_tier() -> None:
    """A $1,000 position is below every tier ceiling — gets the most lenient MMR."""
    t = lookup_mmr_tier("kraken-futures", "ETH", 1_000)
    assert t.mmr <= 0.005


def test_large_position_gets_higher_tier() -> None:
    """A $5M position on Kraken should land in a higher-MMR tier than $1k."""
    small = lookup_mmr_tier("kraken-futures", "ETH", 1_000)
    huge = lookup_mmr_tier("kraken-futures", "ETH", 5_000_000)
    assert huge.mmr > small.mmr


def test_btc_alias_uses_btc_table_on_bybit() -> None:
    """Bybit has a dedicated BTC schedule that's more lenient at large sizes."""
    btc = lookup_mmr_tier("bybit", "BTC", 1_500_000)
    alt = lookup_mmr_tier("bybit", "DOGE", 1_500_000)
    # BTC table allows 0.4% MMR up to $2M; default starts ramping at $100k.
    assert btc.mmr < alt.mmr


def test_unknown_venue_falls_back_to_constant() -> None:
    t = lookup_mmr_tier("bogus-venue", "BTC", 100_000)
    assert t.mmr == 0.005


def test_none_venue_uses_default() -> None:
    t = lookup_mmr_tier(None, "BTC", 100_000)
    assert t.mmr > 0


def test_tier_max_leverage_consistent_with_mmr() -> None:
    """Higher tier (larger position) should never have higher max-leverage
    than a lower tier — venues only ratchet leverage down, not up."""
    for venue in supported_venues():
        prev_lev = float("inf")
        for notional in (10_000, 100_000, 1_000_000, 10_000_000, 100_000_000):
            t = lookup_mmr_tier(venue, "BTC", notional)
            assert t.max_leverage <= prev_lev
            prev_lev = t.max_leverage
