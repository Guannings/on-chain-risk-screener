"""Decision engine — stateful, with debounce.

The engine reads a `MonitorState`, computes the three rule conditions, and
returns a `Decision`. It carries small private state (consecutive-tick
counters) so debounce is enforced WITHIN the engine, not by the runner.

Rules (defaults overridable via DecisionConfig):

  CRITICAL FLOOR
    L_t / L_0 < CRITICAL_LIQ_RATIO  (default 0.5)
    → EXECUTE immediately, no debounce.
    Rationale: half the liquidity is gone since you started watching; whether
    by atomic pull or fast bleed, sit-and-wait is not an option.

  LARGE SINGLE EVENT
    ΔL_10s ≤ -LARGE_EVENT_PCT  (default -20%)
    → EXECUTE after LARGE_EVENT_DEBOUNCE consecutive ticks (default 2).
    Rationale: a single 20% drop in 10 seconds is almost certainly real
    distribution or a partial pull. Two consecutive ticks of -20% removes
    false positives from one bad polled sample.

  SLOW BLEED
    ΔL_60s ≤ -SLOW_BLEED_60S_PCT  (default -10%)  AND
    ΔL_300s ≤ -SLOW_BLEED_300S_PCT  (default -15%)
    → ALERT immediately on first hit; escalate to EXECUTE after
      SLOW_BLEED_DEBOUNCE consecutive ticks (default 6, ≈ 30s at 5s polling).
    Rationale: liquidity decaying steadily over minutes is the classic soft
    rug. Alert first to give the human a chance to react; escalate if it
    keeps bleeding.

  Otherwise → NONE (no action). The decision metrics still report the
  observed windowed deltas for audit and console display.

If multiple rules fire on the same tick, the most severe wins:
  EXECUTE > ALERT > NONE
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from memecheck.monitor.state import MonitorState


ACTION_NONE: str = "NONE"
ACTION_ALERT: str = "ALERT"
ACTION_EXECUTE: str = "EXECUTE"

# Severity ordering for "most severe wins" logic.
_SEVERITY: dict[str, int] = {ACTION_NONE: 0, ACTION_ALERT: 1, ACTION_EXECUTE: 2}


@dataclass(frozen=True)
class Decision:
    action: str
    reason: Optional[str] = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> int:
        return _SEVERITY.get(self.action, 0)


@dataclass(frozen=True)
class DecisionConfig:
    """All thresholds for the decision engine, overridable at construction.

    Defaults match the plan you approved. Override individual fields via
    `DecisionConfig(critical_liq_ratio=0.4, ...)`.
    """

    # Critical floor — liquidity vs starting baseline.
    critical_liq_ratio: float = 0.5

    # Large single event — 10-second window.
    large_event_pct: float = -20.0           # ΔL_10s ≤ this triggers
    large_event_debounce: int = 2            # required consecutive ticks

    # Slow bleed — 60s AND 300s windows both bad.
    slow_bleed_60s_pct: float = -10.0
    slow_bleed_300s_pct: float = -15.0
    slow_bleed_debounce: int = 6             # ticks to escalate ALERT → EXECUTE


DEFAULT_CONFIG: DecisionConfig = DecisionConfig()


def decide_noop(state: MonitorState) -> Decision:
    """Phase 1a placeholder kept for tests / dry-run mode. Always NONE.

    Useful for verifying that the source + state + runner pipeline behaves
    correctly without ever firing an action. Reports the same metrics shape
    as the real Decider so audit log entries are uniform.
    """
    return Decision(
        action=ACTION_NONE,
        reason=None,
        metrics=_metrics_snapshot(state),
    )


def _metrics_snapshot(state: MonitorState) -> dict[str, Any]:
    m: dict[str, Any] = {"count": state.count}
    cur = state.current
    if cur is not None:
        m["liquidity_usd"] = cur.liquidity_usd
        m["price_usd"] = cur.price_usd
    m["delta_vs_baseline_pct"] = state.liquidity_vs_baseline_pct()
    m["delta_10s_pct"] = state.windowed_delta_pct(10)
    m["delta_60s_pct"] = state.windowed_delta_pct(60)
    m["delta_300s_pct"] = state.windowed_delta_pct(300)
    return m


class Decider:
    """Stateful decision engine with per-rule debounce counters.

    Construct one per monitored token and call `decide(state)` once per
    tick. The instance tracks how many consecutive ticks have met each
    rule's condition so debounce logic is internal.
    """

    def __init__(self, config: DecisionConfig = DEFAULT_CONFIG) -> None:
        self._config = config
        # Consecutive-tick counters for each rule's condition.
        self._large_event_streak: int = 0
        self._slow_bleed_streak: int = 0

    @property
    def config(self) -> DecisionConfig:
        return self._config

    def decide(self, state: MonitorState) -> Decision:
        metrics = _metrics_snapshot(state)
        cfg = self._config

        # ----- 1. Critical floor (highest priority, no debounce) ---------
        baseline_pct = state.liquidity_vs_baseline_pct()
        if baseline_pct is not None and (1.0 + baseline_pct / 100.0) < cfg.critical_liq_ratio:
            # Once critical, latch the streaks to "armed" so subsequent
            # ticks of the same severity don't reset the slower paths.
            self._large_event_streak = max(self._large_event_streak, cfg.large_event_debounce)
            self._slow_bleed_streak = max(self._slow_bleed_streak, cfg.slow_bleed_debounce)
            return Decision(
                action=ACTION_EXECUTE,
                reason=(
                    f"Liquidity at {1.0 + baseline_pct / 100.0:.0%} of L0 "
                    f"(critical floor {cfg.critical_liq_ratio:.0%})"
                ),
                metrics=metrics,
            )

        # ----- 2. Large single event (10s window, with debounce) --------
        d10 = state.windowed_delta_pct(10)
        if d10 is not None and d10 <= cfg.large_event_pct:
            self._large_event_streak += 1
        else:
            self._large_event_streak = 0

        # ----- 3. Slow bleed (60s + 300s windows together) --------------
        d60 = state.windowed_delta_pct(60)
        d300 = state.windowed_delta_pct(300)
        slow_bleed_condition = (
            d60 is not None
            and d300 is not None
            and d60 <= cfg.slow_bleed_60s_pct
            and d300 <= cfg.slow_bleed_300s_pct
        )
        if slow_bleed_condition:
            self._slow_bleed_streak += 1
        else:
            self._slow_bleed_streak = 0

        # ----- Resolve to a single decision (most severe wins) ----------
        best: Decision = Decision(action=ACTION_NONE, reason=None, metrics=metrics)

        if self._large_event_streak >= cfg.large_event_debounce:
            d = Decision(
                action=ACTION_EXECUTE,
                reason=(
                    f"Large single event: ΔL_10s = {d10:.1f}% "
                    f"for {self._large_event_streak} consecutive tick(s)"
                ),
                metrics=metrics,
            )
            if d.severity > best.severity:
                best = d

        if slow_bleed_condition:
            if self._slow_bleed_streak >= cfg.slow_bleed_debounce:
                d = Decision(
                    action=ACTION_EXECUTE,
                    reason=(
                        f"Slow bleed escalated: ΔL_60s={d60:.1f}%, "
                        f"ΔL_300s={d300:.1f}% for {self._slow_bleed_streak} consecutive tick(s)"
                    ),
                    metrics=metrics,
                )
            else:
                d = Decision(
                    action=ACTION_ALERT,
                    reason=(
                        f"Slow bleed: ΔL_60s={d60:.1f}%, ΔL_300s={d300:.1f}%"
                    ),
                    metrics=metrics,
                )
            if d.severity > best.severity:
                best = d

        return best
