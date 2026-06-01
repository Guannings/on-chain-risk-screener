"""End-to-end runner — drives the async loop with a FakeSource."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Iterable

from memecheck.monitor.runner import run_monitor
from memecheck.monitor.source import LiquidityEvent, LiquiditySource


class FakeSource(LiquiditySource):
    """Yields a fixed sequence of events with no real time elapsed."""

    def __init__(self, events: Iterable[LiquidityEvent]) -> None:
        self._events = list(events)

    async def stream(self) -> AsyncIterator[LiquidityEvent]:
        for ev in self._events:
            yield ev


def _mk_event(ts: float, liq_usd: float, price: float = 1.0) -> LiquidityEvent:
    return LiquidityEvent(
        ts=ts,
        base_reserve=100.0,
        quote_reserve=1.0,
        quote_price_usd=100.0,
        liquidity_usd=liq_usd,
        price_usd=price,
        source="test",
    )


def test_runner_consumes_all_events_when_source_ends() -> None:
    events = [
        _mk_event(ts=0,   liq_usd=1000.0),
        _mk_event(ts=5,   liq_usd=900.0),
        _mk_event(ts=10,  liq_usd=800.0),
    ]
    stats = asyncio.run(run_monitor(FakeSource(events), print_each_tick=False))
    assert stats.ticks == 3
    assert stats.last_event is events[-1]
    assert stats.last_decision is not None
    assert stats.last_decision.action == "NONE"


def test_runner_respects_max_ticks() -> None:
    events = [_mk_event(ts=i, liq_usd=1000.0) for i in range(10)]
    stats = asyncio.run(
        run_monitor(FakeSource(events), max_ticks=3, print_each_tick=False)
    )
    assert stats.ticks == 3
    assert stats.last_event is events[2]


def test_runner_decision_metrics_include_windowed_deltas() -> None:
    events = [
        _mk_event(ts=0,   liq_usd=1000.0),
        _mk_event(ts=60,  liq_usd=900.0),
        _mk_event(ts=120, liq_usd=800.0),
    ]
    stats = asyncio.run(run_monitor(FakeSource(events), print_each_tick=False))
    assert stats.last_decision is not None
    m = stats.last_decision.metrics
    # 60s window from ts=120 → cutoff=60 → oldest in window is t=60 (liq=900)
    # delta = (800 / 900 - 1) * 100 = -11.11%
    assert m["delta_60s_pct"] is not None
    assert m["delta_60s_pct"] < -10.0
    # Baseline delta: 800 vs 1000 = -20%
    assert m["delta_vs_baseline_pct"] is not None
    assert -20.5 < m["delta_vs_baseline_pct"] < -19.5


def test_runner_with_empty_source_finishes_cleanly() -> None:
    stats = asyncio.run(run_monitor(FakeSource([]), print_each_tick=False))
    assert stats.ticks == 0
    assert stats.last_event is None
    assert stats.last_decision is None
