"""Backtest harness — replay a tape against the Decider.

Tape format (CSV, header row required):
    timestamp,liquidity_usd,price_usd

Optional labels file (CSV, header row):
    timestamp,event
where `event` is one of `rug`, `migration`, `none`.

The harness feeds events from the tape into MonitorState + Decider tick by
tick, records every non-NONE decision, and optionally computes precision
and recall vs labels.

A "rug" is considered detected if any `ALERT` or `EXECUTE` decision fires
within `--tolerance` seconds of a labelled `rug` event. Precision is
detections / total non-NONE decisions; recall is detections / total rugs.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from memecheck.monitor.decision import (
    ACTION_ALERT,
    ACTION_EXECUTE,
    ACTION_NONE,
    DEFAULT_CONFIG,
    Decider,
    DecisionConfig,
)
from memecheck.monitor.source import LiquidityEvent
from memecheck.monitor.state import MonitorState


DEFAULT_TOLERANCE_SECONDS: float = 60.0


@dataclass(frozen=True)
class BacktestReport:
    ticks: int
    none_count: int
    alert_count: int
    execute_count: int
    actions: list[dict[str, Any]] = field(default_factory=list)
    rugs_in_labels: int = 0
    rugs_detected: int = 0
    false_positives: int = 0
    precision: Optional[float] = None
    recall: Optional[float] = None
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS


def _load_tape(path: Path) -> list[LiquidityEvent]:
    events: list[LiquidityEvent] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = float(row["timestamp"])
                liq = float(row["liquidity_usd"])
                price = float(row["price_usd"])
            except (KeyError, ValueError, TypeError) as e:
                raise ValueError(f"bad tape row {row!r}: {e}")
            events.append(
                LiquidityEvent(
                    ts=ts,
                    base_reserve=0.0,
                    quote_reserve=0.0,
                    quote_price_usd=1.0,
                    liquidity_usd=liq,
                    price_usd=price,
                    source="tape",
                )
            )
    return events


def _load_labels(path: Path) -> list[tuple[float, str]]:
    labels: list[tuple[float, str]] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = float(row["timestamp"])
            evt = (row.get("event") or "none").strip().lower()
            labels.append((ts, evt))
    return labels


def replay(
    tape: list[LiquidityEvent],
    *,
    config: DecisionConfig = DEFAULT_CONFIG,
    labels: Optional[list[tuple[float, str]]] = None,
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
) -> BacktestReport:
    state = MonitorState()
    decider = Decider(config)

    actions: list[dict[str, Any]] = []
    none_count = 0
    alert_count = 0
    execute_count = 0

    for ev in tape:
        state.add(ev)
        dec = decider.decide(state)
        if dec.action == ACTION_NONE:
            none_count += 1
            continue
        actions.append({
            "ts": ev.ts,
            "action": dec.action,
            "reason": dec.reason,
            "liquidity_usd": ev.liquidity_usd,
            "delta_vs_baseline_pct": dec.metrics.get("delta_vs_baseline_pct"),
        })
        if dec.action == ACTION_ALERT:
            alert_count += 1
        elif dec.action == ACTION_EXECUTE:
            execute_count += 1

    rugs_in_labels = 0
    rugs_detected = 0
    false_positives = 0
    precision: Optional[float] = None
    recall: Optional[float] = None

    if labels is not None:
        rug_times = [ts for ts, evt in labels if evt == "rug"]
        rugs_in_labels = len(rug_times)

        # A non-NONE action within `tolerance_seconds` of a rug counts as
        # a detection. Detect each rug at most once.
        unmatched_actions = list(actions)
        for rug_ts in rug_times:
            matched_idx: Optional[int] = None
            for i, a in enumerate(unmatched_actions):
                if abs(a["ts"] - rug_ts) <= tolerance_seconds:
                    matched_idx = i
                    break
            if matched_idx is not None:
                rugs_detected += 1
                unmatched_actions.pop(matched_idx)

        false_positives = len(unmatched_actions)
        total_actions = alert_count + execute_count
        if total_actions > 0:
            precision = (total_actions - false_positives) / total_actions
        if rugs_in_labels > 0:
            recall = rugs_detected / rugs_in_labels

    return BacktestReport(
        ticks=len(tape),
        none_count=none_count,
        alert_count=alert_count,
        execute_count=execute_count,
        actions=actions,
        rugs_in_labels=rugs_in_labels,
        rugs_detected=rugs_detected,
        false_positives=false_positives,
        precision=precision,
        recall=recall,
        tolerance_seconds=tolerance_seconds,
    )


def format_report(report: BacktestReport) -> str:
    lines = []
    lines.append("=== BACKTEST REPORT ===")
    lines.append(f"  Ticks replayed:   {report.ticks}")
    lines.append(f"  Decisions:        "
                 f"NONE={report.none_count}, ALERT={report.alert_count}, "
                 f"EXECUTE={report.execute_count}")
    if report.rugs_in_labels > 0 or report.recall is not None:
        lines.append("")
        lines.append("=== LABELLED EVENTS ===")
        lines.append(f"  Tolerance:        ±{report.tolerance_seconds:.0f}s")
        lines.append(f"  Rugs in labels:   {report.rugs_in_labels}")
        lines.append(f"  Rugs detected:    {report.rugs_detected}")
        lines.append(f"  False positives:  {report.false_positives}")
        if report.precision is not None:
            lines.append(f"  Precision:        {report.precision:.2%}")
        if report.recall is not None:
            lines.append(f"  Recall:           {report.recall:.2%}")
    if report.actions:
        lines.append("")
        lines.append("=== ACTIONS (first 10) ===")
        for a in report.actions[:10]:
            lines.append(
                f"  t={a['ts']:>10.0f}  [{a['action']}]  "
                f"liq=${a['liquidity_usd']:,.0f}  {a['reason']}"
            )
        if len(report.actions) > 10:
            lines.append(f"  … +{len(report.actions) - 10} more")
    return "\n".join(lines)


def run_backtest_cli(
    tape_path: str,
    *,
    labels_path: Optional[str] = None,
    critical_ratio_override: Optional[float] = None,
    as_json: bool = False,
) -> int:
    p = Path(tape_path)
    if not p.exists():
        print(f"backtest: tape not found at {p}", file=sys.stderr)
        return 3
    try:
        tape = _load_tape(p)
    except ValueError as e:
        print(f"backtest: {e}", file=sys.stderr)
        return 3

    labels: Optional[list[tuple[float, str]]] = None
    if labels_path:
        lp = Path(labels_path)
        if not lp.exists():
            print(f"backtest: labels not found at {lp}", file=sys.stderr)
            return 3
        try:
            labels = _load_labels(lp)
        except (KeyError, ValueError) as e:
            print(f"backtest: bad labels file: {e}", file=sys.stderr)
            return 3

    config = DEFAULT_CONFIG
    if critical_ratio_override is not None:
        config = DecisionConfig(
            critical_liq_ratio=critical_ratio_override,
            large_event_pct=DEFAULT_CONFIG.large_event_pct,
            large_event_debounce=DEFAULT_CONFIG.large_event_debounce,
            slow_bleed_60s_pct=DEFAULT_CONFIG.slow_bleed_60s_pct,
            slow_bleed_300s_pct=DEFAULT_CONFIG.slow_bleed_300s_pct,
            slow_bleed_debounce=DEFAULT_CONFIG.slow_bleed_debounce,
        )

    report = replay(tape, config=config, labels=labels)

    if as_json:
        print(json.dumps({
            "tape": str(p),
            "labels": str(labels_path) if labels_path else None,
            "ticks": report.ticks,
            "decisions": {
                "NONE": report.none_count,
                "ALERT": report.alert_count,
                "EXECUTE": report.execute_count,
            },
            "labels_summary": {
                "rugs_in_labels": report.rugs_in_labels,
                "rugs_detected": report.rugs_detected,
                "false_positives": report.false_positives,
                "precision": report.precision,
                "recall": report.recall,
                "tolerance_seconds": report.tolerance_seconds,
            },
            "actions": report.actions,
        }, indent=2, default=str))
    else:
        print(format_report(report))

    return 0
