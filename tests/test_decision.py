"""Decision engine: thresholds, debounce, severity ordering."""

from __future__ import annotations

from memecheck.monitor.decision import (
    ACTION_ALERT,
    ACTION_EXECUTE,
    ACTION_NONE,
    DEFAULT_CONFIG,
    Decider,
    DecisionConfig,
    decide_noop,
)
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


# ----------------------------- noop --------------------------------------


def test_noop_always_returns_none() -> None:
    s = MonitorState()
    s.add(_mk_event(ts=0, liq_usd=1000.0))
    d = decide_noop(s)
    assert d.action == ACTION_NONE


# ----------------------------- critical floor ----------------------------


def test_critical_floor_fires_execute_immediately() -> None:
    s = MonitorState()
    s.add(_mk_event(ts=0, liq_usd=1000.0))       # baseline = 1000
    s.add(_mk_event(ts=10, liq_usd=400.0))       # 40% of L0, below 0.5 floor

    d = Decider().decide(s)
    assert d.action == ACTION_EXECUTE
    assert "critical floor" in (d.reason or "").lower() or "critical" in (d.reason or "").lower()


def test_critical_floor_does_not_fire_above_threshold() -> None:
    s = MonitorState()
    s.add(_mk_event(ts=0, liq_usd=1000.0))
    s.add(_mk_event(ts=10, liq_usd=700.0))       # 70% of L0, above 0.5 floor
    # Also doesn't trip the 10s window (only one event back, but 10s ago)
    d = Decider().decide(s)
    # 700 / 1000 = -30% over 10s, which IS a large-event trigger. Debounce
    # is 2, so first tick this fires we should still be in streak=1, no execute yet.
    assert d.action == ACTION_NONE


# ----------------------------- large single event ------------------------


def test_large_event_requires_debounce() -> None:
    """A single -25% tick at 10s should set streak=1 but NOT fire execute
    (debounce=2 by default). The second consecutive -25% tick triggers."""
    d = Decider()
    s = MonitorState()
    s.add(_mk_event(ts=0, liq_usd=1000.0))
    s.add(_mk_event(ts=10, liq_usd=750.0))       # -25% over 10s
    dec1 = d.decide(s)
    assert dec1.action == ACTION_NONE             # streak=1, debounce=2

    s.add(_mk_event(ts=20, liq_usd=560.0))       # -25% more
    dec2 = d.decide(s)
    assert dec2.action == ACTION_EXECUTE          # streak=2 → fires


def test_large_event_streak_resets_on_recovery() -> None:
    """If the condition stops holding, the streak should reset."""
    d = Decider()
    s = MonitorState()
    s.add(_mk_event(ts=0, liq_usd=1000.0))
    s.add(_mk_event(ts=10, liq_usd=750.0))        # -25% → streak=1
    d.decide(s)
    s.add(_mk_event(ts=20, liq_usd=760.0))        # +1.3% → streak resets
    d.decide(s)
    s.add(_mk_event(ts=30, liq_usd=600.0))        # back to drop, but new streak
    dec = d.decide(s)
    # First tick of a new bleed — streak=1, not yet executing.
    assert dec.action == ACTION_NONE


# ----------------------------- slow bleed --------------------------------


def test_slow_bleed_fires_alert_immediately() -> None:
    """When both 60s and 300s windows are bad, ALERT fires on the very
    first qualifying tick (no debounce for ALERT level).

    Strict windowed-delta semantics require 300s of history, so this test
    sets up a t=0 baseline, an intermediate event 60s before current
    (for the 60s window), and a current event exactly 300s after baseline
    (just enough for the 300s window to evaluate).
    """
    d = Decider()
    s = MonitorState()
    s.add(_mk_event(ts=0,    liq_usd=1000.0))
    s.add(_mk_event(ts=240,  liq_usd=900.0))      # 60s before current
    s.add(_mk_event(ts=300,  liq_usd=800.0))      # current — span = 300
    # 60s: (800/900 - 1)*100 = -11.1%  (worse than -10)
    # 300s: (800/1000 - 1)*100 = -20%  (worse than -15)
    dec = d.decide(s)
    assert dec.action == ACTION_ALERT
    assert "slow bleed" in (dec.reason or "").lower()


def test_slow_bleed_escalates_to_execute_after_debounce() -> None:
    """After slow_bleed_debounce consecutive ALERT-eligible ticks, escalate to EXECUTE."""
    cfg = DecisionConfig(slow_bleed_debounce=3)
    d = Decider(cfg)
    s = MonitorState()
    s.add(_mk_event(ts=0,   liq_usd=1000.0))
    s.add(_mk_event(ts=240, liq_usd=900.0))     # sets up 60s window for t=300

    # Tick 1: t=300, both windows trigger. Streak=1, debounce=3 → ALERT.
    s.add(_mk_event(ts=300, liq_usd=800.0))
    dec1 = d.decide(s)
    assert dec1.action == ACTION_ALERT, f"tick 1: got {dec1.action}"

    # Tick 2: t=360. d60 cutoff=300 → oldest=t=300 (liq=800). delta=-12.5%.
    # d300 cutoff=60 → oldest=t=240 (liq=900). delta=(700/900-1)*100 = -22.2%.
    s.add(_mk_event(ts=360, liq_usd=700.0))
    dec2 = d.decide(s)
    assert dec2.action == ACTION_ALERT, f"tick 2: got {dec2.action}"

    # Tick 3: t=420. Streak=3 → EXECUTE.
    s.add(_mk_event(ts=420, liq_usd=600.0))
    dec3 = d.decide(s)
    assert dec3.action == ACTION_EXECUTE, f"tick 3: got {dec3.action}"


# ----------------------------- severity ordering -------------------------


def test_severity_ordering_execute_beats_alert() -> None:
    """Critical floor triggers EXECUTE which should beat any concurrent ALERT-level signal."""
    d = Decider()
    s = MonitorState()
    s.add(_mk_event(ts=0,   liq_usd=1000.0))
    s.add(_mk_event(ts=300, liq_usd=400.0))       # well below critical floor
    dec = d.decide(s)
    assert dec.action == ACTION_EXECUTE


# ----------------------------- pass-through metrics ----------------------


def test_decision_metrics_always_present() -> None:
    """Even on NONE, metrics should include the windowed deltas for audit."""
    d = Decider()
    s = MonitorState()
    s.add(_mk_event(ts=0, liq_usd=1000.0))
    dec = d.decide(s)
    assert dec.action == ACTION_NONE
    assert "delta_vs_baseline_pct" in dec.metrics
    assert "delta_10s_pct" in dec.metrics
    assert "delta_60s_pct" in dec.metrics
    assert "delta_300s_pct" in dec.metrics
    assert dec.metrics["liquidity_usd"] == 1000.0
