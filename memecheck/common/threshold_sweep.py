"""Grid-search over Decider thresholds to find a Pareto-optimal operating point.

Self-review item #1: the rule thresholds (`critical_liq_ratio`,
`large_event_pct`, `slow_bleed_60s_pct`) are hand-tuned and have no
data behind them. This module sweeps a grid of values, replays the same
tape against each, and reports the precision-vs-recall frontier so the
user can pick a defensible operating point.

Workflow
--------
1. Pick a tape + labels (synthetic from `samples/` or a real labelled
   corpus the user assembles).
2. Define a grid for one or more thresholds. The CLI takes
   `--critical-ratio 0.3,0.4,0.5,0.6,0.7` etc.
3. Run replay for each combination, compute F1 / precision / recall.
4. Identify the Pareto frontier (no other point dominates this point
   in BOTH precision AND recall).
5. Print a small table; the JSON output carries the full grid for
   downstream plotting.

This is the gateway to actually saying "memecheck achieves X% recall
at Y% FPR on dataset Z" — a concrete claim that today's hand-tuned
thresholds cannot honestly back.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Optional

from memecheck.common.backtest import _load_labels, _load_tape, replay
from memecheck.monitor.decision import DEFAULT_CONFIG, DecisionConfig


@dataclass(frozen=True)
class SweepPoint:
    """One (config, metrics) pair from the sweep."""
    critical_ratio: float
    large_event_pct: float
    slow_bleed_60s_pct: float
    precision: Optional[float]
    recall: Optional[float]
    f1: Optional[float]
    fpr_ticks: Optional[float]
    tp_ticks: int
    fp_ticks: int


def _config_for(
    critical_ratio: float,
    large_event_pct: float,
    slow_bleed_60s_pct: float,
) -> DecisionConfig:
    """Build a DecisionConfig with one or more thresholds overridden."""
    return DecisionConfig(
        critical_liq_ratio=critical_ratio,
        large_event_pct=large_event_pct,
        large_event_debounce=DEFAULT_CONFIG.large_event_debounce,
        slow_bleed_60s_pct=slow_bleed_60s_pct,
        slow_bleed_300s_pct=DEFAULT_CONFIG.slow_bleed_300s_pct,
        slow_bleed_debounce=DEFAULT_CONFIG.slow_bleed_debounce,
    )


def sweep(
    tape_path: Path,
    labels_path: Path,
    *,
    critical_ratios: list[float],
    large_event_pcts: list[float],
    slow_bleed_60s_pcts: list[float],
    tolerance_seconds: float = 60.0,
) -> list[SweepPoint]:
    """Replay the tape for each grid combination. Returns one point per combo."""
    tape = _load_tape(tape_path)
    labels = _load_labels(labels_path)

    points: list[SweepPoint] = []
    for cr, lep, sbp in product(critical_ratios, large_event_pcts, slow_bleed_60s_pcts):
        cfg = _config_for(cr, lep, sbp)
        report = replay(
            tape, config=cfg, labels=labels, tolerance_seconds=tolerance_seconds
        )
        points.append(SweepPoint(
            critical_ratio=cr,
            large_event_pct=lep,
            slow_bleed_60s_pct=sbp,
            precision=report.precision,
            recall=report.recall,
            f1=report.f1,
            fpr_ticks=report.fpr_ticks,
            tp_ticks=report.tp_ticks,
            fp_ticks=report.fp_ticks,
        ))
    return points


def pareto_frontier(points: list[SweepPoint]) -> list[SweepPoint]:
    """A point is on the frontier if no other point has BOTH higher
    precision AND higher recall (with at least one strictly higher)."""
    eligible = [p for p in points if p.precision is not None and p.recall is not None]
    frontier: list[SweepPoint] = []
    for p in eligible:
        dominated = False
        for q in eligible:
            if q is p:
                continue
            qp = q.precision
            qr = q.recall
            pp = p.precision
            pr = p.recall
            assert qp is not None and qr is not None and pp is not None and pr is not None
            if qp >= pp and qr >= pr and (qp > pp or qr > pr):
                dominated = True
                break
        if not dominated:
            frontier.append(p)
    return frontier


def format_sweep_table(points: list[SweepPoint], frontier: list[SweepPoint]) -> str:
    """Pretty-print the sweep as a small table, marking frontier points with *."""
    frontier_set = set(id(p) for p in frontier)
    lines = []
    lines.append("=== THRESHOLD SWEEP ===")
    lines.append(
        "  crit-ratio │ large-evt │ bleed-60s │  prec  │ recall │   F1   │  FPR   │ pareto"
    )
    lines.append(
        "  ───────────┼───────────┼───────────┼────────┼────────┼────────┼────────┼───────"
    )
    # Sort by F1 desc for human readability.
    sorted_pts = sorted(
        points, key=lambda p: (p.f1 if p.f1 is not None else -1), reverse=True,
    )
    for p in sorted_pts:
        prec = f"{p.precision:.2%}" if p.precision is not None else "  N/A "
        recall = f"{p.recall:.2%}" if p.recall is not None else "  N/A "
        f1 = f"{p.f1:.2%}" if p.f1 is not None else "  N/A "
        fpr = f"{p.fpr_ticks:.2%}" if p.fpr_ticks is not None else "  N/A "
        star = "  *  " if id(p) in frontier_set else "     "
        lines.append(
            f"     {p.critical_ratio:>5.2f}  │   "
            f"{p.large_event_pct:+5.1f}  │   "
            f"{p.slow_bleed_60s_pct:+5.1f}  │ {prec} │ {recall} │ {f1} │ {fpr} │ {star}"
        )
    if frontier:
        lines.append("")
        lines.append("=== PARETO FRONTIER ===")
        for p in sorted(
            frontier,
            key=lambda x: (x.recall if x.recall is not None else 0),
            reverse=True,
        ):
            lines.append(
                f"  crit={p.critical_ratio:.2f}  large={p.large_event_pct:+.1f}%  "
                f"bleed60={p.slow_bleed_60s_pct:+.1f}%  →  "
                f"P={p.precision:.2%}  R={p.recall:.2%}  F1={p.f1:.2%}"
            )
    return "\n".join(lines)
