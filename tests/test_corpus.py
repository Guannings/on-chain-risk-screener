"""Corpus builder tests — focus on the pure detect_rug logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from memecheck.common.corpus import (
    MIN_HOURS_OF_HISTORY,
    MIN_PEAK_VOLUME_USD,
    NO_RECOVERY_RATIO,
    PEAK_TO_TROUGH_CRASH_RATIO,
    RugCandidate,
    detect_rug,
    write_aggregated,
    write_tape,
)


def _row(ts: int, close: float, volume: float = 10_000.0) -> list[float]:
    """One GT-format OHLCV row [ts, open, high, low, close, volume]."""
    return [ts, close, close, close, close, volume]


def _flat_then_crash(
    *, n_flat: int = 30, peak: float = 1.0, n_crash: int = 30, crash_close: float = 0.001
) -> list[list[float]]:
    """Synthetic OHLCV: n_flat hours at `peak`, then immediate crash."""
    rows = []
    for i in range(n_flat):
        rows.append(_row(1_000_000 + i * 3600, peak))
    for i in range(n_crash):
        rows.append(_row(1_000_000 + (n_flat + i) * 3600, crash_close, volume=1.0))
    return rows


def test_detects_classic_rug() -> None:
    """Flat at peak, then collapse to <5%, no recovery → rugged."""
    rows = _flat_then_crash()
    out = detect_rug(rows)
    assert out is not None
    peak_ts, peak_close, rug_ts, rug_close = out
    assert peak_close == pytest.approx(1.0)
    assert rug_close <= PEAK_TO_TROUGH_CRASH_RATIO * peak_close
    assert rug_ts > peak_ts


def test_rejects_history_too_short() -> None:
    """Fewer than MIN_HOURS_OF_HISTORY rows → can't decide."""
    rows = _flat_then_crash(n_flat=5, n_crash=5)
    assert detect_rug(rows) is None


def test_rejects_no_meaningful_volume_at_peak() -> None:
    """A pool that never had real volume at its peak isn't a 'real' rug."""
    rows = []
    for i in range(40):
        rows.append(_row(1_000_000 + i * 3600, 1.0, volume=10.0))    # below MIN_PEAK_VOLUME_USD
    for i in range(40):
        rows.append(_row(1_000_000 + (40 + i) * 3600, 0.001, volume=1.0))
    assert detect_rug(rows) is None


def test_rejects_clean_pool() -> None:
    """Stable price, no crash → no rug."""
    rows = [_row(1_000_000 + i * 3600, 1.0 + 0.01 * (i % 5)) for i in range(60)]
    assert detect_rug(rows) is None


def test_rejects_dip_with_recovery() -> None:
    """Crash then bounce back above the no-recovery threshold → NOT a rug."""
    rows = []
    for i in range(30):
        rows.append(_row(1_000_000 + i * 3600, 1.0))
    # 1-hour crash to 0.001
    rows.append(_row(1_000_000 + 30 * 3600, 0.001))
    # Then recover to 0.5 (above NO_RECOVERY_RATIO=0.2 of peak)
    for i in range(30):
        rows.append(_row(1_000_000 + (31 + i) * 3600, 0.5))
    assert detect_rug(rows) is None


def test_handles_newest_first_ordering() -> None:
    """GT returns newest-first; detector must sort internally."""
    rows = _flat_then_crash()
    out_chrono = detect_rug(rows)
    out_reversed = detect_rug(list(reversed(rows)))
    assert out_chrono == out_reversed


def test_write_tape_round_trips(tmp_path: Path) -> None:
    """Tape format must match what _load_tape consumes."""
    rows = _flat_then_crash()
    out = detect_rug(rows)
    assert out is not None
    peak_ts, peak_close, rug_ts, rug_close = out

    cand = RugCandidate(
        chain="solana", pool_address="POOL_ADDR_FAKE",
        pool_name="TEST / SOL", peak_ts=peak_ts, peak_close=peak_close,
        rug_ts=rug_ts, rug_close=rug_close, ohlcv_rows=rows,
    )
    tape_path, labels_path = write_tape(cand, tmp_path / "event")
    assert tape_path.exists()
    assert labels_path.exists()

    # The tape must load cleanly through the existing backtest reader.
    from memecheck.common.backtest import _load_tape, _load_labels
    tape = _load_tape(tape_path)
    labels = _load_labels(labels_path)
    assert len(tape) == len(rows)
    assert len(labels) == 1
    assert labels[0][1] == "rug"


def test_write_aggregated_offsets_events(tmp_path: Path) -> None:
    """Aggregated tape must give each event a non-overlapping window so the
    sweep treats them as distinct timelines."""
    cands = []
    for i in range(3):
        rows = _flat_then_crash()
        out = detect_rug(rows)
        assert out is not None
        peak_ts, peak_close, rug_ts, rug_close = out
        cands.append(RugCandidate(
            chain="solana", pool_address=f"POOL_{i}",
            pool_name=f"TEST{i}/SOL", peak_ts=peak_ts, peak_close=peak_close,
            rug_ts=rug_ts, rug_close=rug_close, ohlcv_rows=rows,
        ))
    tape_path, labels_path = write_aggregated(cands, tmp_path)
    from memecheck.common.backtest import _load_labels, _load_tape
    tape = _load_tape(tape_path)
    labels = _load_labels(labels_path)
    assert len(labels) == 3
    # Labels should be 10-day-spaced.
    ts_sorted = sorted(t for t, _ in labels)
    assert ts_sorted[1] - ts_sorted[0] >= 10 * 86400 - 7200
