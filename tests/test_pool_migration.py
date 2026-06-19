"""Pool-migration auto-resolve — exercised against a stubbed DexScreener.

The source's `_maybe_resolve_migration` reads three external surfaces:
  1. fetch_dexscreener (via _resolve_pool) — to find the *current* deepest pool
  2. get_json — to peek at the candidate pool's depth
We stub both at the module boundary so these tests have zero network.
"""

from __future__ import annotations

import time
from typing import Optional

import pytest

from memecheck.monitor import source as src_mod
from memecheck.monitor.source import (
    DexScreenerPollSource,
    LiquidityEvent,
    _ResolvedPool,
    MIGRATION_NOTICE_PREFIX,
)


def _make_pool(addr: str, dex_id: str = "raydium") -> _ResolvedPool:
    return _ResolvedPool(
        chain="solana",
        pair_address=addr,
        base_symbol="TOK",
        quote_symbol="SOL",
        dex_id=dex_id,
    )


def _make_event(liq_usd: float, pool: _ResolvedPool) -> LiquidityEvent:
    return LiquidityEvent(
        ts=time.time(),
        base_reserve=1.0,
        quote_reserve=liq_usd / 2 / 200.0,    # arbitrary
        quote_price_usd=200.0,
        liquidity_usd=liq_usd,
        price_usd=0.01,
        source="dexscreener-poll",
        chain="solana",
        pair_address=pool.pair_address,
    )


@pytest.fixture
def stub_resolver(monkeypatch):
    """Make construction skip the live resolve, then let each test set
    what `_resolve_pool` returns thereafter."""
    initial = _make_pool("OLD_POOL", dex_id="pumpfun")

    def fake_resolve(addr, forced_chain):
        return fake_resolve.next_pool

    fake_resolve.next_pool = initial    # type: ignore[attr-defined]
    monkeypatch.setattr(src_mod, "_resolve_pool", fake_resolve)
    return fake_resolve


def test_no_migration_when_liquidity_stable(stub_resolver, monkeypatch):
    notices: list[str] = []
    s = DexScreenerPollSource(
        "TOKEN_MINT", interval_seconds=1.0, on_error=lambda m: notices.append(m),
    )
    # Set baseline.
    s._initial_liquidity_usd = 100_000.0
    event = _make_event(95_000.0, s._pool)
    s._maybe_resolve_migration(event)
    assert all(not n.startswith(MIGRATION_NOTICE_PREFIX) for n in notices)


def test_no_migration_when_no_deeper_pool_exists(stub_resolver, monkeypatch):
    """Liquidity collapsed but the resolver returns the SAME pool — no migration."""
    notices: list[str] = []
    s = DexScreenerPollSource(
        "TOKEN_MINT", interval_seconds=1.0, on_error=lambda m: notices.append(m),
    )
    s._initial_liquidity_usd = 100_000.0
    # Resolver returns same pool address → no candidate.
    stub_resolver.next_pool = _make_pool("OLD_POOL", "pumpfun")

    event = _make_event(10_000.0, s._pool)    # 10% of baseline → trigger
    s._maybe_resolve_migration(event)
    assert all(not n.startswith(MIGRATION_NOTICE_PREFIX) for n in notices)


def test_migration_fires_when_deeper_pool_appears(stub_resolver, monkeypatch):
    """Classic pump.fun → Raydium case: old pool drained, new pool deeper."""
    notices: list[str] = []
    s = DexScreenerPollSource(
        "TOKEN_MINT", interval_seconds=1.0, on_error=lambda m: notices.append(m),
    )
    s._initial_liquidity_usd = 100_000.0

    # Make the resolver return a different (deeper) pool.
    new_pool = _make_pool("NEW_POOL", "raydium")
    stub_resolver.next_pool = new_pool

    # Stub the candidate-peek HTTP call to return a deep pool payload.
    def fake_get_json(url, timeout=15):
        return {
            "pair": {
                "chainId": "solana",
                "pairAddress": "NEW_POOL",
                "baseToken": {"address": "TOK", "symbol": "TOK"},
                "quoteToken": {"address": "SOL", "symbol": "SOL"},
                "liquidity": {"usd": 80_000.0, "base": 5_000_000, "quote": 200},
                "priceUsd": "0.016",
                "priceNative": "0.00008",
            }
        }
    monkeypatch.setattr(src_mod, "get_json", fake_get_json)

    event = _make_event(10_000.0, s._pool)    # baseline×10% → trigger
    s._maybe_resolve_migration(event)

    # Migration should have fired: pool switched, notice emitted.
    assert s._pool.pair_address == "NEW_POOL"
    assert any(n.startswith(MIGRATION_NOTICE_PREFIX) for n in notices)


def test_migration_cooldown_prevents_thrashing(stub_resolver, monkeypatch):
    """A second trigger within 60s should NOT re-run the candidate check."""
    notices: list[str] = []
    s = DexScreenerPollSource(
        "TOKEN_MINT", interval_seconds=1.0, on_error=lambda m: notices.append(m),
    )
    s._initial_liquidity_usd = 100_000.0
    # Pretend we JUST checked.
    s._last_migration_check_ts = time.time()

    call_count = {"n": 0}
    def counting_resolver(addr, forced_chain):
        call_count["n"] += 1
        return _make_pool("WOULD_BE_NEW", "raydium")
    monkeypatch.setattr(src_mod, "_resolve_pool", counting_resolver)

    event = _make_event(5_000.0, s._pool)
    s._maybe_resolve_migration(event)
    assert call_count["n"] == 0    # cooldown skipped the check entirely


def test_migration_disabled_flag_skips_check(stub_resolver, monkeypatch):
    notices: list[str] = []
    s = DexScreenerPollSource(
        "TOKEN_MINT", interval_seconds=1.0, on_error=lambda m: notices.append(m),
        enable_migration_resolve=False,
    )
    s._initial_liquidity_usd = 100_000.0

    call_count = {"n": 0}
    def counting_resolver(addr, forced_chain):
        call_count["n"] += 1
        return _make_pool("X", "raydium")
    monkeypatch.setattr(src_mod, "_resolve_pool", counting_resolver)

    event = _make_event(5_000.0, s._pool)
    s._maybe_resolve_migration(event)
    assert call_count["n"] == 0
