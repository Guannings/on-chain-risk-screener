"""Latency recorder unit tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memecheck.common.latency import LatencyRecorder, LatencySample


def _mk(tick: int, fetch_s: float, decide_s: float, dispatch_s=None) -> LatencySample:
    return LatencySample(
        tick=tick,
        fetch_s=fetch_s,
        decide_s=decide_s,
        dispatch_s=dispatch_s,
        total_s=fetch_s + decide_s + (dispatch_s or 0.0),
    )


def test_empty_summary_safe() -> None:
    rec = LatencyRecorder()
    summ = rec.summary()
    assert summ == {"samples": 0}
    assert "no samples" in rec.format_summary()


def test_summary_reports_p50_p99() -> None:
    rec = LatencyRecorder()
    for i in range(100):
        # fetch latencies sweep 1ms → 100ms
        rec.add(_mk(i, fetch_s=(i + 1) / 1000, decide_s=0.001))
    s = rec.summary()
    assert s["samples"] == 100
    # p50 should be near 50ms; p99 near 99ms
    assert 0.045 <= s["fetch_s"]["p50"] <= 0.055
    assert 0.095 <= s["fetch_s"]["p99"] <= 0.10


def test_dispatch_percentile_ignores_none_samples() -> None:
    rec = LatencyRecorder()
    # Most ticks have no dispatch.
    for i in range(20):
        rec.add(_mk(i, 0.001, 0.001, dispatch_s=None))
    # One tick fires an alert and dispatches in 50ms.
    rec.add(_mk(20, 0.001, 0.001, dispatch_s=0.05))
    p50 = rec.percentile("dispatch_s", 0.5)
    assert p50 == pytest.approx(0.05)


def test_log_path_writes_jsonl(tmp_path: Path) -> None:
    log = tmp_path / "latency.jsonl"
    rec = LatencyRecorder(log_path=log)
    rec.add(_mk(1, 0.005, 0.002, dispatch_s=None))
    rec.add(_mk(2, 0.004, 0.001, dispatch_s=0.012))
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["tick"] == 1
    assert first["fetch_s"] == 0.005
    assert first["dispatch_s"] is None
    second = json.loads(lines[1])
    assert second["dispatch_s"] == 0.012


def test_ring_buffer_caps_memory() -> None:
    """Long runs should not retain unbounded samples."""
    rec = LatencyRecorder()
    # _MAX_BUFFER = 50_000; push 60k and verify we're capped under the limit.
    for i in range(60_000):
        rec.add(_mk(i, 0.001, 0.001))
    assert rec.count <= 50_000


def test_format_summary_includes_ticks_and_percentiles() -> None:
    rec = LatencyRecorder()
    for i in range(10):
        rec.add(_mk(i, 0.01, 0.001))
    text = rec.format_summary()
    assert "latency SLO over 10 ticks" in text
    assert "fetch" in text and "decide" in text and "total" in text
