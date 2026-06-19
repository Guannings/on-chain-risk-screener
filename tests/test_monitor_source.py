"""DexScreenerPollSource — resolution + event construction, mocked HTTP."""

from __future__ import annotations

import asyncio

import pytest

from memecheck.monitor import source as source_mod
from memecheck.monitor.source import (
    DexScreenerPollSource,
    LiquidityEvent,
    _event_from_pair,
    _resolve_pool,
)


# ----------------------------- _event_from_pair --------------------------

def _mk_pair(usd: float = 1000.0, price_usd: str = "0.50", price_native: str = "0.005"):
    """Build a minimal DexScreener pair payload."""
    return {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "PoolPubkey111111111111111111111111111111111",
        "baseToken": {"symbol": "TEST"},
        "quoteToken": {"symbol": "SOL"},
        "liquidity": {"usd": usd},
        "priceUsd": price_usd,
        "priceNative": price_native,
    }


def _mk_pool():
    return source_mod._ResolvedPool(
        chain="solana",
        pair_address="PoolPubkey111111111111111111111111111111111",
        base_symbol="TEST",
        quote_symbol="SOL",
        dex_id="raydium",
    )


def test_event_from_pair_carries_all_metrics() -> None:
    ev = _event_from_pair(_mk_pair(usd=2300.0), _mk_pool())
    assert ev is not None
    assert isinstance(ev, LiquidityEvent)
    assert ev.chain == "solana"
    assert ev.pair_address == "PoolPubkey111111111111111111111111111111111"
    assert ev.source == "dexscreener-poll"
    assert ev.price_usd == pytest.approx(0.5, rel=1e-6)
    # quote_price_usd = priceUsd / priceNative = 0.5 / 0.005 = 100
    assert ev.quote_price_usd == pytest.approx(100.0, rel=1e-6)
    # Derived from liquidity.usd fallback: R_q = 2300 / (2 * 100) = 11.5
    assert ev.quote_reserve == pytest.approx(11.5, rel=1e-6)


def test_event_from_pair_returns_none_on_unusable_payload() -> None:
    # priceNative = 0 makes derive_reserves return None.
    assert (
        _event_from_pair(_mk_pair(price_native="0"), _mk_pool()) is None
    )


# ----------------------------- _resolve_pool -----------------------------

def test_resolve_pool_uses_fetch_dexscreener(monkeypatch) -> None:
    fake_primary = {
        "chainId": "ethereum",
        "dexId": "uniswap",
        "pairAddress": "0xPair",
        "baseToken": {"symbol": "PEPE"},
        "quoteToken": {"symbol": "WETH"},
    }
    monkeypatch.setattr(
        source_mod,
        "fetch_dex_pairs",
        lambda a, c=None: (fake_primary, [fake_primary], None),
    )
    pool = _resolve_pool("0xWhatever", forced_chain=None)
    assert pool.chain == "ethereum"
    assert pool.pair_address == "0xPair"
    assert pool.dex_id == "uniswap"
    assert pool.base_symbol == "PEPE"


def test_resolve_pool_raises_on_no_pair(monkeypatch) -> None:
    monkeypatch.setattr(
        source_mod,
        "fetch_dex_pairs",
        lambda a, c=None: (None, [], "No DEX pairs found"),
    )
    with pytest.raises(RuntimeError, match="Cannot resolve pool"):
        _resolve_pool("0xnone", forced_chain=None)


# ----------------------------- DexScreenerPollSource ---------------------

def test_poll_source_yields_events_then_stops(monkeypatch) -> None:
    """End-to-end: mock fetch_dexscreener for resolution and get_json for
    polling, drive the async stream for 3 ticks, assert event shape."""
    fake_primary = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "PoolPubkey1",
        "baseToken": {"symbol": "TEST"},
        "quoteToken": {"symbol": "SOL"},
    }
    monkeypatch.setattr(
        source_mod,
        "fetch_dex_pairs",
        lambda a, c=None: (fake_primary, [fake_primary], None),
    )

    # Mock the per-tick poll response. Decrement liquidity each call so the
    # test also confirms repeated polls produce distinct events.
    call_count = {"n": 0}
    def fake_get_json(url: str, timeout: int = 15):
        call_count["n"] += 1
        return {
            "pair": _mk_pair(usd=2000.0 - 500.0 * call_count["n"]),
        }
    monkeypatch.setattr(source_mod, "get_json", fake_get_json)

    src = DexScreenerPollSource("AddrDoesntMatter", interval_seconds=0.001)

    async def drain_three() -> list[LiquidityEvent]:
        events: list[LiquidityEvent] = []
        async for ev in src.stream():
            events.append(ev)
            if len(events) >= 3:
                break
        return events

    events = asyncio.run(drain_three())
    assert len(events) == 3
    # Liquidity values should strictly decrease as the mock decrements them.
    libs = [e.liquidity_usd for e in events]
    assert libs[0] > libs[1] > libs[2]
    # All three events should carry the resolved pool's metadata.
    for e in events:
        assert e.chain == "solana"
        assert e.pair_address == "PoolPubkey1"


def test_poll_source_swallows_bad_polls(monkeypatch) -> None:
    """A single bad poll should not crash the stream; the next good poll
    should be yielded normally."""
    fake_primary = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "PoolPubkey1",
        "baseToken": {"symbol": "TEST"},
        "quoteToken": {"symbol": "SOL"},
    }
    monkeypatch.setattr(
        source_mod,
        "fetch_dex_pairs",
        lambda a, c=None: (fake_primary, [fake_primary], None),
    )
    call_count = {"n": 0}
    def fake_get_json(url: str, timeout: int = 15):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"_error": "HTTP 503 boom"}
        return {"pair": _mk_pair(usd=2000.0)}

    errors: list[str] = []
    monkeypatch.setattr(source_mod, "get_json", fake_get_json)
    src = DexScreenerPollSource(
        "AddrDoesntMatter",
        interval_seconds=0.001,
        on_error=lambda msg: errors.append(msg),
    )

    async def drain_one() -> LiquidityEvent:
        async for ev in src.stream():
            return ev
        raise AssertionError("no events")

    ev = asyncio.run(drain_one())
    assert ev.liquidity_usd > 0
    assert any("503" in e for e in errors)
