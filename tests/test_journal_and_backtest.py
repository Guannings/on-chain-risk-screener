"""Smoke tests for the trade journal + backtest harness."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from memecheck.common.backtest import _load_tape, replay
from memecheck.common.journal import list_entries, log_entry
from memecheck.monitor.decision import DEFAULT_CONFIG, DecisionConfig
from memecheck.monitor.source import LiquidityEvent


# ----------------------------- journal ----------------------------------


def test_journal_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "j.sqlite"
    id1 = log_entry(
        venue="dex", symbol_or_addr="0xPEPE", side="long",
        account_usd=1000, entry_price=0.0001, stop_price=0.00009,
        leverage=1.0, position_notional_usd=200, risk_usd=10,
        verdict="RISKY", refused=False, forced=False,
        funding_per_8h_pct=None, path=db,
    )
    assert id1 > 0
    entries = list_entries(path=db)
    assert len(entries) == 1
    e = entries[0]
    assert e.venue == "dex"
    assert e.symbol_or_addr == "0xPEPE"
    assert e.side == "long"
    assert e.verdict == "RISKY"


def test_journal_filter_by_venue(tmp_path: Path) -> None:
    db = tmp_path / "j.sqlite"
    log_entry(venue="dex", symbol_or_addr="0xA", side="long",
              account_usd=1000, entry_price=1, stop_price=0.95,
              leverage=1.0, position_notional_usd=200, risk_usd=10,
              verdict="clean", refused=False, forced=False, path=db)
    log_entry(venue="cex", symbol_or_addr="XRP", side="short",
              account_usd=1000, entry_price=1.18, stop_price=1.22,
              leverage=5.0, position_notional_usd=290, risk_usd=10,
              verdict="clean", refused=False, forced=False, path=db)
    dex_only = list_entries(venue="dex", path=db)
    cex_only = list_entries(venue="cex", path=db)
    assert len(dex_only) == 1 and dex_only[0].symbol_or_addr == "0xA"
    assert len(cex_only) == 1 and cex_only[0].symbol_or_addr == "XRP"


# ----------------------------- backtest --------------------------------


def _write_tape(path: Path, rows: list[tuple[float, float, float]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "liquidity_usd", "price_usd"])
        for r in rows:
            w.writerow(r)


def test_backtest_loads_tape(tmp_path: Path) -> None:
    p = tmp_path / "t.csv"
    _write_tape(p, [(0, 1000.0, 1.0), (5, 900.0, 0.9), (10, 800.0, 0.8)])
    events = _load_tape(p)
    assert len(events) == 3
    assert events[0].liquidity_usd == 1000.0
    assert events[1].ts == 5.0


def test_backtest_replay_clean_tape_no_actions(tmp_path: Path) -> None:
    p = tmp_path / "t.csv"
    _write_tape(p, [(i * 5, 100_000.0, 0.5) for i in range(60)])
    events = _load_tape(p)
    report = replay(events)
    assert report.alert_count == 0
    assert report.execute_count == 0
    assert report.ticks == 60


def test_backtest_replay_atomic_pull_fires_execute(tmp_path: Path) -> None:
    p = tmp_path / "t.csv"
    rows = [(i * 5, 100_000.0, 0.5) for i in range(60)]
    # After tick 60: drop to 10% — triggers critical floor.
    rows += [(i * 5, 10_000.0, 0.05) for i in range(60, 120)]
    _write_tape(p, rows)
    events = _load_tape(p)
    report = replay(events)
    assert report.execute_count > 0


def test_backtest_precision_recall_with_labels(tmp_path: Path) -> None:
    tape = tmp_path / "t.csv"
    rows = [(i * 5, 100_000.0, 0.5) for i in range(60)]
    rows += [(i * 5, 10_000.0, 0.05) for i in range(60, 120)]
    _write_tape(tape, rows)
    events = _load_tape(tape)
    labels = [(300.0, "rug")]
    report = replay(events, labels=labels)
    assert report.rugs_in_labels == 1
    assert report.rugs_detected == 1
    assert report.recall == 1.0
