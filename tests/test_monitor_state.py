"""MonitorState — ring buffer, baseline, windowed deltas."""

from __future__ import annotations

import math

from memecheck.monitor.source import LiquidityEvent
from memecheck.monitor.state import MonitorState


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


# ----------------------------- basic shape -------------------------------

def test_empty_state_has_no_baseline_or_current() -> None:
    s = MonitorState()
    assert s.count == 0
    assert s.current is None
    assert s.baseline is None
    assert s.liquidity_vs_baseline_pct() is None
    assert s.windowed_delta_pct(10) is None


def test_first_event_sets_both_baseline_and_current() -> None:
    s = MonitorState()
    e = _mk_event(ts=100, liq_usd=1000.0)
    s.add(e)
    assert s.count == 1
    assert s.current is e
    assert s.baseline is e


def test_baseline_does_not_change_with_later_events() -> None:
    s = MonitorState()
    e0 = _mk_event(ts=100, liq_usd=1000.0)
    e1 = _mk_event(ts=200, liq_usd=500.0)
    s.add(e0)
    s.add(e1)
    assert s.baseline is e0
    assert s.current is e1


# ----------------------------- liquidity_vs_baseline ---------------------

def test_liquidity_vs_baseline_pct_known_drop() -> None:
    s = MonitorState()
    s.add(_mk_event(ts=100, liq_usd=1000.0))
    s.add(_mk_event(ts=200, liq_usd=500.0))
    # Drop from 1000 to 500 = -50%
    assert math.isclose(s.liquidity_vs_baseline_pct(), -50.0, rel_tol=1e-6)


def test_liquidity_vs_baseline_pct_growth() -> None:
    s = MonitorState()
    s.add(_mk_event(ts=100, liq_usd=1000.0))
    s.add(_mk_event(ts=200, liq_usd=1500.0))
    assert math.isclose(s.liquidity_vs_baseline_pct(), 50.0, rel_tol=1e-6)


def test_liquidity_vs_baseline_handles_zero_baseline() -> None:
    s = MonitorState()
    s.add(_mk_event(ts=100, liq_usd=0.0))
    s.add(_mk_event(ts=200, liq_usd=500.0))
    assert s.liquidity_vs_baseline_pct() is None  # avoid div-by-zero


# ----------------------------- windowed_delta_pct ------------------------

def test_windowed_delta_within_window() -> None:
    """Events at t=0, 5, 10, 15. windowed_delta_pct(10) should compare t=15
    to the oldest event at or after t=5 (i.e. the t=5 event)."""
    s = MonitorState()
    s.add(_mk_event(ts=0, liq_usd=1000.0))
    s.add(_mk_event(ts=5, liq_usd=900.0))
    s.add(_mk_event(ts=10, liq_usd=800.0))
    s.add(_mk_event(ts=15, liq_usd=700.0))
    # Window 10s back from ts=15 → cutoff is ts >= 5. Oldest in window = t=5.
    # delta = (700 / 900 - 1) * 100 = -22.22%
    delta = s.windowed_delta_pct(10)
    assert delta is not None
    assert math.isclose(delta, -22.22222, rel_tol=1e-4)


def test_windowed_delta_window_outside_buffer_returns_none() -> None:
    """If all events are within the lookback window, the oldest IS the
    current event when there's only one — so there's no comparison."""
    s = MonitorState()
    s.add(_mk_event(ts=100, liq_usd=1000.0))
    # Only one event — no prior point to compare against.
    assert s.windowed_delta_pct(10) is None


def test_windowed_delta_no_event_in_window() -> None:
    """If the only event is well before the window, no useful delta."""
    s = MonitorState()
    s.add(_mk_event(ts=0, liq_usd=1000.0))
    s.add(_mk_event(ts=1000, liq_usd=500.0))
    # cutoff = 1000 - 10 = 990. The only event >= 990 is current itself.
    assert s.windowed_delta_pct(10) is None


def test_windowed_delta_multiple_windows() -> None:
    """Same series, different windows give different deltas."""
    s = MonitorState()
    s.add(_mk_event(ts=0,   liq_usd=1000.0))
    s.add(_mk_event(ts=60,  liq_usd=900.0))   # -10% over 60s
    s.add(_mk_event(ts=120, liq_usd=800.0))   # -20% over 120s from t=0
    s.add(_mk_event(ts=180, liq_usd=700.0))   # current
    # 10s window: cutoff = 170, no event in [170, 180) except current → None
    assert s.windowed_delta_pct(10) is None
    # 60s window: cutoff = 120, oldest in window = t=120 (liq=800)
    # delta = (700/800 - 1) * 100 = -12.5%
    d60 = s.windowed_delta_pct(60)
    assert d60 is not None
    assert math.isclose(d60, -12.5, rel_tol=1e-6)
    # 200s window: cutoff = -20, oldest = t=0 (liq=1000) → -30%
    d200 = s.windowed_delta_pct(200)
    assert d200 is not None
    assert math.isclose(d200, -30.0, rel_tol=1e-6)


# ----------------------------- ring buffer eviction ----------------------

def test_ring_buffer_evicts_oldest_when_full() -> None:
    s = MonitorState(buffer_size=3)
    for i in range(5):
        s.add(_mk_event(ts=i, liq_usd=1000.0 + i))
    # Only the last 3 should remain.
    assert s.count == 3
    assert s.current is not None
    assert s.current.ts == 4
    # Baseline is NOT the first event in the buffer — it's the first ever observed.
    assert s.baseline is not None
    assert s.baseline.ts == 0


# ----------------------------- window_covered ----------------------------

def test_window_covered_only_when_span_sufficient() -> None:
    s = MonitorState()
    assert s.window_covered(10) is False
    s.add(_mk_event(ts=0, liq_usd=1000.0))
    assert s.window_covered(10) is False  # one event, no span
    s.add(_mk_event(ts=5, liq_usd=900.0))
    assert s.window_covered(5) is True
    assert s.window_covered(10) is False  # span is 5s, not 10s
