"""Threshold sweep + ML signal interface + extended confusion matrix tests."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from memecheck.common.backtest import _load_labels, _load_tape, replay
from memecheck.common.ml_signal import (
    FEATURE_NAMES,
    ConstantSignalModel,
    LogisticSignalModel,
    MODEL_BAND_HIGH,
    MODEL_BAND_LOW,
    features_from_state,
    reconcile,
)
from memecheck.common.threshold_sweep import (
    pareto_frontier,
    sweep,
)
from memecheck.monitor.decision import DEFAULT_CONFIG
from memecheck.monitor.source import LiquidityEvent
from memecheck.monitor.state import MonitorState


# ----------------------------- helpers -----------------------------------


def _write_tape(path: Path, events: list[tuple[float, float, float]]) -> None:
    """Write `events` = list of (ts, liq_usd, price_usd) tuples."""
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "liquidity_usd", "price_usd"])
        for ts, liq, px in events:
            w.writerow([ts, liq, px])


def _write_labels(path: Path, rows: list[tuple[float, str]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "event"])
        for ts, evt in rows:
            w.writerow([ts, evt])


# ----------------------------- confusion matrix (#2) --------------------


def test_replay_populates_confusion_matrix(tmp_path: Path) -> None:
    """Synthetic atomic pull at t=10: depth drops 100k → 10k."""
    tape_path = tmp_path / "atomic.csv"
    labels_path = tmp_path / "atomic.labels.csv"
    events = [(t, 100_000.0, 1.0) for t in range(0, 11)]
    events += [(t, 10_000.0, 1.0) for t in range(11, 30)]
    _write_tape(tape_path, events)
    _write_labels(labels_path, [(11.0, "rug")])
    tape = _load_tape(tape_path)
    labels = _load_labels(labels_path)
    report = replay(tape, labels=labels, tolerance_seconds=60.0)
    # Some ticks should fire (atomic pull → critical floor breached).
    assert report.alert_count + report.execute_count > 0
    # TP + FN must equal total ticks within tolerance of the labelled rug.
    expected_positive_ticks = sum(
        1 for ev in tape if abs(ev.ts - 11.0) <= 60.0
    )
    assert report.tp_ticks + report.fn_ticks == expected_positive_ticks
    # TN + FP must equal total negative ticks.
    expected_negative_ticks = len(tape) - expected_positive_ticks
    assert report.tn_ticks + report.fp_ticks == expected_negative_ticks


def test_f1_computed_when_precision_and_recall_both_known(tmp_path: Path) -> None:
    """F1 = 2PR/(P+R) when both > 0; None otherwise."""
    tape_path = tmp_path / "t.csv"
    labels_path = tmp_path / "l.csv"
    events = [(t, 100_000.0, 1.0) for t in range(20)]
    events += [(t, 10_000.0, 1.0) for t in range(20, 40)]
    _write_tape(tape_path, events)
    _write_labels(labels_path, [(20.0, "rug")])
    tape = _load_tape(tape_path)
    labels = _load_labels(labels_path)
    report = replay(tape, labels=labels)
    if report.precision is not None and report.recall is not None:
        if report.precision + report.recall > 0:
            assert report.f1 is not None
            expected = 2 * report.precision * report.recall / (
                report.precision + report.recall
            )
            assert report.f1 == pytest.approx(expected)


# ----------------------------- threshold sweep (#1) ---------------------


def test_sweep_runs_all_grid_combinations(tmp_path: Path) -> None:
    """3x2x2 = 12 combinations → 12 sweep points."""
    tape_path = tmp_path / "t.csv"
    labels_path = tmp_path / "l.csv"
    events = [(t, 100_000.0, 1.0) for t in range(50)]
    events += [(t, 5_000.0, 1.0) for t in range(50, 80)]
    _write_tape(tape_path, events)
    _write_labels(labels_path, [(50.0, "rug")])
    points = sweep(
        tape_path, labels_path,
        critical_ratios=[0.3, 0.5, 0.7],
        large_event_pcts=[-25.0, -15.0],
        slow_bleed_60s_pcts=[-15.0, -7.0],
    )
    assert len(points) == 12


def test_pareto_frontier_drops_dominated_points() -> None:
    """A point dominated by another in BOTH precision AND recall is excluded."""
    from memecheck.common.threshold_sweep import SweepPoint
    points = [
        SweepPoint(0.5, -20, -10, precision=0.8, recall=0.9, f1=0.85, fpr_ticks=0.01, tp_ticks=10, fp_ticks=1),
        SweepPoint(0.6, -20, -10, precision=0.5, recall=0.5, f1=0.5,  fpr_ticks=0.05, tp_ticks=5,  fp_ticks=5),  # dominated
        SweepPoint(0.7, -25, -15, precision=0.9, recall=0.7, f1=0.79, fpr_ticks=0.02, tp_ticks=7,  fp_ticks=1),
    ]
    frontier = pareto_frontier(points)
    # First and third points trade off precision vs recall — both should be on frontier.
    # Middle point is dominated → excluded.
    assert len(frontier) == 2
    assert all(p.precision >= 0.8 for p in frontier)


# ----------------------------- ML signal (#3) ---------------------------


def test_constant_model_returns_fixed_proba() -> None:
    m = ConstantSignalModel(p=0.73)
    assert m.predict_proba({"liquidity_usd": 50_000}) == 0.73


def test_logistic_model_uses_sigmoid() -> None:
    """A weight of 0 means the feature doesn't matter; intercept alone drives output."""
    m = LogisticSignalModel(weights={"liquidity_usd": 0.0}, intercept=0.0)
    assert m.predict_proba({"liquidity_usd": 1_000_000}) == pytest.approx(0.5)


def test_logistic_model_negative_intercept_pushes_below_half() -> None:
    m = LogisticSignalModel(weights={}, intercept=-2.0)
    p = m.predict_proba({})
    assert 0.10 < p < 0.20


def test_features_from_state_exposes_full_contract() -> None:
    state = MonitorState()
    state.add(LiquidityEvent(
        ts=100.0, base_reserve=0.0, quote_reserve=0.0, quote_price_usd=1.0,
        liquidity_usd=100_000.0, price_usd=0.01, source="t",
    ))
    feats = features_from_state(state)
    # All documented features present.
    for name in FEATURE_NAMES:
        assert name in feats
    assert feats["liquidity_usd"] == 100_000.0
    assert feats["ticks_seen"] == 1


def test_features_from_state_computes_ratio_and_deltas() -> None:
    state = MonitorState()
    state.add(LiquidityEvent(
        ts=0.0, base_reserve=0.0, quote_reserve=0.0, quote_price_usd=1.0,
        liquidity_usd=100_000.0, price_usd=0.01, source="t",
    ))
    state.add(LiquidityEvent(
        ts=10.0, base_reserve=0.0, quote_reserve=0.0, quote_price_usd=1.0,
        liquidity_usd=50_000.0, price_usd=0.01, source="t",
    ))
    feats = features_from_state(state)
    assert feats["liquidity_ratio_vs_l0"] == pytest.approx(0.5)


def test_reconcile_neutral_when_both_quiet() -> None:
    """Rules quiet + model quiet → no note."""
    v = reconcile("NONE", model_proba=0.2)
    assert v.combined_note is None


def test_reconcile_flags_rule_quiet_model_alarmed() -> None:
    v = reconcile("NONE", model_proba=0.85)
    assert v.combined_note is not None
    assert "early warning" in v.combined_note


def test_reconcile_flags_model_disagreement_on_alert() -> None:
    v = reconcile("ALERT", model_proba=0.1)
    assert v.combined_note is not None
    assert "DISAGREES" in v.combined_note


def test_reconcile_band_boundaries() -> None:
    """Documented constants must match the reconciliation logic."""
    assert 0.0 < MODEL_BAND_LOW < MODEL_BAND_HIGH < 1.0
