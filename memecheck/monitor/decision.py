"""Decision engine — pure logic over MonitorState.

Phase 1a ships `decide_noop` only: always returns NONE. The full rule set
(critical floor / large single event / slow bleed with debounce) lands in
Phase 2 alongside the alert action layer. This separation lets Phase 1a
ship a working end-to-end "watch" pipeline that prints state to the console
without ever raising false alerts, so we can verify the math against a known
pool before wiring the alert path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from memecheck.monitor.state import MonitorState


# Action values are intentionally string-typed for cheap JSON-ability later.
ACTION_NONE: str = "NONE"
ACTION_ALERT: str = "ALERT"
ACTION_EXECUTE: str = "EXECUTE"


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


def decide_noop(state: MonitorState) -> Decision:
    """Phase 1a placeholder: never fires. Reports the windowed deltas as
    metrics so the runner can print them for verification."""
    metrics: dict[str, Any] = {
        "count": state.count,
    }
    if state.current is not None:
        metrics["liquidity_usd"] = state.current.liquidity_usd
        metrics["price_usd"] = state.current.price_usd
    metrics["delta_vs_baseline_pct"] = state.liquidity_vs_baseline_pct()
    metrics["delta_10s_pct"] = state.windowed_delta_pct(10)
    metrics["delta_60s_pct"] = state.windowed_delta_pct(60)
    metrics["delta_300s_pct"] = state.windowed_delta_pct(300)
    return Decision(action=ACTION_NONE, reason=None, metrics=metrics)
